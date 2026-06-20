# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the windmill-worker charm."""

import logging
import pathlib

import jubilant
import yaml

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(pathlib.Path("charmcraft.yaml").read_text())

WINDMILL_IMAGE = METADATA["resources"]["windmill-worker-image"]["upstream-source"]


def test_deploy_blocks_without_database(charm: pathlib.Path, juju: jubilant.Juju):
    """The worker deploys and blocks until a database relation is added."""
    juju.deploy(
        charm.resolve(),
        app="windmill-worker",
        resources={"windmill-worker-image": WINDMILL_IMAGE},
    )

    def _blocked(status):
        return (
            status.apps["windmill-worker"].units["windmill-worker/0"].workload_status.status
            == "blocked"
        )

    juju.wait(_blocked, timeout=300)


def test_with_database(charm: pathlib.Path, juju: jubilant.Juju):
    """Relate to postgresql and wait for active/idle."""
    juju.deploy("postgresql-k8s", app="postgresql", channel="14/stable", trust=True)
    juju.integrate("postgresql:database", "windmill-worker:database")
    juju.wait(jubilant.all_active, timeout=900)


def test_restart_action(juju: jubilant.Juju):
    """The restart action completes successfully."""
    result = juju.run("windmill-worker/0", "restart")
    assert result.return_code == 0
