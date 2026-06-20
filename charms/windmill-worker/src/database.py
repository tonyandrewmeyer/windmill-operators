# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""PostgreSQL client relation (postgresql_client interface) wrapper.

Wraps the generic :mod:`data_platform_libs` data-interfaces library so the
charm deals only with a simple :class:`DatabaseInfo` value object. The real
interface protocol (including Juju secret resolution of credentials) is
handled entirely by the upstream library.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Optional

from charms.data_platform_libs.v1.data_interfaces import (
    RequirerCommonModel,
    ResourceProviderModel,
    ResourceRequirerEventHandler,
)
from ops import Application, CharmBase, Relation

logger = logging.getLogger(__name__)

RELATION_NAME = "database"
# Windmill stores all of its state in a single database named "windmill".
DATABASE_NAME = "windmill"


@dataclasses.dataclass(frozen=True)
class DatabaseInfo:
    """Resolved PostgreSQL connection details for the Windmill database."""

    endpoints: str  # host:port (read/write)
    username: str
    password: str
    database: str
    tls_ca: Optional[str] = None

    @property
    def host(self) -> str:
        """Return the host portion of the endpoints."""
        return self.endpoints.split(",", 1)[0].split(":", 1)[0]

    @property
    def port(self) -> str:
        """Return the port portion of the endpoints (default 5432)."""
        ep = self.endpoints.split(",", 1)[0]
        if ":" in ep:
            return ep.split(":", 1)[1]
        return "5432"

    @property
    def connection_string(self) -> str:
        """Return a PostgreSQL connection string (DATABASE_URL)."""
        return f"postgresql://{self.username}:{self.password}@{self.endpoints}/{self.database}"


class Database:
    """Manage the ``postgresql_client`` requirer relation and credentials."""

    def __init__(self, charm: CharmBase) -> None:
        self._charm = charm
        self.interface: ResourceRequirerEventHandler = ResourceRequirerEventHandler(
            charm,
            relation_name=RELATION_NAME,
            requests=[RequirerCommonModel(resource=DATABASE_NAME)],
            response_model=ResourceProviderModel,
        )

    def on(self):  # noqa: ANN201 - thin accessor for the library's event source
        """Return the library's event source for observing."""
        return self.interface.on

    @property
    def relation(self) -> Optional[Relation]:
        """The active database relation, if any."""
        relations = self._charm.model.relations.get(RELATION_NAME, [])
        return relations[0] if relations else None

    def is_ready(self) -> bool:
        """Whether credentials have been provided by the database charm."""
        return self.get_info() is not None

    def get_info(self) -> Optional[DatabaseInfo]:
        """Resolve and return the current database credentials, or ``None``.

        Credentials are resolved from Juju secrets referenced in the relation
        databag by the upstream library.
        """
        relation = self.relation
        if relation is None or relation.app is None:
            return None
        try:
            model = self.interface.interface.build_model(
                relation_id=relation.id, component=relation.app
            )
        except Exception:  # noqa: BLE001 - relation not fully populated yet
            logger.debug("database relation not ready yet", exc_info=True)
            return None

        requests = getattr(model, "requests", []) or []
        if not requests:
            return None
        response = requests[0]
        endpoints = getattr(response, "endpoints", None)
        username = getattr(response, "username", None)
        password = getattr(response, "password", None)
        database = getattr(response, "resource", None) or DATABASE_NAME
        tls_ca = getattr(response, "tls_ca", None)
        if not (endpoints and username and password):
            return None
        return DatabaseInfo(
            endpoints=str(endpoints),
            username=str(username),
            password=str(password),
            database=str(database),
            tls_ca=str(tls_ca) if tls_ca else None,
        )


    @staticmethod
    def app_for(relation: Relation) -> Optional[Application]:
        """Return the remote application of a relation, defensively."""
        return relation.app
