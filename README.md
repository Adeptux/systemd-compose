# systemd-compose

`systemd-compose` is an early Compose-like CLI for running services as systemd
units. By default, `up` uses transient per-user units. Projects can also be
installed as persistent user or system units so they survive reboot. Service
commands are executed through `/usr/bin/bwrap`; the CLI does not generate
wrapper scripts, PID files, or temporary runtime state.

## Quick Start

Create a small `systemd-compose.yaml`:

```yaml
name: demo

services:
  web:
    command: "python -m http.server 8000 --bind 127.0.0.1"
    working_dir: /srv/site
    volumes:
      - "/tmp/systemd-compose-demo/site:/srv/site:ro"
```

Then run it:

```bash
mkdir -p /tmp/systemd-compose-demo/site
printf 'hello from systemd-compose\n' > /tmp/systemd-compose-demo/site/index.txt
uv run systemd-compose up --dry-run
uv run systemd-compose up
uv run systemd-compose ps
uv run systemd-compose logs --follow web
uv run systemd-compose down
```

## Example Compose File

By default, the CLI looks for `systemd-compose.yaml`, then
`systemd-compose.yml`.

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

## Commands

### Create and Update Units

```bash
uv run systemd-compose up --dry-run
uv run systemd-compose up
uv run systemd-compose up web db
uv run systemd-compose up --remove-orphans
```

`up` submits missing units and keeps existing units aligned with the compose
file:

- Creates units that do not exist yet.
- Recreates units whose service definition changed.
- Starts stopped units whose definition is unchanged.
- Skips unchanged units that are already running.
- Operates only on the named services when service names are provided.
- Warns when project-owned units are no longer present in the compose file.
- Stops and resets orphan units when `--remove-orphans` is provided.

Use `--dry-run` to print the generated `systemd-run` commands without creating
units or host-side volume directories.

When a project has been installed, `up` manages the installed unit files instead
of transient units:

- Added services get new persistent unit files and are started through the
  project target.
- Changed services have their unit files rewritten and are restarted.
- Removed services are reported as installed orphans.
- `up --remove-orphans` stops and removes installed orphan units.

### Install Persistent Units

```bash
uv run systemd-compose install
uv run systemd-compose install --now
sudo systemd-compose install --system --now
uv run systemd-compose uninstall
uv run systemd-compose uninstall --now
```

`install` writes persistent unit files and enables the generated project target.
By default, user units are written to `~/.config/systemd/user/` and the project
target is enabled under `default.target`.

Use `install --system` as root to write system units to `/etc/systemd/system/`
and enable the project target under `multi-user.target`.

Lifecycle meaning:

- `install` makes the project survive reboot.
- `install --now` also starts the project after enabling it.
- `uninstall` disables and removes generated persistent units.
- `uninstall --now` stops the project before removing generated persistent
  units.

If you want user units to start at boot without an interactive login, enable
linger for the account:

```bash
loginctl enable-linger "$USER"
```

### Start, Stop, Restart, and Remove

```bash
uv run systemd-compose start
uv run systemd-compose restart web
uv run systemd-compose stop
uv run systemd-compose down
uv run systemd-compose down web db
uv run systemd-compose down --remove-orphans
```

Lifecycle commands have slightly different meanings:

- `start` starts units that were already created by `up`.
- `restart` restarts units that were already created by `up`.
- `stop` stops units without resetting failed state.
- `down` stops units and resets failed transient units.
- For installed projects, `start`, `stop`, `restart`, and `down` operate on the
  generated persistent project target or selected service units.
- `down web db` limits cleanup to the named services.
- `down --remove-orphans` also removes project-owned units that are no longer in
  the compose file.

### Inspect Services

```bash
uv run systemd-compose status
uv run systemd-compose status web
uv run systemd-compose ps
uv run systemd-compose stats
uv run systemd-compose stats --interval 2
uv run systemd-compose stats --no-stream
uv run systemd-compose health
uv run systemd-compose health web
```

Inspection commands:

- `status` shows `systemctl --user status` output for all services or the named
  service.
- `ps` prints a one-shot service table from systemd unit state.
- `stats` streams resource usage once per second by default.
- `stats --interval 2` changes the refresh interval.
- `stats --no-stream` prints a single sampled snapshot.
- `health` reports healthcheck state for all services or the named service.

When inspecting user units manually, use `systemctl --user`, not plain
`systemctl`. System installs use plain `systemctl`. For example:

```bash
systemctl --user status demo-web.service
```

### View Logs

```bash
uv run systemd-compose logs
uv run systemd-compose logs --follow
uv run systemd-compose logs web db
uv run systemd-compose logs web -- --since today --no-pager
uv run systemd-compose logs --health
uv run systemd-compose logs --health static -- --since today
```

Logging behavior:

- `logs` prints existing journal output by default.
- `logs --follow` streams new journal entries.
- Service names limit log output to those services.
- Arguments after `--` are passed through to `journalctl`.
- `logs --health` shows healthcheck sidecar logs.

Service stdout and stderr are tagged in the journal with the generated unit
name, such as `systemd-compose-web`, rather than the `bwrap` process name.

## Compose File Details

### Project Names

The project name is used as the systemd unit prefix.

Resolution order:

- `-p/--project-name`
- Top-level `name:` in the compose file
- Current directory name

### Services

Supported service fields include:

- `command`
- `working_dir`
- `environment`
- `volumes`
- `tmpfs`
- `depends_on`
- `restart`
- `mem_limit`
- `cpus`
- `pids_limit`
- `healthcheck`

Before starting or restarting a service, missing host-side volume paths are
created as directories, similar to Docker Compose bind mount behavior. Dry runs
do not create directories.

### Resource Limits

Docker Compose-like resource limits are mapped to systemd properties:

- `mem_limit` maps to `MemoryMax=`.
- `cpus` maps to `CPUQuota=`.
- `pids_limit` maps to `TasksMax=`.

For `cpus`, `1.0` means one full CPU, `0.5` means half of one CPU, and values
above `1.0` can use more than one CPU on multicore systems.

Created units enable systemd CPU, memory, task, I/O, and IP accounting so
`stats` can report resource usage.

When systemd returns empty accounting values, `stats` falls back to:

- cgroup v2 files, where available.
- `/proc` RSS and process I/O counters in user-service environments that do not
  delegate memory or I/O controllers.

Resource fields show `-` when the corresponding metric is unavailable. Network
I/O is commonly unavailable for ordinary user services.

### Healthchecks

Services can define Docker Compose-like healthchecks.

Healthchecks run as companion transient systemd timer/service units:

- `<project>-<service>-health.timer`
- `<project>-<service>-health.service`

Health state is reported in two places:

- `ps` includes the latest health state inline.
- `health` prints health state for all services or the named service.

States:

- `healthy` after the latest successful probe.
- `unhealthy` after the latest failed probe.
- `starting` before a probe result exists.

`retries` is parsed and included in change detection, but this daemonless
version does not yet track consecutive failures like Docker.

### Orphans

If services are removed from the compose file, their old transient units are
treated as orphans.

Orphan handling:

- `up` warns about project-owned orphans.
- `up --remove-orphans` stops and resets project-owned orphans.
- `down --remove-orphans` stops and resets project-owned orphans.

## Runtime Model

`systemd-compose` submits units to the per-user systemd manager. It is designed
around host binaries and a `bwrap` sandbox, not container images.

The base `bwrap` profile exposes host:

- `/dev`
- `/usr`
- `/etc`
- `/proc`
- `/sys`

Because this model uses host binaries, NVIDIA GPU workloads do not need special
syntax. Host NVIDIA device nodes and libraries are available when the host system
exposes them.

On WSL, `nvidia-smi` is commonly available as:

```text
/usr/lib/wsl/lib/nvidia-smi
```

rather than `nvidia-smi` on the default service `PATH`.

## Nuitka Linux Binary

Build a standalone x86_64 Linux executable with Nuitka:

```bash
scripts/build-nuitka-linux.sh
```

The Docker builder uses the `manylinux2014_x86_64` image so the binary is built
against an older glibc baseline than most current distros.

Outputs are written to `dist/nuitka/`:

- Executable
- Checksum
- Smoke-test output
- Builder glibc version
- Referenced glibc symbols

See `packaging/nuitka/README.md` for details and build knobs.
