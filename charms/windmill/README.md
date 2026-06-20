# Windmill Charm

[CharmHub](https://charmhub.io/windmill) ·
[Windmill](https://windmill.dev) ·
[Source](https://github.com/windmill-labs/windmill)

A Juju **Kubernetes** charm that deploys and operates the
[Windmill](https://windmill.dev) **server** — the stateless API, web frontend
and job scheduler.

Windmill workers (which execute jobs) are deployed separately via the
[`windmill-worker`](../windmill-worker) charm. Both the server and the workers
are stateless; **all state lives in PostgreSQL**, so back up the database to
back up the instance.

## Relations

| Name | Interface | Direction | Purpose |
|------|-----------|-----------|---------|
| `database` | `postgresql_client` | requires | PostgreSQL — required; holds all state |
| `ingress` | `ingress` | requires | External HTTP access via `traefik-k8s` |
| `metrics-endpoint` | `prometheus_scrape` | provides | Prometheus metrics scraping |
| `logging` | `loki_push_api` | requires | Forward workload logs to Loki |
| `tracing` | `tracing` | requires | OTLP endpoint for job traces (Tempo) |

> **Database compatibility**: the charm uses the v1 `postgresql_client` data
> contract from `data_platform_libs`. Relate it to a recent
> `postgresql-k8s` (14/stable or 16/stable).

## Containers & storage

- **`windmill`** — the Windmill server OCI image
  (`ghcr.io/windmill-labs/windmill:main`, Community Edition; use
  `ghcr.io/windmill-labs/windmill-ee:main` for Enterprise Edition).
- **`logs`** filesystem storage — service logs at `/tmp/windmill/logs`.

The server listens on port **8000** (API + frontend) and, when
`enable-smtp-trigger=true`, on port **2525** (inbound SMTP for email-triggered
flows). Prometheus metrics are exposed on port **8001** (Enterprise Edition).

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `base-url` | `""` | Public base URL; derived from ingress when empty |
| `json-logs` | `true` | Emit structured JSON logs (`JSON_FMT`) |
| `db-connections` | `50` | PostgreSQL pool size (`DATABASE_CONNECTIONS`) |
| `zombie-job-timeout` | `30` | Seconds before a job is declared zombie |
| `restart-zombie-jobs` | `true` | Restart rather than fail zombie jobs |
| `enable-metrics` | `true` | Expose Prometheus metrics on :8001 (EE) |
| `enable-smtp-trigger` | `false` | Start the inbound SMTP listener on :2525 |
| `superadmin-secret` | `""` | Bearer token for the virtual superadmin |
| `ca-cert` | `""` | PEM CA cert to trust for outbound TLS |

See `charmcraft.yaml` for the full, authoritative list.

## Actions

| Action | Description |
|--------|-------------|
| `restart` | Restart the Windmill server workload |
| `init-db` | Create the `windmill_admin`/`windmill_user` PostgreSQL roles Windmill's migrations require (see *Initialising the database* below) |
| `set-admin-password` | Rotate the `admin@windmill.dev` password (requires `superadmin-secret`); returns and stores the new password as a Juju secret |
| `pre-backup` | Confirm the instance is healthy before a DB backup |
| `post-restore` | Reconcile the workload after a DB restore (restart) |

## Deploy

```bash
juju deploy postgresql-k8s --channel 14/stable --trust
juju deploy windmill --resource \
    windmill-image=ghcr.io/windmill-labs/windmill:main
juju integrate postgresql-k8s:database windmill:database

juju deploy traefik-k8s --trust
juju integrate windmill:ingress traefik-k8s:ingress
```

Then deploy workers and relate them to the same database (see the
[`windmill-worker`](../windmill-worker) README).

## Day-2 operations

### First login

Visit the ingress URL and sign in with `admin@windmill.dev` / `changeme`,
then rotate the password:

```bash
juju config windmill superadmin-secret="$(openssl rand -hex 32)"
juju run windmill/0 set-admin-password
```

The new password is returned in the action output and stored as a Juju
secret labelled `admin-password`.

### Initialising the database (required on managed PostgreSQL)

Windmill's first migration creates a `windmill_admin` role `WITH BYPASSRLS`,
which requires a PostgreSQL superuser. On managed PostgreSQL — including the
Charmed `postgresql-k8s` charm, where the relation user is **not** a
superuser — the server will crash-loop with
`role "windmill_admin" does not exist` and the unit will wait with a message
pointing you here. Initialise the database once after relating PostgreSQL:

```bash
# Charmed postgresql-k8s: the superuser is `operator`; its password is in the
# charm's `database-peers.<app>.app` Juju secret (operator-password field).
juju run windmill/0 init-db \
    superuser-url="postgresql://operator:<operator-password>@postgresql-primary:5432/postgres"

# External/managed Postgres (RDS, Cloud SQL, …): use your superuser DSN.
juju run windmill/0 init-db \
    superuser-url="postgresql://postgres:<password>@<host>:5432/postgres"
```

The action creates the roles idempotently, grants them to the relation user,
and restarts the server so migrations re-run. After it, the unit goes active.

### Backups & restores

Windmill's state is entirely in PostgreSQL — there is nothing else to back up.

```bash
# Take a backup via the database charm
juju run postgresql-k8s/0 backup

# Restore
juju run postgresql-k8s/0 restore
# Then reconcile the server with the restored database:
juju run windmill/0 post-restore
```

For disaster recovery of the *code* (scripts, flows, apps), use Windmill's
`wmill sync push/pull` against a Git repository.

### Scaling & upgrades

```bash
juju scale-application windmill 2          # horizontal scale, behind ingress
# Upgrade: point the resource at a new image tag and restart
juju refresh windmill --resource \
    windmill-image=ghcr.io/windmill-labs/windmill:vX.Y.Z
juju run windmill/0 restart
```

### High availability

Run multiple server units behind traefik and multiple workers, all pointing
at a highly-available PostgreSQL (e.g. `postgresql-k8s` with multiple units,
or a managed HA Postgres). Servers and workers are stateless, so failover is
simply a matter of promoting a DB replica and repointing the relation.

## Observability

```bash
juju integrate windmill:logging        loki-k8s:logging
juju integrate windmill:metrics-endpoint prometheus-k8s:metrics-endpoint
juju integrate windmill:tracing        tempo-coordinator-k8s:tracing
```

- **Metrics** are an Enterprise Edition feature (`METRICS_ADDR`); on Community
  Edition the metrics port is not opened.
- **Logs** are forwarded via Pebble log forwarding to Loki.
- **Traces**: the OTLP endpoint is exposed to job scripts via `OTEL_*` env
  vars so they can export spans to Tempo. Server-level tracing requires
  configuring OpenTelemetry in Windmill's instance settings (EE).

## Security considerations

- Rotate `superadmin-secret` regularly; it grants full admin access.
- Encrypt PostgreSQL at rest (the database charm supports TLS via
  `tls-certificates`).
- Workers (not this charm) execute untrusted user code — see the
  `windmill-worker` README for sandboxing options.

## License

Apache 2.0; Windmill itself is AGPLv3.
