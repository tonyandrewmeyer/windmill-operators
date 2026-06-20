# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Workload helpers for the Windmill server.

Pure functions and a small HTTP client that have no charming concerns, so they
can be unit-tested in isolation.
"""

from __future__ import annotations

import json
import logging
import secrets
import string
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from ops import pebble

logger = logging.getLogger(__name__)

CONTAINER_NAME = "windmill"
SERVICE_NAME = "windmill-server"
SERVER_PORT = 8000
METRICS_PORT = 8001
SMTP_PORT = 2525
HEALTH_PATH = "/health"
METRICS_PATH = "/metrics"

# Default admin user created by Windmill on first run.
DEFAULT_ADMIN_EMAIL = "admin@windmill.dev"


def build_layer(
    *,
    database_url: str,
    mode: str = "server",
    base_url: str = "",
    json_logs: bool = True,
    db_connections: int = 50,
    zombie_job_timeout: int = 30,
    restart_zombie_jobs: bool = True,
    enable_metrics: bool = True,
    enable_smtp: bool = False,
    superadmin_secret: str = "",
    ca_cert: str = "",
    extra_env: Optional[dict[str, str]] = None,
) -> pebble.LayerDict:
    """Build the Pebble layer for the Windmill server/worker workload.

    The Windmill binary is the container entrypoint; the ``MODE`` environment
    variable selects server vs worker behaviour.
    """
    environment: dict[str, str] = {
        "DATABASE_URL": database_url,
        "MODE": mode,
        "JSON_FMT": "true" if json_logs else "false",
        "DATABASE_CONNECTIONS": str(db_connections),
        "ZOMBIE_JOB_TIMEOUT": str(zombie_job_timeout),
        "RESTART_ZOMBIE_JOBS": "true" if restart_zombie_jobs else "false",
        "SILENCE_HEALTH_LOGS": "true",
    }
    if base_url:
        environment["BASE_URL"] = base_url
    if enable_metrics:
        # Expose Prometheus metrics on METRICS_PORT (Enterprise Edition feature).
        environment["METRICS_ADDR"] = f":{METRICS_PORT}"
    if enable_smtp:
        environment["ENABLE_SMTP"] = "true"
        environment["SMTP_PORT"] = str(SMTP_PORT)
    if superadmin_secret:
        environment["SUPERADMIN_SECRET"] = superadmin_secret
    if ca_cert:
        environment["RUN_UPDATE_CA_CERTIFICATE_AT_START"] = "true"
    if extra_env:
        environment.update(extra_env)

    services: dict[str, pebble.ServiceDict] = {
        SERVICE_NAME: {
            "override": "replace",
            "summary": "Windmill server (API + frontend + scheduler)",
            "startup": "enabled",
            "command": "/usr/local/bin/windmill",
            "environment": environment,
            "on-failure": "restart",
        }
    }
    checks: dict[str, pebble.CheckDict] = {
        "http-ready": {
            "override": "replace",
            "level": "ready",
            "threshold": 3,
            "http": {"url": f"http://localhost:{SERVER_PORT}{HEALTH_PATH}"},
        },
        "http-alive": {
            "override": "replace",
            "level": "alive",
            "threshold": 3,
            "http": {"url": f"http://localhost:{SERVER_PORT}{HEALTH_PATH}"},
        },
    }
    return {"services": services, "checks": checks}


def generate_password(length: int = 24) -> str:
    """Generate a strong random password suitable for Windmill users."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    # Ensure at least one of each class for complexity-policy-friendly deployments.
    guaranteed = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*"),
    ]
    rest = [secrets.choice(alphabet) for _ in range(length - len(guaranteed))]
    pwd = guaranteed + rest
    secrets.SystemRandom().shuffle(pwd)
    return "".join(pwd)


class WindmillAdminError(Exception):
    """Raised when an admin API call to Windmill fails."""


class WindmillAdminClient:
    """Tiny HTTP client for the Windmill admin/superadmin API.

    Only the handful of endpoints the charm needs are implemented. The client
    authenticates with the ``SUPERADMIN_SECRET`` bearer token.
    """

    def __init__(self, base_url: str, superadmin_secret: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = superadmin_secret
        self._timeout = timeout

    def _request(
        self, method: str, path: str, body: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        url = urllib.parse.urljoin(self._base_url + "/", path.lstrip("/"))
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                payload = resp.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise WindmillAdminError(f"{method} {url} failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise WindmillAdminError(f"{method} {url} failed: {exc.reason}") from exc
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {"raw": payload}

    def is_healthy(self) -> bool:
        """Return whether the server reports as healthy."""
        try:
            self._request("GET", HEALTH_PATH)
            return True
        except WindmillAdminError:
            return False

    def set_user_password(self, email: str, password: str) -> None:
        """Set the password of an existing user (requires superadmin)."""
        # Windmill superadmin endpoint to update a user's password.
        self._request(
            "POST",
            "/api/users/set_password",
            body={"email": email, "password": password},
        )
