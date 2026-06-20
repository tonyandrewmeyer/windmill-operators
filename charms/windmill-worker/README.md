# Windmill Worker Charm

[CharmHub](https://charmhub.io/windmill-worker) ·
[Windmill](https://windmill.dev)

A Juju **Kubernetes** charm that deploys and operates a
[Windmill](https://windmill.dev) **worker** — a stateless process that pulls
jobs from the shared Windmill PostgreSQL queue and executes them.

Workers do **not** communicate with the Windmill server; they only talk to
PostgreSQL. They therefore scale horizontally and independently of the server.
Deploy the server with the [`windmill`](../windmill) charm.

## Sizing

Rule of thumb: **one worker per ~1 vCPU and 1–2 GiB of memory**. Each worker
runs one job at a time. The native worker group runs 8 subworkers in process
for high-throughput native jobs.

## Relations

| Name | Interface | Direction | Purpose |
|------|-----------|-----------|---------|
| `database` | `postgresql_client` | requires | Shared job queue (required) |
| `metrics-endpoint` | `prometheus_scrape` | provides | Prometheus metrics (EE) |
| `logging` | `loki_push_api` | requires | Forward workload logs to Loki |
| `tracing` | `tracing` | requires | OTLP endpoint for job traces |

## Containers & storage

- **`windmill-worker`** — the Windmill OCI image (same image as the server;
  `MODE=worker` selects worker behaviour).
- **`cache`** storage — dependency cache at `/tmp/windmill/cache`.
- **`jobs`** storage — per-job working directories at `/tmp/windmill`.

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `worker-group` | `default` | Worker group (`WORKER_GROUP`) |
| `native-mode` | `false` | Native in-process worker (`NATIVE_MODE`) |
| `worker-tags` | `""` | Override advertised tags (`WORKER_TAGS`) |
| `sleep-queue` | `50` | ms between queue polls (`SLEEP_QUEUE`) |
| `keep-job-dir` | `false` | Keep job dirs after completion |
| `min-free-disk-space-mb` | `15000` | Disk threshold for self-declared unhealthy |
| `enable-pid-isolation` | `false` | PID namespace isolation (`ENABLE_UNSHARE_PID`) |
| `privileged` | `false` | Run the container privileged (for isolation) |
| `disable-nsjail` | `true` | Disable NSJAIL job sandboxing |
| `enable-metrics` | `true` | Expose Prometheus metrics on :8001 (EE) |
| `json-logs` | `true` | Structured JSON logs |
| `py-concurrent-downloads` | `20` | In-flight Python dependency downloads |
| `pip-local-dependencies` | `""` | Pre-installed Python packages |
| `whitelist-envs` | `""` | Env vars to pass through to jobs |

## Action

| Action | Description |
|--------|-------------|
| `restart` | Restart the worker workload |

## Deploy

```bash
juju deploy postgresql-k8s --channel 14/stable --trust
juju deploy windmill-worker --resource \
    windmill-worker-image=ghcr.io/windmill-labs/windmill:main
juju integrate postgresql-k8s:database windmill-worker:database
juju scale-application windmill-worker 3
```

Relate the workers to the **same** `postgresql-k8s` application as the
`windmill` server so they share the job queue.

## Worker groups & tags

Workers join a group via `worker-group`. The `default` group advertises tags
for all supported languages (`deno,python3,go,bash,...`). The `native` group
(with `native-mode=true`) advertises native tags
(`nativets,postgresql,mysql,...`) and runs 8 subworkers in process.

```bash
# A dedicated GPU worker group
juju deploy windmill-worker wm-worker-gpu --config worker-group=gpu \
    --config worker-tags=gpu,big
juju integrate postgresql-k8s:database wm-worker-gpu:database
```

## Sandboxing (running untrusted code)

Workers execute user-supplied code. **Isolation is disabled by default** —
enable it for untrusted workloads:

- **PID namespace isolation** — `enable-pid-isolation=true` sets
  `ENABLE_UNSHARE_PID`. This requires the workload container to run
  **privileged** (with host user namespaces enabled). Juju sidecar charms
  don't make the container privileged by themselves; provide a privileged
  security context via a custom rock or your cluster's pod-spec policy.
- **NSJAIL** — `disable-nsjail=false` enables NSJAIL per-job sandboxing
  (filesystem, network and resource limits). Also requires a privileged
  container, or the `SYS_ADMIN`, `SYS_RESOURCE`, `SETPCAP` capabilities with
  `allowPrivilegeEscalation`.

Until the container is privileged, leave both disabled (the defaults) —
otherwise the worker will fail to start. For defence in depth, deploy
untrusted workers in a separate Juju model with their own database replica
and network policies.

## Scaling & upgrades

```bash
juju scale-application windmill-worker 10

# Upgrade: point the resource at a new image tag and restart each unit
juju refresh windmill-worker --resource \
    windmill-worker-image=ghcr.io/windmill-labs/windmill:vX.Y.Z
juju run windmill-worker/0 restart
```

Graceful exit: in-flight jobs finish unless they exceed the container's
termination grace period. Queued/scheduled jobs resume automatically on the
remaining workers.

## License

Apache 2.0; Windmill itself is AGPLv3.
