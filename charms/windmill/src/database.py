# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""PostgreSQL client relation (postgresql_client v0 interface) wrapper.

Implements the ``postgresql_client`` interface directly, using the stable v0
databag protocol that the Charmed ``postgresql-k8s`` charm (14/stable and
16/stable) speaks. Credentials are exchanged via Juju secrets referenced from
the relation application databag.

This deliberately avoids the newer generic ``data_platform_libs`` v1 data
contract, which is not yet understood by the ``postgresql-k8s`` charms
currently published on the 14/stable and 16/stable channels.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Mapping, Optional

from ops import CharmBase, EventBase, EventSource, Object, ObjectEvents, Relation

logger = logging.getLogger(__name__)

RELATION_NAME = "database"
# Windmill stores all of its state in a single database named "windmill".
DATABASE_NAME = "windmill"


@dataclasses.dataclass(frozen=True)
class DatabaseInfo:
    """Resolved PostgreSQL connection details for the Windmill database."""

    endpoints: str  # host:port (read/write), comma-separated if multiple
    username: str
    password: str
    database: str
    tls_ca: Optional[str] = None

    @property
    def host(self) -> str:
        """Return the host portion of the first endpoint."""
        return self.endpoints.split(",", 1)[0].split(":", 1)[0]

    @property
    def port(self) -> str:
        """Return the port portion of the first endpoint (default 5432)."""
        ep = self.endpoints.split(",", 1)[0]
        if ":" in ep:
            return ep.split(":", 1)[1]
        return "5432"

    @property
    def connection_string(self) -> str:
        """Return a PostgreSQL connection string (DATABASE_URL)."""
        return f"postgresql://{self.username}:{self.password}@{self.endpoints}/{self.database}"


class DatabaseChangedEvent(EventBase):
    """Emitted when database credentials appear or change."""


class DatabaseEvents(ObjectEvents):
    """Events emitted by :class:`Database`."""

    changed = EventSource(DatabaseChangedEvent)


class Database(Object):
    """Manage the ``postgresql_client`` requirer relation and credentials."""

    on = DatabaseEvents()  # type: ignore[assignment]

    def __init__(self, charm: CharmBase) -> None:
        super().__init__(charm, RELATION_NAME)
        self._charm = charm
        self.framework.observe(charm.on[RELATION_NAME].relation_created, self._on_relation_created)
        self.framework.observe(charm.on[RELATION_NAME].relation_changed, self._on_relation_changed)
        self.framework.observe(charm.on.secret_changed, self._on_secret_changed)

    # ----------------------------------------------------------------- properties

    @property
    def relation(self) -> Optional[Relation]:
        """The active database relation, if any."""
        relations = self._charm.model.relations.get(RELATION_NAME, [])
        return relations[0] if relations else None

    def is_ready(self) -> bool:
        """Whether credentials have been provided by the database charm."""
        return self.get_info() is not None

    # --------------------------------------------------------------------- events

    def _on_relation_created(self, event) -> None:
        """Publish the database request to the provider (leader only)."""
        if not self._charm.unit.is_leader():
            return
        if event.relation.app is None:
            return
        local_app = self._charm.app
        data = event.relation.data[local_app]
        data["database"] = DATABASE_NAME
        data.setdefault("extensions", "[]")
        # Windmill's first migration creates a `windmill_admin` role WITH
        # BYPASSRLS, which requires superuser. Requesting SUPERUSER for the
        # relation user lets Windmill self-provision on managed PostgreSQL.
        data.setdefault("extra-user-roles", "SUPERUSER")
        data.setdefault("limit", "none")
        data.setdefault("read-only-endpoints", "")

    def _on_relation_changed(self, event) -> None:
        """Re-emit a changed event when the provider updates its databag."""
        if self.get_info() is not None:
            self.on.changed.emit()

    def _on_secret_changed(self, event) -> None:
        """Re-emit a changed event when a referenced secret is rotated."""
        # Secret labels from postgresql-k8s carry the relation id; only react
        # to those that look like ours to avoid spurious reconciles.
        label = getattr(event.secret, "label", None) or ""
        if RELATION_NAME in label or "database" in label or label == "":
            self.on.changed.emit()

    # ----------------------------------------------------------------- credential fetch

    def get_info(self) -> Optional[DatabaseInfo]:
        """Resolve and return the current database credentials, or ``None``."""
        relation = self.relation
        if relation is None or relation.app is None:
            return None
        provider_data = relation.data[relation.app]
        endpoints = provider_data.get("endpoints")
        if not endpoints:
            return None

        username = self._secret_field(provider_data, "secret-user", "username")
        password = self._secret_field(provider_data, "secret-password", "password")
        database = self._secret_field(provider_data, "secret-db", "database") or DATABASE_NAME
        tls_ca = self._secret_field(provider_data, "secret-tls-ca", "cert")

        # Fallback to legacy plaintext fields for older providers.
        if not username:
            username = provider_data.get("username")
        if not password:
            password = provider_data.get("password")
        if not database:
            database = provider_data.get("database") or DATABASE_NAME

        if not (username and password):
            return None
        return DatabaseInfo(
            endpoints=endpoints,
            username=username,
            password=password,
            database=database,
            tls_ca=tls_ca,
        )

    def _secret_field(
        self, provider_data: Mapping[str, str], uri_key: str, content_key: str
    ) -> Optional[str]:
        """Resolve a Juju secret referenced in the provider databag."""
        uri = provider_data.get(uri_key)
        if not uri:
            return None
        try:
            secret = self._charm.model.get_secret(id=uri)
            content = secret.get_content(refresh=True)
        except Exception:  # noqa: BLE001 - secret may not be granted/ready yet
            logger.debug("could not resolve secret %s", uri_key, exc_info=True)
            return None
        return content.get(content_key)
