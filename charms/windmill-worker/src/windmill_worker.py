# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Workload helpers for the Windmill worker.

Pure functions with no charming concerns.
"""

from __future__ import annotations

import logging
from typing import Optional

from ops import pebble

logger = logging.getLogger(__name__)

CONTAINER_NAME = "windmill-worker"
SERVICE_NAME = "windmill-worker"
METRICS_PORT = 8001

# Tags the worker advertises by default for the "default" group.
DEFAULT_WORKER_TAGS = (
    "deno,python3,go,bash,powershell,dependency,flow,hub,bun,php,rust,"
    "ansible,csharp,java,nu,ruby,duckdb,other"
)
# Tags advertised by the native worker group.
NATIVE_WORKER_TAGS = "nativets,postgresql,mysql,mssql,graphql,snowflake,bigquery,oracledb"


def build_layer(
    *,
    database_url: str,
    worker_group: str = "default",
    native_mode: bool = False,
    worker_tags: str = "",
    sleep_queue: int = 50,
    keep_job_dir: bool = False,
    min_free_disk_space_mb: int = 15000,
    enable_pid_isolation: bool = False,
    disable_nsjail: bool = True,
    json_logs: bool = True,
    enable_metrics: bool = True,
    py_concurrent_downloads: int = 20,
    pip_local_dependencies: str = "",
    whitelist_envs: str = "",
    extra_env: Optional[dict[str, str]] = None,
) -> pebble.LayerDict:
    """Build the Pebble layer for a Windmill worker."""
    if not worker_tags:
        worker_tags = NATIVE_WORKER_TAGS if native_mode else DEFAULT_WORKER_TAGS

    environment: dict[str, str] = {
        "DATABASE_URL": database_url,
        "MODE": "worker",
        "WORKER_GROUP": worker_group,
        "WORKER_TAGS": worker_tags,
        "SLEEP_QUEUE": str(sleep_queue),
        "KEEP_JOB_DIR": "true" if keep_job_dir else "false",
        "MIN_FREE_DISK_SPACE_MB": str(min_free_disk_space_mb),
        "JSON_FMT": "true" if json_logs else "false",
        "DISABLE_NSJAIL": "true" if disable_nsjail else "false",
        "PY_CONCURRENT_DOWNLOADS": str(py_concurrent_downloads),
        "QUIET": "false",
    }
    if native_mode:
        environment["NATIVE_MODE"] = "true"
    if enable_pid_isolation:
        environment["ENABLE_UNSHARE_PID"] = "true"
        environment["FAVOR_UNSHARE_PID"] = "true"
    if enable_metrics:
        environment["METRICS_ADDR"] = f":{METRICS_PORT}"
    if pip_local_dependencies:
        environment["PIP_LOCAL_DEPENDENCIES"] = pip_local_dependencies
    if whitelist_envs:
        environment["WHITELIST_ENVS"] = whitelist_envs
    if extra_env:
        environment.update(extra_env)

    services: dict[str, pebble.ServiceDict] = {
        SERVICE_NAME: {
            "override": "replace",
            "summary": f"Windmill worker (group={worker_group})",
            "startup": "enabled",
            "command": "/usr/local/bin/windmill",
            "environment": environment,
            "on-failure": "restart",
        }
    }
    # The worker has no HTTP health endpoint, so use an exec check that exits 0
    # while the worker process is alive. Pebble restarts the service if it dies.
    checks: dict[str, pebble.CheckDict] = {
        "worker-alive": {
            "override": "replace",
            "level": "alive",
            "threshold": 3,
            "exec": {"command": "pgrep -f '/usr/local/bin/windmill'"},
        }
    }
    return {"services": services, "checks": checks}
