# Contributing to the Windmill Operator

Thanks for your interest in improving the Windmill charms! This document
describes how to set up a development environment and the conventions to
follow.

## Prerequisites

- [Juju](https://juju.is) >= 3.6 with a bootstrapped K8s controller
- [charmcraft](https://documentation.ubuntu.com/charmcraft) >= 4
- [`uv`](https://docs.astral.sh/uv) (installed automatically by charmcraft)
- Python 3.12+

## Repository layout

```
charms/windmill/         # the server charm
charms/windmill-worker/  # the worker charm
```

Each charm is self-contained: it has its own `charmcraft.yaml`, `pyproject.toml`,
`uv.lock`, `src/`, `lib/`, and `tests/`.

## Setting up

```bash
cd charms/windmill        # or charms/windmill-worker
uv sync                   # create the venv and install all dependencies
```

## Running checks

From a charm directory:

```bash
# Format and lint
uv run --group lint ruff format src tests
uv run --group lint ruff check src tests
uv run --group lint pyright

# Unit tests
PYTHONPATH=src:lib uv run --group unit pytest tests/unit -v

# Or use tox
tox -e format
tox -e lint
tox -e unit
```

You can also run `tox` (with no arguments) to run the default environments
(`format`, `lint`, `unit`).

## Building a charm

```bash
charmcraft pack
```

This produces `<charm>_amd64.charm`, which you can deploy with
`juju deploy ./<charm>_amd64.charm`.

## Integration tests

Integration tests use [`jubilant`](https://github.com/canonical/jubilant) and
require a bootstrapped Juju K8s controller:

```bash
tox -e integration
```

## Charm libraries

Charm libraries live under `lib/charms/` and are declared in
`charmcraft.yaml` under `charm-libs`. To update them:

```bash
charmcraft fetch-libs
```

Never hand-edit files under `lib/`.

## Conventions

- Follow the [Juju charm style guide](https://documentation.ubuntu.com/ops/latest/reference/charm-style-guide/).
- Keep charm code (`src/`) free of charming concerns where possible — put
  workload-specific logic in a separate module (e.g. `windmill.py`).
- Every new feature or bug fix should include unit tests.
- Use clear, actionable unit statuses: `BlockedStatus` tells the operator
  exactly what to do; `WaitingStatus`/`MaintenanceStatus` are transient.
- Document new config options and actions in the charm's `README.md`.

## Commit messages

Use a short imperative subject line, optionally prefixed with the charm name,
e.g. `windmill: add SMTP trigger config`. Reference issues in the body.

## Pull requests

- Open a PR against `main`.
- CI must pass (lint, unit tests, build).
- Squash-merge on approval.
