#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the Windmill worker.

Workers are stateless and pull jobs from the shared Windmill PostgreSQL queue.
They do not talk to the server; only to PostgreSQL. Scale horizontally with
``juju scale-application windmill-worker N``.
"""

from __future__ import annotations

import logging
from typing import Optional

import ops
from charms.loki_k8s.v1.loki_push_api import LogForwarder
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.tempo_coordinator_k8s.v0.tracing import TracingEndpointRequirer

import windmill_worker
from database import Database, DatabaseInfo

logger = logging.getLogger(__name__)


class WindmillWorkerCharm(ops.CharmBase):
    """Charm the Windmill worker."""

    def __init__(self, framework: ops.Framework) -> None:
        super().__init__(framework)

        self.container = self.unit.get_container(windmill_worker.CONTAINER_NAME)
        self.database = Database(self)

        self.metrics_endpoint = MetricsEndpointProvider(
            self,
            relation_name="metrics-endpoint",
            refresh_event=[
                self.on[windmill_worker.CONTAINER_NAME].pebble_ready,
                self.on.config_changed,
            ],
            jobs=[
                {
                    "metrics_path": "/metrics",
                    "static_configs": [{"targets": [f"*:{windmill_worker.METRICS_PORT}"]}],
                }
            ],
        )
        self._log_forwarder = LogForwarder(self, relation_name="logging")
        self.tracing = TracingEndpointRequirer(
            self,
            relation_name="tracing",
            protocols=["otlp_http", "otlp_grpc"],
        )

        framework.observe(self.on[windmill_worker.CONTAINER_NAME].pebble_ready, self._reconcile)
        framework.observe(self.on.config_changed, self._reconcile)
        framework.observe(self.on.update_status, self._reconcile)

        db_on = self.database.on
        framework.observe(db_on.changed, self._reconcile)

        framework.observe(self.tracing.on.endpoint_changed, self._reconcile)  # type: ignore[attr-defined]
        framework.observe(self.tracing.on.endpoint_removed, self._reconcile)  # type: ignore[attr-defined]

        framework.observe(self.on.restart_action, self._on_restart_action)

    def _noop_event(self, event: ops.EventBase) -> None:  # pragma: no cover - defensive
        """No-op handler used when an optional event source is absent."""

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

        self.unit.status = ops.MaintenanceStatus("configuring worker")
        try:
            self._render_layer(db_info)
        except ops.pebble.Error as exc:
            logger.warning("failed to update workload layer: %s", exc)
            self.unit.status = ops.ErrorStatus(f"workload layer error: {exc}")
            return

        self.unit.status = ops.WaitingStatus("waiting for worker to be ready")
        if self._is_running():
            self._set_active()

    def _stop_workload(self) -> None:
        if self.container.get_services().get(windmill_worker.SERVICE_NAME):
            try:
                self.container.stop(windmill_worker.SERVICE_NAME)
            except ops.pebble.Error as exc:  # pragma: no cover - defensive
                logger.warning("could not stop workload: %s", exc)

    def _render_layer(self, db_info: DatabaseInfo) -> None:
        layer = windmill_worker.build_layer(
            database_url=db_info.connection_string,
            worker_group=str(self.config["worker-group"]),
            native_mode=bool(self.config["native-mode"]),
            worker_tags=str(self.config["worker-tags"]),
            sleep_queue=int(self.config["sleep-queue"]),
            keep_job_dir=bool(self.config["keep-job-dir"]),
            min_free_disk_space_mb=int(self.config["min-free-disk-space-mb"]),
            enable_pid_isolation=bool(self.config["enable-pid-isolation"]),
            disable_nsjail=bool(self.config["disable-nsjail"]),
            json_logs=bool(self.config["json-logs"]),
            enable_metrics=bool(self.config["enable-metrics"]),
            py_concurrent_downloads=int(self.config["py-concurrent-downloads"]),
            pip_local_dependencies=str(self.config["pip-local-dependencies"]),
            whitelist_envs=str(self.config["whitelist-envs"]),
            extra_env=self._tracing_env(),
        )
        self.container.add_layer("windmill-worker", ops.pebble.Layer(layer), combine=True)
        self.container.replan()

    def _tracing_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        try:
            if self.tracing.is_ready():
                endpoint = self.tracing.get_endpoint("otlp_http")
                if endpoint:
                    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = str(endpoint)
                    env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
                    env["OTEL_TRACES_EXPORTER"] = "otlp"
                    env["OTEL_RESOURCE_ATTRIBUTES"] = (
                        f"service.name=windmill-worker,"
                        f"service.namespace={self.model.name},"
                        f"service.instance.id={self.unit.name}"
                    )
        except Exception:  # noqa: BLE001 - tracing is best-effort
            logger.debug("tracing endpoint not available", exc_info=True)
        return env

    def _is_running(self) -> bool:
        if not self.container.can_connect():
            return False
        info = self.container.get_services().get(windmill_worker.SERVICE_NAME)
        return info is not None and info.is_running()

    def _set_active(self) -> None:
        version = self._workload_version()
        if version:
            self.unit.set_workload_version(version)
        group = str(self.config["worker-group"])
        self.unit.status = ops.ActiveStatus(f"ready (group={group})")

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

    def _on_restart_action(self, event: ops.ActionEvent) -> None:
        if not self.container.can_connect():
            event.fail("workload container is not ready")
            return
        try:
            self.container.restart(windmill_worker.SERVICE_NAME)
        except ops.pebble.Error as exc:
            event.fail(f"failed to restart: {exc}")
            return
        event.set_results({"result": "restarted"})


if __name__ == "__main__":  # pragma: no cover
    ops.main(WindmillWorkerCharm)
