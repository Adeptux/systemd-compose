# systemd-compose

`systemd-compose` is a first-draft Compose-like CLI that submits services as transient
systemd user units. Every service command is executed through `/usr/bin/bwrap`; the CLI
does not generate unit files, wrapper scripts, PID files, or temporary runtime state.

## Usage

```bash
uv run systemd-compose up --dry-run
uv run systemd-compose up
uv run systemd-compose up web db
uv run systemd-compose up --remove-orphans
uv run systemd-compose stop
uv run systemd-compose start
uv run systemd-compose restart web
uv run systemd-compose status
uv run systemd-compose status web
uv run systemd-compose ps
uv run systemd-compose stats
uv run systemd-compose stats --interval 2
uv run systemd-compose stats --no-stream
uv run systemd-compose health
uv run systemd-compose health web
uv run systemd-compose logs
uv run systemd-compose logs --follow
uv run systemd-compose logs --health
uv run systemd-compose logs --health static -- --since today
uv run systemd-compose logs web db
uv run systemd-compose logs web -- --since today --no-pager
uv run systemd-compose down
uv run systemd-compose down web db
uv run systemd-compose down --remove-orphans
```

`up` creates missing units, recreates changed units, starts stopped unchanged units,
and skips unchanged running units. When service names are provided, `up` only
operates on those services. `start` and `restart` only operate on units that were
already created by `up`. `stop` stops units without resetting failed state;
`down` stops and resets failed transient units, optionally limited to named
services. `logs` prints existing journal output by default; use `logs --follow`
to stream new entries. Use `logs --health` to inspect healthcheck sidecar logs.

If services are removed from the compose file, their old transient units are treated
as orphans. `up` warns about project-owned orphans, and `up --remove-orphans` or
`down --remove-orphans` stops and resets them.

By default the CLI looks for `systemd-compose.yaml`, then `systemd-compose.yml`.
Set top-level `name:` in the compose file to choose the project name used as the
systemd unit prefix. `-p/--project-name` overrides the file value, and the
current directory name is still used when neither is set.

Before starting or restarting a service, missing host-side volume paths are
created as directories, similar to Docker Compose bind mount behavior. Dry runs
do not create directories.

Units are submitted to the per-user systemd manager. If inspecting them manually,
use `systemctl --user status <unit>.service`, not plain `systemctl status`.
Service stdout and stderr are tagged with the generated unit name in the journal,
for example `systemd-compose-web`, rather than the `bwrap` process name.
`ps` shows a one-shot service table from systemd unit state. `stats` refreshes
resource usage every second by default; tune that with `--interval`, or use
`stats --no-stream` for a single sampled snapshot. Created units enable systemd
CPU, memory, task, I/O, and IP accounting for these stats. When systemd returns
empty accounting values, `stats` falls back to cgroup v2 files where available,
then to `/proc` RSS and process I/O counters for user-service environments that
do not delegate memory or I/O controllers. Resource fields show `-` when the
corresponding metric is unavailable; network I/O is commonly unavailable for
ordinary user services.

Services can set Docker Compose-like resource limits. `mem_limit` maps to
systemd `MemoryMax=`, `cpus` maps to `CPUQuota=`, and `pids_limit` maps to
`TasksMax=`. For `cpus`, `1.0` means one full CPU, `0.5` means half of one CPU,
and values above `1.0` can use more than one CPU on multicore systems.

NVIDIA GPU workloads do not need special syntax in the current host-binary
sandbox model. The base `bwrap` profile exposes host `/dev`, `/usr`, `/etc`,
`/proc`, and `/sys`, so host NVIDIA device nodes and libraries are available
when the host system exposes them. On WSL, `nvidia-smi` is commonly available as
`/usr/lib/wsl/lib/nvidia-smi` rather than `nvidia-smi` on the default service
`PATH`.

Services can also define Docker Compose-like healthchecks. Healthchecks are run
as companion transient systemd timer/service units named
`<project>-<service>-health.timer` and `<project>-<service>-health.service`.
`ps` includes the latest health state inline. `health` reports `healthy` after
the latest successful probe, `unhealthy` after the latest failed probe, and
`starting` before a probe result exists. `retries` is parsed and included in
change detection, but this daemonless version does not yet track consecutive
failures like Docker.

Example `systemd-compose.yaml`:

```yaml
name: demo

services:
  web:
    command: "python -m http.server 8000"
    working_dir: /app
    environment:
      PORT: "8000"
    volumes:
      - "/srv/site:/app:ro"
    tmpfs:
      - "/var/lib/nginx"
    mem_limit: 512m
    cpus: 0.5
    pids_limit: 128
    healthcheck:
      test: ["CMD", "curl", "-f", "http://127.0.0.1:8000"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    restart: on-failure
    depends_on:
      - db

  db:
    command: "postgres -D /var/lib/postgresql/data"
    volumes:
      - "/srv/postgres:/var/lib/postgresql/data"
```

## Nuitka Linux Binary

Build a standalone x86_64 Linux executable with Nuitka:

```bash
scripts/build-nuitka-linux.sh
```

The Docker builder uses the `manylinux2014_x86_64` image so the binary is built
against an older glibc baseline than most current distros. Outputs are written to
`dist/nuitka/`, including the executable, checksum, smoke-test output, builder
glibc version, and referenced glibc symbols.

See `packaging/nuitka/README.md` for details and build knobs.
