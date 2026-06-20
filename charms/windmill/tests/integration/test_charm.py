# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the windmill (server) charm."""

import logging
import pathlib

import jubilant
import yaml

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(pathlib.Path("charmcraft.yaml").read_text())

WINDMILL_IMAGE = METADATA["resources"]["windmill-image"]["upstream-source"]


def test_deploy_blocks_without_database(charm: pathlib.Path, juju: jubilant.Juju):
    """The charm deploys and blocks until a database relation is added."""
    juju.deploy(charm.resolve(), app="windmill", resources={"windmill-image": WINDMILL_IMAGE})

    def _blocked(status):
        return status.apps["windmill"].units["windmill/0"].workload_status.status == "blocked"

    juju.wait(_blocked, timeout=300)


def test_full_stack(charm: pathlib.Path, juju: jubilant.Juju):
    """Deploy postgresql + windmill and wait for active/idle."""
    juju.deploy("postgresql-k8s", app="postgresql", channel="14/stable", trust=True)
    juju.integrate("postgresql:database", "windmill:database")
    juju.wait(jubilant.all_active, timeout=900)


def test_restart_action(juju: jubilant.Juju):
    """The restart action completes successfully."""
    result = juju.run("windmill/0", "restart")
    assert result.return_code == 0


def test_pre_backup_action(juju: jubilant.Juju):
    """The pre-backup action reports the instance healthy."""
    result = juju.run("windmill/0", "pre-backup")
    assert result.return_code == 0
