#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the Windmill server.

The Windmill server is the stateless API, web frontend and job scheduler. All
state lives in PostgreSQL, provided over the ``postgresql_client`` relation.
Workers (deployed separately via the ``windmill-worker`` charm) share the same
database and pull jobs from its queue.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

import ops
from charms.loki_k8s.v1.loki_push_api import LogForwarder
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.tempo_coordinator_k8s.v0.tracing import TracingEndpointRequirer
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer

import db_init
import windmill
from database import Database, DatabaseInfo

logger = logging.getLogger(__name__)


class WindmillCharm(ops.CharmBase):
    """Charm the Windmill server."""

    def __init__(self, framework: ops.Framework) -> None:
        super().__init__(framework)

        self.container = self.unit.get_container(windmill.CONTAINER_NAME)
        self.database = Database(self)

        self.ingress = IngressPerAppRequirer(
            self,
            relation_name="ingress",
            port=windmill.SERVER_PORT,
            scheme="http",
            redirect_https=False,
        )
        self.metrics_endpoint = MetricsEndpointProvider(
            self,
            relation_name="metrics-endpoint",
            refresh_event=[
                self.on[windmill.CONTAINER_NAME].pebble_ready,
                self.on.config_changed,
            ],
            jobs=[
                {
                    "metrics_path": windmill.METRICS_PATH,
                    "static_configs": [{"targets": [f"*:{windmill.METRICS_PORT}"]}],
                }
            ],
        )
        # Forward all Pebble workload logs to Loki when related.
        self._log_forwarder = LogForwarder(self, relation_name="logging")
        # Request OTLP receivers; the endpoint (if any) is exposed to job scripts
        # via standard OTEL_* environment variables so they can export traces.
        self.tracing = TracingEndpointRequirer(
            self,
            relation_name="tracing",
            protocols=["otlp_http", "otlp_grpc"],
        )

        framework.observe(self.on[windmill.CONTAINER_NAME].pebble_ready, self._reconcile)
        framework.observe(self.on.config_changed, self._reconcile)
        framework.observe(self.on.update_status, self._reconcile)
        framework.observe(self.on.leader_settings_changed, self._reconcile)

        db_on = self.database.on
        framework.observe(db_on.changed, self._reconcile)

        framework.observe(self.ingress.on.ready, self._on_ingress_changed)
        framework.observe(self.ingress.on.revoked, self._on_ingress_changed)

        framework.observe(self.tracing.on.endpoint_changed, self._reconcile)  # type: ignore[attr-defined]
        framework.observe(self.tracing.on.endpoint_removed, self._reconcile)  # type: ignore[attr-defined]

        framework.observe(self.on.restart_action, self._on_restart_action)
        framework.observe(self.on.init_db_action, self._on_init_db_action)
        framework.observe(self.on.set_admin_password_action, self._on_set_admin_password_action)
        framework.observe(self.on.pre_backup_action, self._on_pre_backup_action)
        framework.observe(self.on.post_restore_action, self._on_post_restore_action)

    # ------------------------------------------------------------------ events

    def _noop_event(self, event: ops.EventBase) -> None:  # pragma: no cover - defensive
        """No-op handler used when an optional event source is absent."""

    def _on_ingress_changed(self, event: ops.EventBase) -> None:
        """Reconcile when the ingress URL appears or changes."""
        self._reconcile(event)

    # ----------------------------------------------------------------- reconcile

    def _reconcile(self, event: ops.EventBase) -> None:
        """Reconcile charm state: relations, config, workload, status."""
        if not self.container.can_connect():
            self.unit.status = ops.WaitingStatus("waiting for workload container")
            return

        db_info = self.database.get_info()
        if db_info is None:
            self.unit.status = ops.BlockedStatus("add relation: database")
            self._stop_workload()
            return

        if not self._is_leader_or_config_settled():
            # Non-leader units still need to render their own layer; leadership
            # only matters for publishing relation data, which the libraries
            # guard internally. Proceed to render the workload.
            pass

        self.unit.status = ops.MaintenanceStatus("configuring windmill")
        self._ensure_ca_cert(db_info)
        try:
            self._render_layer(db_info)
        except (ops.pebble.Error, ops.pebble.ChangeError) as exc:
            # A ChangeError here usually means the workload exited during
            # startup (e.g. a failed DB migration). Don't put the unit into
            # error state — stay waiting so the operator can remediate (e.g.
            # by running the init-db action) and update-status will retry.
            logger.warning("workload did not start cleanly: %s", exc)
            self.unit.status = ops.WaitingStatus(
                "waiting for windmill to be healthy "
                "(run 'juju run windmill/0 init-db' if DB migrations fail)"
            )
            return

        self.unit.status = ops.WaitingStatus("waiting for windmill to be healthy")
        if self._is_healthy():
            self._set_active()
        # else: stay waiting; update-status will retry.

    # ----------------------------------------------------------- workload layer

    def _stop_workload(self) -> None:
        if self.container.get_services().get(windmill.SERVICE_NAME):
            try:
                self.container.stop(windmill.SERVICE_NAME)
            except ops.pebble.Error as exc:  # pragma: no cover - defensive
                logger.warning("could not stop workload: %s", exc)

    def _render_layer(self, db_info: DatabaseInfo) -> None:
        layer = windmill.build_layer(
            database_url=db_info.connection_string,
            mode="server",
            base_url=self._effective_base_url(),
            json_logs=bool(self.config["json-logs"]),
            db_connections=int(self.config["db-connections"]),
            zombie_job_timeout=int(self.config["zombie-job-timeout"]),
            restart_zombie_jobs=bool(self.config["restart-zombie-jobs"]),
            enable_metrics=bool(self.config["enable-metrics"]),
            enable_smtp=bool(self.config["enable-smtp-trigger"]),
            superadmin_secret=str(self.config["superadmin-secret"]),
            ca_cert=str(self.config["ca-cert"]),
            extra_env=self._tracing_env(),
        )
        self.container.add_layer("windmill", ops.pebble.Layer(layer), combine=True)
        # Mount the CA cert (if any) so the workload trusts it for outbound TLS.
        self._push_ca_cert()
        self.container.replan()

    def _tracing_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        try:
            if self.tracing.is_ready():
                # Prefer http/protobuf (port 4318) as it is the simplest to use
                # from job scripts without a gRPC dependency.
                endpoint = self.tracing.get_endpoint("otlp_http")
                if endpoint:
                    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = str(endpoint)
                    env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
                    env["OTEL_TRACES_EXPORTER"] = "otlp"
                    env["OTEL_RESOURCE_ATTRIBUTES"] = (
                        f"service.name=windmill-server,"
                        f"service.namespace={self.model.name},"
                        f"service.instance.id={self.unit.name}"
                    )
        except Exception:  # noqa: BLE001 - tracing is best-effort
            logger.debug("tracing endpoint not available", exc_info=True)
        return env

    def _effective_base_url(self) -> str:
        configured = str(self.config["base-url"] or "").strip()
        if configured:
            return configured
        if self.ingress.is_ready():
            url = self.ingress.url
            if url:
                return str(url)
        return ""

    # -------------------------------------------------------------- CA certificate

    def _ensure_ca_cert(self, db_info: DatabaseInfo) -> None:
        """No-op placeholder kept for symmetry; CA is pushed in _render_layer."""

    def _push_ca_cert(self) -> None:
        ca_cert = str(self.config["ca-cert"] or "").strip()
        if not ca_cert:
            return
        if not ca_cert.endswith("\n"):
            ca_cert += "\n"
        try:
            self.container.push(
                "/usr/local/share/ca-certificates/windmill-internal-ca.crt",
                ca_cert,
                make_dirs=True,
            )
        except ops.pebble.Error as exc:  # pragma: no cover - defensive
            logger.warning("could not push CA certificate: %s", exc)

    # ------------------------------------------------------------------ health

    def _is_healthy(self) -> bool:
        if not self.container.can_connect():
            return False
        services = self.container.get_services()
        info = services.get(windmill.SERVICE_NAME)
        if info is None or not info.is_running():
            return False
        try:
            checks = self.container.get_checks(level=ops.pebble.CheckLevel.READY)
        except ops.pebble.Error:  # pragma: no cover - defensive
            return False
        for check in checks.values():
            if check.status != ops.pebble.CheckStatus.UP:
                return False
        return True

    def _set_active(self) -> None:
        version = self._workload_version()
        if version:
            self.unit.set_workload_version(version)
        base = self._effective_base_url()
        msg = f"ready at {urlparse(base).netloc}" if base else "ready"
        self.unit.status = ops.ActiveStatus(msg)

    def _workload_version(self) -> Optional[str]:
        if not self.container.can_connect():
            return None
        try:
            stdout, _ = self.container.exec(
                ["/usr/local/bin/windmill", "--version"], timeout=10
            ).wait_output()
            out = stdout.strip()
            return out or None
        except Exception:  # noqa: BLE001 - best-effort version detection
            return None

    # --------------------------------------------------------------- leadership

    def _is_leader_or_config_settled(self) -> bool:
        return self.unit.is_leader()

    # ----------------------------------------------------------------- actions

    def _on_restart_action(self, event: ops.ActionEvent) -> None:
        if not self.container.can_connect():
            event.fail("workload container is not ready")
            return
        try:
            self.container.restart(windmill.SERVICE_NAME)
        except ops.pebble.Error as exc:
            event.fail(f"failed to restart: {exc}")
            return
        event.set_results({"result": "restarted"})

    def _on_init_db_action(self, event: ops.ActionEvent) -> None:
        """Create Windmill's PostgreSQL roles via a superuser connection."""
        superuser_url = str(event.params.get("superuser-url") or "").strip()
        if not superuser_url:
            event.fail("superuser-url parameter is required")
            return
        db_info = self.database.get_info()
        if db_info is None:
            event.fail("database relation is not ready")
            return
        try:
            results = db_init.init_database(
                superuser_url=superuser_url, app_username=db_info.username
            )
        except db_init.InitDbError as exc:
            event.fail(str(exc))
            return
        # Restart the workload so the (now unblocked) migration re-runs.
        if self.container.can_connect():
            try:
                self.container.restart(windmill.SERVICE_NAME)
            except ops.pebble.Error as exc:
                logger.warning("could not restart after init-db: %s", exc)
        event.set_results(results)

    def _on_set_admin_password_action(self, event: ops.ActionEvent) -> None:
        token = str(self.config["superadmin-secret"] or "").strip()
        if not token:
            event.fail("set the superadmin-secret config option before using this action")
            return
        base = self._effective_base_url() or f"http://localhost:{windmill.SERVER_PORT}"
        client = windmill.WindmillAdminClient(base, token)
        if not client.is_healthy():
            event.fail("windmill server is not healthy")
            return
        password = str(event.params.get("password") or "").strip() or windmill.generate_password()
        try:
            client.set_user_password(windmill.DEFAULT_ADMIN_EMAIL, password)
        except windmill.WindmillAdminError as exc:
            event.fail(str(exc))
            return
        # Persist the new password as a Juju secret owned by the charm.
        try:
            secret = self.app.add_secret(
                {"username": windmill.DEFAULT_ADMIN_EMAIL, "password": password},
                label="admin-password",
            )
            _ = secret  # app-owned secret is auto-accessible to all units
        except Exception:  # noqa: BLE001 - secret is best-effort convenience
            logger.debug("could not persist admin password as a secret", exc_info=True)
        event.set_results({"password": password, "username": windmill.DEFAULT_ADMIN_EMAIL})

    def _on_pre_backup_action(self, event: ops.ActionEvent) -> None:
        if not self._is_healthy():
            event.fail("windmill server is not healthy; cannot prepare for backup")
            return
        event.set_results(
            {"result": "healthy", "note": "run 'backup' on the postgresql-k8s charm"}
        )

    def _on_post_restore_action(self, event: ops.ActionEvent) -> None:
        if not self.container.can_connect():
            event.fail("workload container is not ready")
            return
        self.unit.status = ops.MaintenanceStatus("reconciling after restore")
        try:
            self.container.restart(windmill.SERVICE_NAME)
        except ops.pebble.Error as exc:
            event.fail(f"failed to restart: {exc}")
            return
        event.set_results({"result": "restarted after restore"})


if __name__ == "__main__":  # pragma: no cover
    ops.main(WindmillCharm)
