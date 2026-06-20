# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the windmill-worker charm."""

from __future__ import annotations

import ops
import pytest
from ops import testing

import database
import windmill_worker
from charm import WindmillWorkerCharm

CONTAINER_NAME = "windmill-worker"
SERVICE_NAME = "windmill-worker"

FAKE_DB = database.DatabaseInfo(
    endpoints="db.example.com:5432",
    username="windmill_user",
    password="s3cr3t",
    database="windmill",
)

BASE_LAYER = ops.pebble.Layer(
    {
        "checks": {
            "worker-alive": {
                "override": "replace",
                "level": "alive",
                "threshold": 3,
                "exec": {"command": "pgrep -f '/usr/bin/windmill'"},
            }
        }
    }
)


def _state(**overrides) -> testing.State:
    container = testing.Container(
        CONTAINER_NAME,
        can_connect=True,
        layers={"base": BASE_LAYER},
        service_statuses={SERVICE_NAME: ops.pebble.ServiceStatus.INACTIVE},
        check_infos={
            testing.CheckInfo(
                "worker-alive",
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
    """The worker blocks when the database relation is absent."""
    monkeypatch.setattr(database.Database, "get_info", lambda self: None)
    ctx = testing.Context(WindmillWorkerCharm)
    state = _state()
    state_out = ctx.run(ctx.on.pebble_ready(state.get_container(CONTAINER_NAME)), state)
    assert state_out.unit_status == testing.BlockedStatus("add relation: database")


def test_pebble_ready_starts_service(monkeypatch: pytest.MonkeyPatch):
    """With a database, the worker service becomes active."""
    _patch_db(monkeypatch)
    ctx = testing.Context(WindmillWorkerCharm)
    state = _state()
    state_out = ctx.run(ctx.on.pebble_ready(state.get_container(CONTAINER_NAME)), state)
    container_out = state_out.get_container(CONTAINER_NAME)
    assert container_out.service_statuses[SERVICE_NAME] == ops.pebble.ServiceStatus.ACTIVE
    assert state_out.unit_status == testing.ActiveStatus("ready (group=default)")


def test_layer_has_worker_env(monkeypatch: pytest.MonkeyPatch):
    """The rendered layer sets MODE=worker and the configured group/tags."""
    _patch_db(monkeypatch)
    ctx = testing.Context(WindmillWorkerCharm)
    state = _state(config={"worker-group": "gpu", "worker-tags": "gpu,big"})
    ctx.run(ctx.on.config_changed(), state)
    # Re-fetch the planned layer from a pebble_ready run
    state2 = _state(config={"worker-group": "gpu", "worker-tags": "gpu,big"})
    state_out = ctx.run(ctx.on.pebble_ready(state2.get_container(CONTAINER_NAME)), state2)
    env = (
        state_out.get_container(CONTAINER_NAME)
        .layers["windmill-worker"]
        .services[SERVICE_NAME]
        .environment
    )
    assert env["MODE"] == "worker"
    assert env["WORKER_GROUP"] == "gpu"
    assert env["WORKER_TAGS"] == "gpu,big"
    assert env["DATABASE_URL"] == FAKE_DB.connection_string


def test_native_mode_uses_native_tags(monkeypatch: pytest.MonkeyPatch):
    """Native mode advertises the native tag set."""
    _patch_db(monkeypatch)
    ctx = testing.Context(WindmillWorkerCharm)
    state = _state(config={"worker-group": "native", "native-mode": True})
    state_out = ctx.run(ctx.on.pebble_ready(state.get_container(CONTAINER_NAME)), state)
    env = (
        state_out.get_container(CONTAINER_NAME)
        .layers["windmill-worker"]
        .services[SERVICE_NAME]
        .environment
    )
    assert env["NATIVE_MODE"] == "true"
    assert "nativets" in env["WORKER_TAGS"]


def test_restart_action(monkeypatch: pytest.MonkeyPatch):
    """The restart action restarts the worker service."""
    _patch_db(monkeypatch)
    ctx = testing.Context(WindmillWorkerCharm)
    state = _state()
    state = ctx.run(ctx.on.pebble_ready(state.get_container(CONTAINER_NAME)), state)
    ctx.run(ctx.on.action("restart"), state)
    assert ctx.action_results == {"result": "restarted"}


def test_build_layer_defaults():
    """build_layer produces a worker layer with sensible defaults."""
    layer = ops.pebble.Layer(
        windmill_worker.build_layer(database_url="postgresql://u:p@h:5432/windmill")
    )
    svc = layer.services[SERVICE_NAME]
    assert svc.environment["MODE"] == "worker"
    assert svc.environment["WORKER_GROUP"] == "default"
    assert "python3" in svc.environment["WORKER_TAGS"]
    assert svc.environment["DISABLE_NSJAIL"] == "true"


def test_build_layer_pid_isolation():
    """PID isolation flags are set when enabled."""
    layer = ops.pebble.Layer(
        windmill_worker.build_layer(
            database_url="postgresql://u:p@h:5432/windmill",
            enable_pid_isolation=True,
        )
    )
    env = layer.services[SERVICE_NAME].environment
    assert env["ENABLE_UNSHARE_PID"] == "true"
    assert env["FAVOR_UNSHARE_PID"] == "true"
