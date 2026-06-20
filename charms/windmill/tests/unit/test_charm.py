# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the windmill (server) charm."""

from __future__ import annotations

import ops
import pytest
from ops import testing

import database
import windmill
from charm import WindmillCharm

CONTAINER_NAME = "windmill"
SERVICE_NAME = "windmill-server"

# A base layer present in the workload container before the charm reconciles.
# It declares the Pebble checks the charm relies on; the charm adds its own
# service layer on top at runtime.
BASE_LAYER = ops.pebble.Layer(
    {
        "checks": {
            "http-ready": {
                "override": "replace",
                "level": "ready",
                "threshold": 3,
                "http": {"url": f"http://localhost:{windmill.SERVER_PORT}/api/health"},
            },
            "http-alive": {
                "override": "replace",
                "level": "alive",
                "threshold": 3,
                "http": {"url": f"http://localhost:{windmill.SERVER_PORT}/api/health"},
            },
        }
    }
)

FAKE_DB = database.DatabaseInfo(
    endpoints="db.example.com:5432",
    username="windmill_user",
    password="s3cr3t",
    database="windmill",
)


def _state(**overrides) -> testing.State:
    container = testing.Container(
        CONTAINER_NAME,
        can_connect=True,
        layers={"base": BASE_LAYER},
        service_statuses={SERVICE_NAME: ops.pebble.ServiceStatus.INACTIVE},
        check_infos={
            testing.CheckInfo(
                "http-ready",
                level=ops.pebble.CheckLevel.READY,
                status=ops.pebble.CheckStatus.UP,
                startup=ops.pebble.CheckStartup.UNSET,
            ),
            testing.CheckInfo(
                "http-alive",
                level=ops.pebble.CheckLevel.ALIVE,
                status=ops.pebble.CheckStatus.UP,
                startup=ops.pebble.CheckStartup.UNSET,
            ),
        },
    )
    return testing.State(containers={container}, **overrides)


def _patch_db(monkeypatch: pytest.MonkeyPatch, info=FAKE_DB) -> None:
    monkeypatch.setattr(database.Database, "get_info", lambda self: info)


def test_blocked_without_database(monkeypatch: pytest.MonkeyPatch):
    """The charm blocks when the database relation is absent."""
    monkeypatch.setattr(database.Database, "get_info", lambda self: None)
    ctx = testing.Context(WindmillCharm)
    state_out = ctx.run(ctx.on.pebble_ready(_state().get_container(CONTAINER_NAME)), _state())
    assert state_out.unit_status == testing.BlockedStatus("add relation: database")


def test_pebble_ready_starts_service(monkeypatch: pytest.MonkeyPatch):
    """When the database is available the server service becomes active."""
    _patch_db(monkeypatch)
    ctx = testing.Context(WindmillCharm)
    container_in = _state().get_container(CONTAINER_NAME)
    state_out = ctx.run(ctx.on.pebble_ready(container_in), _state())
    container_out = state_out.get_container(CONTAINER_NAME)
    assert container_out.service_statuses[SERVICE_NAME] == ops.pebble.ServiceStatus.ACTIVE
    assert state_out.unit_status == testing.ActiveStatus("ready")


def test_layer_contains_required_environment(monkeypatch: pytest.MonkeyPatch):
    """The rendered Pebble layer exposes the required Windmill environment."""
    _patch_db(monkeypatch)
    ctx = testing.Context(WindmillCharm)
    container_in = _state().get_container(CONTAINER_NAME)
    state_out = ctx.run(ctx.on.pebble_ready(container_in), _state())
    container_out = state_out.get_container(CONTAINER_NAME)
    layer = container_out.layers["windmill"]
    svc = layer.services[SERVICE_NAME]
    env = svc.environment
    assert env["DATABASE_URL"] == FAKE_DB.connection_string
    assert env["MODE"] == "server"
    assert env["JSON_FMT"] == "true"
    assert env["METRICS_ADDR"] == f":{windmill.METRICS_PORT}"


def test_metrics_disabled_when_configured(monkeypatch: pytest.MonkeyPatch):
    """Disabling metrics removes METRICS_ADDR from the environment."""
    _patch_db(monkeypatch)
    ctx = testing.Context(WindmillCharm)
    state = _state(config={"enable-metrics": False, "json-logs": True})
    state_out = ctx.run(ctx.on.config_changed(), state)
    container_out = state_out.get_container(CONTAINER_NAME)
    env = container_out.layers["windmill"].services[SERVICE_NAME].environment
    assert "METRICS_ADDR" not in env


def test_restart_action(monkeypatch: pytest.MonkeyPatch):
    """The restart action restarts the Pebble service."""
    _patch_db(monkeypatch)
    ctx = testing.Context(WindmillCharm)
    state = _state()
    state = ctx.run(ctx.on.pebble_ready(state.get_container(CONTAINER_NAME)), state)
    ctx.run(ctx.on.action("restart"), state)
    assert ctx.action_results == {"result": "restarted"}


def test_set_admin_password_requires_token(monkeypatch: pytest.MonkeyPatch):
    """The set-admin-password action fails without superadmin-secret."""
    from ops._private.harness import ActionFailed

    _patch_db(monkeypatch)
    ctx = testing.Context(WindmillCharm)
    state = _state()
    with pytest.raises(ActionFailed) as exc:
        ctx.run(ctx.on.action("set-admin-password"), state)
    assert "superadmin-secret" in str(exc.value)


def test_build_layer_server_defaults():
    """build_layer produces a server layer with the expected defaults."""
    layer_dict = windmill.build_layer(database_url="postgresql://u:p@h:5432/windmill")
    layer = ops.pebble.Layer(layer_dict)
    svc = layer.services[SERVICE_NAME]
    assert svc.environment["MODE"] == "server"
    assert svc.environment["DATABASE_URL"] == "postgresql://u:p@h:5432/windmill"
    assert svc.environment["METRICS_ADDR"] == f":{windmill.METRICS_PORT}"
    assert "http-ready" in layer_dict["checks"]  # type: ignore[index]
    http_check = layer_dict["checks"]["http-ready"]  # type: ignore[index]
    assert http_check["http"]["url"].endswith("/api/health")  # type: ignore[index]


def test_generate_password_is_strong():
    """Generated passwords meet a minimum length and complexity."""
    pwd = windmill.generate_password(24)
    assert len(pwd) == 24
    assert any(c.islower() for c in pwd)
    assert any(c.isupper() for c in pwd)
    assert any(c.isdigit() for c in pwd)


def test_database_info_connection_string():
    """DatabaseInfo builds the correct connection string and host/port."""
    info = database.DatabaseInfo(
        endpoints="h:5432", username="u", password="p", database="windmill"
    )
    assert info.host == "h"
    assert info.port == "5432"
    assert info.connection_string == "postgresql://u:p@h:5432/windmill"
