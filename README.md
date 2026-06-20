# Windmill Operator

[![CI](https://github.com/canonical/windmill-operator/actions/workflows/ci.yaml/badge.svg)](https://github.com/canonical/windmill-operator/actions/workflows/ci.yaml)
[![CharmHub](https://charmhub.io/windmill/badge.svg)](https://charmhub.io/windmill)

Juju charms for [Windmill](https://windmill.dev), the open-source developer
platform to build internal tools, scripts, flows, apps and scheduled jobs.

Windmill is composed of a **stateless server** (API, web frontend and
scheduler) and one or more **stateless workers** that pull jobs from a shared
PostgreSQL queue. All state lives in PostgreSQL, so the database is the only
component that must be backed up.

This repository contains two Kubernetes (sidecar) charms that map onto that
architecture:

| Charm | Role | State | Scales |
|-------|------|-------|--------|
| [`windmill`](charms/windmill) | Server: API + frontend + scheduler | Stateless | Horizontally, behind ingress |
| [`windmill-worker`](charms/windmill-worker) | Job execution | Stateless | Horizontally, independently |

## Deploy

```bash
# Add a Juju K8s model
juju add-model windmill

# The database — Windmill stores 100% of its state here
juju deploy postgresql-k8s --channel 14/stable --trust

# The Windmill server (Community Edition by default)
juju deploy ./charms/windmill/windmill_amd64.charm windmill --resource \
    windmill-image=ghcr.io/windmill-labs/windmill:main

# Workers (scale to taste; ~1 worker per vCPU)
juju deploy ./charms/windmill-worker/windmill-worker_amd64.charm windmill-worker \
    --resource windmill-worker-image=ghcr.io/windmill-labs/windmill:main
juju scale-application windmill-worker 3

# Wire it up: both server and workers share the same database queue
juju integrate postgresql-k8s:database windmill:database
juju integrate postgresql-k8s:database windmill-worker:database

# External access (TLS termination) via traefik
juju deploy traefik-k8s --trust
juju integrate windmill:ingress traefik-k8s:ingress

# Observability (optional) — the Charmed OpenTelemetry / Loki / Prometheus stack
juju deploy cos-lite --trust   # or individual charms
juju integrate windmill:logging        loki-k8s:logging
juju integrate windmill:metrics-endpoint prometheus-k8s:metrics-endpoint
juju integrate windmill:tracing        tempo-coordinator-k8s:tracing
```

After deployment, visit the traefik URL and sign in with
`admin@windmill.dev` / `changeme`, then rotate the admin password:

```bash
juju config windmill superadmin-secret="$(openssl rand -hex 32)"
juju run windmill/0 set-admin-password
```

## Day-2 operations

- **Backups**: Windmill's state is entirely in PostgreSQL. Back up via the
  `postgresql-k8s` charm's `backup`/`restore` actions — see
  [`charms/windmill/README.md`](charms/windmill/README.md) for details.
- **Scaling**: `juju scale-application windmill N` and
  `juju scale-application windmill-worker N`. Workers scale independently.
- **Upgrades**: update the OCI image resource and `juju run <unit> restart`.
- **Secret rotation**: rotate `superadmin-secret`, then
  `juju run windmill/0 set-admin-password`. Rotate DB credentials by
  re-adding the database relation.

## Observability

Both charms integrate with the Canonical Observability Stack:

- **Metrics** — `provides: metrics-endpoint` (`prometheus_scrape`). Windmill
  exposes Prometheus metrics on port 8001 when `enable-metrics=true`
  (Enterprise Edition feature; Community Edition does not open the port).
- **Logs** — `requires: logging` (`loki_push_api`). All Pebble workload logs
  are forwarded to Loki via the `LogForwarder` library.
- **Traces** — `requires: tracing` (`tracing`). The OTLP endpoint from Tempo
  is exposed to job scripts via standard `OTEL_*` environment variables.

## Repository layout

```
charms/
  windmill/            # server charm
  windmill-worker/     # worker charm
.github/workflows/     # CI
```

Each charm is built with [charmcraft](https://documentation.ubuntu.com/charmcraft)
and the [ops](https://documentation.ubuntu.com/ops) framework, and uses
[`uv`](https://docs.astral.sh/uv) for dependency management.

## Development

```bash
# From a charm directory, e.g. charms/windmill
uv sync
PYTHONPATH=src:lib uv run --group unit pytest tests/unit
uv run --group lint ruff check src tests
uv run --group lint pyright
charmcraft pack
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for more.

## License

The charm code is licensed under the [Apache 2.0 License](LICENSE).
Windmill itself is licensed under the AGPLv3 — see the
[upstream project](https://github.com/windmill-labs/windmill).
