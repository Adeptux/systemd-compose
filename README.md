# systemd-compose

`systemd-compose` is a first-draft Compose-like CLI that submits services as transient
systemd user units. Every service command is executed through `/usr/bin/bwrap`; the CLI
does not generate unit files, wrapper scripts, PID files, or temporary runtime state.

## Usage

```bash
uv run systemd-compose up --dry-run
uv run systemd-compose up
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
uv run systemd-compose logs
uv run systemd-compose logs web db
uv run systemd-compose logs web -- --since today --no-pager
uv run systemd-compose down
uv run systemd-compose down --remove-orphans
```

`up` creates missing units, recreates changed units, starts stopped unchanged units,
and skips unchanged running units. `start` and `restart` only operate on units that
were already created by `up`. `stop` stops units without resetting failed state;
`down` stops and resets failed transient units.

If services are removed from the compose file, their old transient units are treated
as orphans. `up` warns about project-owned orphans, and `up --remove-orphans` or
`down --remove-orphans` stops and resets them.

By default the CLI looks for `systemd-compose.yaml`, then `systemd-compose.yml`.

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

Example `systemd-compose.yaml`:

```yaml
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
    restart: on-failure
    depends_on:
      - db

  db:
    command: "postgres -D /var/lib/postgresql/data"
    volumes:
      - "/srv/postgres:/var/lib/postgresql/data"
```
