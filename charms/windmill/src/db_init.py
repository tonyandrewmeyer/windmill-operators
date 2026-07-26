# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Database initialisation helpers for Windmill on managed PostgreSQL.

Windmill's first migration creates a ``windmill_admin`` role ``WITH
BYPASSRLS``. On managed PostgreSQL where the application user is not a
superuser (e.g. the Charmed ``postgresql-k8s`` charm), that migration fails
with ``role "windmill_admin" does not exist``.

These helpers implement the equivalent of Windmill's
``init-db-as-superuser.sql``: they create the required roles with a superuser
connection and grant them to the Windmill application user, so that the
subsequent migration succeeds.
"""

from __future__ import annotations

import logging
import urllib.parse

logger = logging.getLogger(__name__)

# The roles Windmill expects. See:
# https://www.windmill.dev/docs/integrations/postgresql#use-a-managed-postgres
ADMIN_ROLE = "windmill_admin"
USER_ROLE = "windmill_user"


def init_database(*, superuser_url: str, app_username: str) -> dict[str, str]:
    """Create Windmill's roles and grant them to the application user.

    Args:
        superuser_url: A PostgreSQL superuser connection string.
        app_username: The Windmill application username (from the relation)
            that will be granted membership of the roles.

    Returns:
        A summary of what was done, suitable for action results.

    Raises:
        InitDbError: if the superuser connection or SQL execution fails.
    """
    import pg8000  # imported lazily so unit tests don't need a live DB

    statements = [
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ADMIN_ROLE}') THEN CREATE ROLE {ADMIN_ROLE} WITH BYPASSRLS; END IF; END $$;",  # noqa: E501
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{USER_ROLE}') THEN CREATE ROLE {USER_ROLE}; END IF; END $$;",  # noqa: E501
        # Grant membership to the application user (idempotent enough; ignore
        # duplicate-grant errors by wrapping in a DO block).
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member WHERE r.rolname = '{ADMIN_ROLE}' AND u.rolname = '{app_username}') THEN GRANT {ADMIN_ROLE} TO \"{app_username}\"; END IF; END $$;",  # noqa: E501
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member WHERE r.rolname = '{USER_ROLE}' AND u.rolname = '{app_username}') THEN GRANT {USER_ROLE} TO \"{app_username}\"; END IF; END $$;",  # noqa: E501
        f"GRANT USAGE ON SCHEMA public TO {USER_ROLE};",
        f"GRANT USAGE ON SCHEMA public TO {ADMIN_ROLE};",
    ]

    try:
        conn = pg8000.connect(**_parse_dsn(superuser_url), timeout=15)  # type: ignore[call-overload]
    except Exception as exc:
        raise InitDbError(f"could not connect as superuser: {exc}") from exc

    try:
        conn.autocommit = True
        cur = conn.cursor()
        for stmt in statements:
            cur.execute(stmt)
        cur.close()
    except Exception as exc:
        raise InitDbError(f"failed to initialise database: {exc}") from exc
    finally:
        try:
            conn.close()
        except Exception:
            logger.debug("error closing superuser connection", exc_info=True)

    return {
        "result": "initialised",
        "roles": f"{ADMIN_ROLE}, {USER_ROLE}",
        "granted-to": app_username,
    }


class InitDbError(Exception):
    """Raised when database initialisation fails."""


def _parse_dsn(dsn: str) -> dict[str, str | int]:
    """Parse a ``postgresql://`` DSN into pg8000 connect kwargs."""
    parsed = urllib.parse.urlparse(dsn)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise InitDbError(f"unsupported DSN scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise InitDbError("DSN is missing a host")
    return {
        "user": urllib.parse.unquote(parsed.username or "postgres"),
        "password": urllib.parse.unquote(parsed.password or ""),
        "host": parsed.hostname,
        "port": int(parsed.port) if parsed.port else 5432,
        "database": parsed.path.lstrip("/") or "postgres",
        "ssl_context": False,
    }
