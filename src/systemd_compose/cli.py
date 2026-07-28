from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from systemd_compose.builders import (
    build_health_systemd_run_command,
    build_systemd_run_command,
    extract_definition_hash,
    health_unit_name,
    service_definition_hash,
    unit_name,
    unit_prefix,
)
from systemd_compose.errors import SystemdComposeError
from systemd_compose.models import Service
from systemd_compose.parser import parse_compose_file
from systemd_compose.runner import run_command, run_command_capture

DEFAULT_COMPOSE_FILE = "systemd-compose.yaml"
DEFAULT_COMPOSE_FILES = (DEFAULT_COMPOSE_FILE, "systemd-compose.yml")
SYSTEMCTL_NOT_LOADED_EXIT_CODE = 5
READY_DEPENDENCY_STATES = {"active", "activating", "reloading"}
RUNNING_SERVICE_STATES = {"active", "activating", "reloading"}
PS_PROPERTIES = [
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "ExecMainStartTimestamp",
]
STATS_PROPERTIES = [
    "Id",
    "ActiveState",
    "ControlGroup",
    "CPUUsageNSec",
    "MemoryCurrent",
    "MemoryMax",
    "IPIngressBytes",
    "IPEgressBytes",
    "IOReadBytes",
    "IOWriteBytes",
    "TasksCurrent",
]
HEALTH_PROPERTIES = [
    "LoadState",
    "ActiveState",
    "Result",
    "ExecMainStatus",
    "InactiveExitTimestamp",
]
MISSING_SYSTEMD_VALUES = {"", "[not set]", "[no data]", "infinity", "max", "18446744073709551615"}
CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_ROOT = Path("/proc")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        exit_code = args.handler(args)
    except SystemdComposeError as exc:
        print(f"systemd-compose: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        command_parts = exc.cmd if isinstance(exc.cmd, list | tuple) else [str(exc.cmd)]
        command = shlex.join(str(part) for part in command_parts)
        print(f"systemd-compose: command failed with exit code {exc.returncode}: {command}", file=sys.stderr)
        return exc.returncode or 1
    except KeyboardInterrupt:
        print("systemd-compose: interrupted", file=sys.stderr)
        return 130

    return exit_code if isinstance(exit_code, int) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="systemd-compose",
        description="Run Compose-like services as transient systemd user units with mandatory bwrap sandboxes.",
    )
    parser.add_argument(
        "-f",
        "--file",
        default=DEFAULT_COMPOSE_FILE,
        help="compose YAML file (default: systemd-compose.yaml, then systemd-compose.yml)",
    )
    parser.add_argument(
        "-p",
        "--project-name",
        default=None,
        help="project name used as the unit name prefix (default: current directory name)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    up_parser = subparsers.add_parser("up", help="submit services to systemd")
    up_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print generated commands without executing them",
    )
    up_parser.add_argument(
        "--remove-orphans",
        action="store_true",
        help="stop project units that are no longer present in the compose file",
    )
    up_parser.add_argument("services", nargs="*", help="service names; omit to submit every service")
    up_parser.set_defaults(handler=handle_up)

    start_parser = subparsers.add_parser("start", help="start existing submitted services")
    start_parser.add_argument("services", nargs="*", help="service names; omit to start every service")
    start_parser.set_defaults(handler=handle_start)

    stop_parser = subparsers.add_parser("stop", help="stop submitted services without resetting them")
    stop_parser.add_argument("services", nargs="*", help="service names; omit to stop every service")
    stop_parser.set_defaults(handler=handle_stop)

    restart_parser = subparsers.add_parser("restart", help="restart existing submitted services")
    restart_parser.add_argument("services", nargs="*", help="service names; omit to restart every service")
    restart_parser.set_defaults(handler=handle_restart)

    down_parser = subparsers.add_parser("down", help="stop submitted services")
    down_parser.add_argument(
        "--remove-orphans",
        action="store_true",
        help="also stop project units that are no longer present in the compose file",
    )
    down_parser.add_argument("services", nargs="*", help="service names; omit to stop every service")
    down_parser.set_defaults(handler=handle_down)

    status_parser = subparsers.add_parser("status", help="show systemd status for submitted services")
    status_parser.add_argument(
        "service",
        nargs="?",
        help="service name; omit to show every service from the compose file",
    )
    status_parser.set_defaults(handler=handle_status)

    ps_parser = subparsers.add_parser("ps", help="list compose services")
    ps_parser.add_argument("services", nargs="*", help="service names; omit to list every service")
    ps_parser.set_defaults(handler=handle_ps)

    stats_parser = subparsers.add_parser("stats", help="show live service resource usage")
    stats_parser.add_argument("services", nargs="*", help="service names; omit to show every service")
    stats_parser.add_argument(
        "--no-stream",
        action="store_true",
        help="print one stats snapshot instead of refreshing every second",
    )
    stats_parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="refresh interval in seconds (default: 1)",
    )
    stats_parser.set_defaults(handler=handle_stats)

    health_parser = subparsers.add_parser("health", help="show service healthcheck status")
    health_parser.add_argument("services", nargs="*", help="service names; omit to show every service")
    health_parser.set_defaults(handler=handle_health)

    logs_parser = subparsers.add_parser(
        "logs",
        help="show journal logs for services",
        description=(
            "Show journal logs for all compose services by default, or for the listed services. "
            "Pass journalctl arguments after '--'."
        ),
    )
    logs_parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="follow journal logs",
    )
    logs_parser.add_argument(
        "log_args",
        nargs=argparse.REMAINDER,
        help="optional service names, then '--' followed by journalctl arguments",
    )
    logs_parser.set_defaults(handler=handle_logs)

    return parser


def handle_up(args: argparse.Namespace) -> int | None:
    config = parse_compose_file(resolve_compose_file(args.file))
    project_name = resolve_project_name(args.project_name)
    service_names = selected_service_names(config.services, args.services)
    exit_code = 0
    orphans = find_orphan_units(project_name, set(config.services)) if args.remove_orphans or not args.dry_run else []

    if args.remove_orphans:
        if args.dry_run:
            for orphan in orphans:
                print(shlex.join(["systemctl", "--user", "stop", f"{orphan}.service"]))
                print(shlex.join(["systemctl", "--user", "reset-failed", f"{orphan}.service"]))
        else:
            orphan_exit_code = cleanup_orphan_units(orphans)
            if orphan_exit_code != 0 and exit_code == 0:
                exit_code = orphan_exit_code
    elif orphans and not args.dry_run:
        print(
            "Found orphan unit(s): "
            f"{', '.join(f'{orphan}.service' for orphan in orphans)}. "
            "Use --remove-orphans to stop them."
        )

    for service_name in service_names:
        service = config.services[service_name]
        unit = unit_name(project_name, service_name)
        command = build_systemd_run_command(project_name, service_name, service)
        health_command = build_health_systemd_run_command(project_name, service_name, service)
        if args.dry_run:
            print(shlex.join(command))
            if health_command is not None:
                print(shlex.join(health_command))
        else:
            loaded_unit = inspect_unit(unit)
            desired_hash = service_definition_hash(project_name, service_name, service)
            if loaded_unit.load_state == "loaded" and should_skip_loaded_unit(loaded_unit, desired_hash):
                print(f"Skipping unchanged running unit: {unit}.service")
                continue
            if loaded_unit.load_state == "loaded":
                if loaded_unit.definition_hash == desired_hash:
                    print(f"Starting unchanged non-running unit: {unit}.service ({loaded_unit.active_state})")
                    ensure_volume_host_paths(service)
                    service_exit_code = start_existing_unit(unit)
                    if service_exit_code == 0:
                        service_exit_code = start_health_unit(project_name, service_name, service)
                    if service_exit_code != 0 and exit_code == 0:
                        exit_code = service_exit_code
                    continue
                print(f"Recreating changed unit: {unit}.service")
                cleanup_health_units(project_name, service_name)
                cleanup_exit_code = cleanup_unit(unit, reset_failed=True)
                if cleanup_exit_code != 0:
                    raise SystemdComposeError(f"could not recreate {unit}.service")
            dependency_blocker = blocked_dependency(project_name, service.depends_on)
            if dependency_blocker is not None:
                dependency_unit, dependency_state = dependency_blocker
                print(
                    f"Skipping {unit}.service: dependency {dependency_unit}.service "
                    f"is {dependency_state}"
                )
                exit_code = exit_code or 1
                continue
            ensure_volume_host_paths(service)
            run_command(command)
            if health_command is not None:
                run_command(health_command)

    return None if args.dry_run else exit_code


def handle_start(args: argparse.Namespace) -> int:
    config = parse_compose_file(resolve_compose_file(args.file))
    project_name = resolve_project_name(args.project_name)
    service_names = selected_service_names(config.services, args.services)
    exit_code = 0

    for service_name in service_names:
        unit = unit_name(project_name, service_name)
        loaded_unit = inspect_unit(unit)
        if loaded_unit.load_state != "loaded":
            print(f"Cannot start {unit}.service: service has not been created by up", file=sys.stderr)
            exit_code = exit_code or 1
            continue
        if loaded_unit.active_state in RUNNING_SERVICE_STATES:
            print(f"Skipping running unit: {unit}.service")
            continue
        ensure_volume_host_paths(config.services[service_name])
        service_exit_code = start_existing_unit(unit)
        if service_exit_code == 0:
            service_exit_code = start_health_unit(project_name, service_name, config.services[service_name])
        if service_exit_code != 0 and exit_code == 0:
            exit_code = service_exit_code

    return exit_code


def handle_stop(args: argparse.Namespace) -> int:
    config = parse_compose_file(resolve_compose_file(args.file))
    project_name = resolve_project_name(args.project_name)
    service_names = selected_service_names(config.services, args.services)
    exit_code = 0

    for service_name in reversed(service_names):
        unit = unit_name(project_name, service_name)
        health_exit_code = stop_health_units(project_name, service_name, config.services[service_name])
        if health_exit_code != 0 and exit_code == 0:
            exit_code = health_exit_code
        service_exit_code = stop_unit(unit)
        if service_exit_code != 0 and exit_code == 0:
            exit_code = service_exit_code

    return exit_code


def handle_restart(args: argparse.Namespace) -> int:
    config = parse_compose_file(resolve_compose_file(args.file))
    project_name = resolve_project_name(args.project_name)
    service_names = selected_service_names(config.services, args.services)
    exit_code = 0

    for service_name in service_names:
        unit = unit_name(project_name, service_name)
        loaded_unit = inspect_unit(unit)
        if loaded_unit.load_state != "loaded":
            print(f"Cannot restart {unit}.service: service has not been created by up", file=sys.stderr)
            exit_code = exit_code or 1
            continue
        ensure_volume_host_paths(config.services[service_name])
        service_exit_code = restart_existing_unit(unit)
        if service_exit_code == 0:
            service_exit_code = start_health_unit(project_name, service_name, config.services[service_name])
        if service_exit_code != 0 and exit_code == 0:
            exit_code = service_exit_code

    return exit_code


def handle_down(args: argparse.Namespace) -> int:
    config = parse_compose_file(resolve_compose_file(args.file))
    project_name = resolve_project_name(args.project_name)
    service_names = selected_service_names(config.services, args.services)
    exit_code = 0

    for service_name in reversed(service_names):
        unit = unit_name(project_name, service_name)
        health_exit_code = cleanup_health_units(project_name, service_name, config.services[service_name])
        if health_exit_code != 0 and exit_code == 0:
            exit_code = health_exit_code
        service_exit_code = cleanup_unit(unit, reset_failed=True)
        if service_exit_code != 0 and exit_code == 0:
            exit_code = service_exit_code

    if args.remove_orphans:
        orphan_exit_code = cleanup_orphan_units(find_orphan_units(project_name, set(config.services)))
        if orphan_exit_code != 0 and exit_code == 0:
            exit_code = orphan_exit_code

    return exit_code


def handle_status(args: argparse.Namespace) -> None:
    project_name = resolve_project_name(args.project_name)
    if args.service:
        service_names = [args.service]
    else:
        config = parse_compose_file(resolve_compose_file(args.file))
        service_names = list(config.services)

    units = [f"{unit_name(project_name, service_name)}.service" for service_name in service_names]
    return run_command(["systemctl", "--user", "--no-pager", "status", *units], check=False)


def handle_ps(args: argparse.Namespace) -> None:
    config = parse_compose_file(resolve_compose_file(args.file))
    project_name = resolve_project_name(args.project_name)
    service_names = selected_service_names(config.services, args.services)
    rows = [
        build_ps_row(project_name, service_name, config.services[service_name])
        for service_name in service_names
    ]
    print_table(["NAME", "SERVICE", "STATUS", "PID", "STARTED", "COMMAND"], rows)


def handle_stats(args: argparse.Namespace) -> None:
    config = parse_compose_file(resolve_compose_file(args.file))
    project_name = resolve_project_name(args.project_name)
    service_names = selected_service_names(config.services, args.services)
    cpu_count = os.cpu_count() or 1
    if args.interval < 0:
        raise SystemdComposeError("stats --interval must be greater than or equal to 0")

    if args.no_stream:
        previous_time = time.monotonic()
        previous = [
            collect_stats_snapshot(project_name, service_name)
            for service_name in service_names
        ]
        time.sleep(args.interval)
        current_time = time.monotonic()
        current = [
            collect_stats_snapshot(project_name, service_name)
            for service_name in service_names
        ]
        print(render_stats_table(current, previous, current_time, previous_time, cpu_count))
        return None

    previous: list[dict[str, str]] | None = None
    previous_time: float | None = None

    while True:
        current_time = time.monotonic()
        current = [
            collect_stats_snapshot(project_name, service_name)
            for service_name in service_names
        ]
        table = render_stats_table(current, previous, current_time, previous_time, cpu_count)
        if args.no_stream:
            print(table)
            return None
        print("\033[2J\033[H", end="")
        print(table, flush=True)
        previous = current
        previous_time = current_time
        time.sleep(args.interval)


def handle_health(args: argparse.Namespace) -> None:
    config = parse_compose_file(resolve_compose_file(args.file))
    project_name = resolve_project_name(args.project_name)
    service_names = selected_service_names(config.services, args.services)
    rows = [
        build_health_row(project_name, service_name, config.services[service_name])
        for service_name in service_names
    ]
    print_table(["NAME", "SERVICE", "HEALTH", "LAST CHECK"], rows)


def handle_logs(args: argparse.Namespace) -> None:
    project_name = resolve_project_name(args.project_name)
    service_names, journal_args = split_log_args(args.log_args)
    if not service_names:
        config = parse_compose_file(resolve_compose_file(args.file))
        service_names = list(config.services)

    command = ["journalctl", "--user"]
    if args.follow:
        command.append("-f")
    for service_name in service_names:
        command.extend(["-u", f"{unit_name(project_name, service_name)}.service"])
    command.extend(journal_args)
    run_command(command)


def build_ps_row(project_name: str, service_name: str, service: Service) -> list[str]:
    unit = unit_name(project_name, service_name)
    properties = unit_properties(unit, PS_PROPERTIES)
    return [
        unit,
        service_name,
        format_ps_status(properties),
        format_pid(properties.get("MainPID", "")),
        format_systemd_timestamp(properties.get("ExecMainStartTimestamp", "")),
        format_command(service),
    ]


def collect_stats_snapshot(project_name: str, service_name: str) -> dict[str, str]:
    unit = unit_name(project_name, service_name)
    properties = unit_properties(unit, STATS_PROPERTIES)
    properties["Name"] = unit
    properties["Service"] = service_name
    apply_cgroup_stats_fallbacks(properties)
    return properties


def build_health_row(project_name: str, service_name: str, service: Service) -> list[str]:
    unit = unit_name(project_name, service_name)
    if service.healthcheck is None:
        return [unit, service_name, "none", "-"]
    if service.healthcheck.disabled:
        return [unit, service_name, "disabled", "-"]

    main_properties = unit_properties(unit, ["LoadState", "ActiveState"])
    if main_properties.get("LoadState") != "loaded":
        return [unit, service_name, "not created", "-"]
    if main_properties.get("ActiveState") not in RUNNING_SERVICE_STATES:
        return [unit, service_name, main_properties.get("ActiveState", "inactive") or "inactive", "-"]

    health_unit = health_unit_name(project_name, service_name)
    properties = unit_file_properties(f"{health_unit}.service", HEALTH_PROPERTIES)
    health = format_health_status(properties)
    return [unit, service_name, health, format_systemd_timestamp(properties.get("InactiveExitTimestamp", ""))]


def format_health_status(properties: dict[str, str]) -> str:
    result = properties.get("Result", "")
    exit_status = properties.get("ExecMainStatus", "")
    if result == "success" or exit_status == "0":
        return "healthy"
    if result and result not in MISSING_SYSTEMD_VALUES:
        return "unhealthy"
    if properties.get("LoadState") != "loaded":
        return "starting"
    return "starting"


def apply_cgroup_stats_fallbacks(properties: dict[str, str]) -> None:
    cgroup = properties.get("ControlGroup", "")
    if cgroup in MISSING_SYSTEMD_VALUES:
        return
    cgroup_path = CGROUP_ROOT / cgroup.lstrip("/")
    if not cgroup_path.is_dir():
        return

    cpu_usage = read_cgroup_keyed_value(cgroup_path / "cpu.stat", "usage_usec")
    if is_missing_stats_value(properties.get("CPUUsageNSec", "")) and cpu_usage is not None:
        properties["CPUUsageNSec"] = str(cpu_usage * 1000)

    memory_current = read_cgroup_value(cgroup_path / "memory.current")
    if memory_current is None:
        memory_current = read_process_rss_total(cgroup_path / "cgroup.procs")
    if is_missing_stats_value(properties.get("MemoryCurrent", "")) and memory_current is not None:
        properties["MemoryCurrent"] = memory_current

    memory_max = read_cgroup_value(cgroup_path / "memory.max")
    if is_missing_stats_value(properties.get("MemoryMax", "")) and memory_max is not None:
        properties["MemoryMax"] = memory_max

    pids_current = read_cgroup_value(cgroup_path / "pids.current")
    if pids_current is None:
        pids_current = count_cgroup_lines(cgroup_path / "cgroup.threads") or count_cgroup_lines(cgroup_path / "cgroup.procs")
    if is_missing_stats_value(properties.get("TasksCurrent", "")) and pids_current is not None:
        properties["TasksCurrent"] = pids_current

    io_totals = read_cgroup_io_totals(cgroup_path / "io.stat")
    if io_totals is None:
        io_totals = read_process_io_totals(cgroup_path / "cgroup.procs")
    if io_totals is not None:
        read_bytes, write_bytes = io_totals
        if is_missing_stats_value(properties.get("IOReadBytes", "")):
            properties["IOReadBytes"] = str(read_bytes)
        if is_missing_stats_value(properties.get("IOWriteBytes", "")):
            properties["IOWriteBytes"] = str(write_bytes)


def render_stats_table(
    current: list[dict[str, str]],
    previous: list[dict[str, str]] | None,
    current_time: float,
    previous_time: float | None,
    cpu_count: int,
) -> str:
    previous_by_name = {row["Name"]: row for row in previous or []}
    rows: list[list[str]] = []
    for row in current:
        previous_row = previous_by_name.get(row["Name"])
        rows.append(
            [
                row["Name"],
                format_cpu_percent(row, previous_row, current_time, previous_time, cpu_count),
                format_memory_usage(row.get("MemoryCurrent", ""), row.get("MemoryMax", "")),
                format_memory_percent(row.get("MemoryCurrent", ""), row.get("MemoryMax", "")),
                format_io_pair(row.get("IPIngressBytes", ""), row.get("IPEgressBytes", "")),
                format_io_pair(row.get("IOReadBytes", ""), row.get("IOWriteBytes", "")),
                format_count(row.get("TasksCurrent", "")),
            ]
        )
    return format_table(["NAME", "CPU %", "MEM USAGE / LIMIT", "MEM %", "NET I/O", "BLOCK I/O", "PIDS"], rows)


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    print(format_table(headers, rows))


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [
        max(len(row[index]) for row in [headers, *rows])
        for index in range(len(headers))
    ]
    lines = [format_table_row(headers, widths)]
    lines.extend(format_table_row(row, widths) for row in rows)
    return "\n".join(lines)


def format_table_row(row: list[str], widths: list[int]) -> str:
    return "   ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()


def format_ps_status(properties: dict[str, str]) -> str:
    load_state = properties.get("LoadState", "")
    if load_state != "loaded":
        return "not created"
    active_state = properties.get("ActiveState", "")
    sub_state = properties.get("SubState", "")
    if active_state == "active":
        return f"Up ({sub_state})" if sub_state else "Up"
    if active_state == "failed":
        return f"Failed ({sub_state})" if sub_state else "Failed"
    if active_state == "inactive":
        return f"Exited ({sub_state})" if sub_state else "Exited"
    if active_state:
        return f"{active_state} ({sub_state})" if sub_state else active_state
    return "-"


def format_pid(value: str) -> str:
    return value if value and value != "0" else "-"


def format_systemd_timestamp(value: str) -> str:
    return "-" if value in MISSING_SYSTEMD_VALUES else value


def format_command(service: Service) -> str:
    if isinstance(service.command, list):
        return shlex.join(service.command)
    return service.command


def format_cpu_percent(
    current: dict[str, str],
    previous: dict[str, str] | None,
    current_time: float,
    previous_time: float | None,
    cpu_count: int,
) -> str:
    if previous is None or previous_time is None:
        return "-"
    current_cpu = parse_systemd_int(current.get("CPUUsageNSec", ""))
    previous_cpu = parse_systemd_int(previous.get("CPUUsageNSec", ""))
    if current_cpu is None or previous_cpu is None:
        return "-"
    elapsed = current_time - previous_time
    if elapsed <= 0:
        return "-"
    percent = ((current_cpu - previous_cpu) / 1_000_000_000) / elapsed / cpu_count * 100
    if percent < 0:
        return "-"
    return f"{percent:.2f}%"


def format_memory_usage(current_value: str, max_value: str) -> str:
    current = parse_systemd_int(current_value)
    maximum = parse_systemd_int(max_value)
    if current is None:
        return "-"
    if maximum is None:
        return f"{format_bytes(current)} / -"
    return f"{format_bytes(current)} / {format_bytes(maximum)}"


def format_memory_percent(current_value: str, max_value: str) -> str:
    current = parse_systemd_int(current_value)
    maximum = parse_systemd_int(max_value)
    if current is None or maximum is None or maximum <= 0:
        return "-"
    return f"{current / maximum * 100:.2f}%"


def format_io_pair(read_value: str, write_value: str) -> str:
    read = parse_systemd_int(read_value)
    write = parse_systemd_int(write_value)
    if read is None and write is None:
        return "-"
    return f"{format_bytes(read or 0)} / {format_bytes(write or 0)}"


def format_count(value: str) -> str:
    parsed = parse_systemd_int(value)
    return str(parsed) if parsed is not None else "-"


def parse_systemd_int(value: str) -> int | None:
    if value in MISSING_SYSTEMD_VALUES:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def format_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)}B"
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{value}B"


def is_missing_stats_value(value: str | None) -> bool:
    return value is None or value in MISSING_SYSTEMD_VALUES


def read_cgroup_value(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def read_cgroup_keyed_value(path: Path, key: str) -> int | None:
    content = read_cgroup_value(path)
    if content is None:
        return None
    for line in content.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key:
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def read_cgroup_io_totals(path: Path) -> tuple[int, int] | None:
    content = read_cgroup_value(path)
    if content is None:
        return None
    read_total = 0
    write_total = 0
    found = False
    for line in content.splitlines():
        for field in line.split()[1:]:
            key, separator, value = field.partition("=")
            if not separator:
                continue
            try:
                parsed = int(value)
            except ValueError:
                continue
            if key == "rbytes":
                read_total += parsed
                found = True
            elif key == "wbytes":
                write_total += parsed
                found = True
    return (read_total, write_total) if found else None


def count_cgroup_lines(path: Path) -> str | None:
    content = read_cgroup_value(path)
    if content is None:
        return None
    return str(sum(1 for line in content.splitlines() if line.strip()))


def read_cgroup_pids(path: Path) -> list[str]:
    content = read_cgroup_value(path)
    if content is None:
        return []
    return [line.strip() for line in content.splitlines() if line.strip()]


def read_process_rss_total(cgroup_procs_path: Path) -> str | None:
    total_kib = 0
    found = False
    for pid in read_cgroup_pids(cgroup_procs_path):
        status = read_cgroup_value(PROC_ROOT / pid / "status")
        if status is None:
            continue
        for line in status.splitlines():
            key, _separator, value = line.partition(":")
            if key != "VmRSS":
                continue
            parts = value.split()
            if not parts:
                continue
            try:
                total_kib += int(parts[0])
            except ValueError:
                continue
            found = True
            break
    return str(total_kib * 1024) if found else None


def read_process_io_totals(cgroup_procs_path: Path) -> tuple[int, int] | None:
    read_total = 0
    write_total = 0
    found = False
    for pid in read_cgroup_pids(cgroup_procs_path):
        io_content = read_cgroup_value(PROC_ROOT / pid / "io")
        if io_content is None:
            continue
        for line in io_content.splitlines():
            key, _separator, value = line.partition(":")
            try:
                parsed = int(value.strip())
            except ValueError:
                continue
            if key == "read_bytes":
                read_total += parsed
                found = True
            elif key == "write_bytes":
                write_total += parsed
                found = True
    return (read_total, write_total) if found else None


def resolve_project_name(project_name: str | None) -> str:
    if project_name:
        return project_name
    return Path(os.getcwd()).name


def resolve_compose_file(path: str) -> str:
    if path != DEFAULT_COMPOSE_FILE:
        return path

    for candidate in DEFAULT_COMPOSE_FILES:
        if Path(candidate).exists():
            return candidate

    return path


def selected_service_names(services: dict[str, Service], selected: list[str]) -> list[str]:
    if not selected:
        return list(services)

    unknown = [service_name for service_name in selected if service_name not in services]
    if unknown:
        raise SystemdComposeError(f"unknown service(s): {', '.join(unknown)}")

    return selected


def ensure_volume_host_paths(service: Service) -> None:
    for volume in service.volumes:
        host_path = Path(volume.host_path)
        if host_path.exists():
            continue
        try:
            host_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SystemdComposeError(
                f"could not create host path for service {service.name!r}: {host_path}"
            ) from exc


def split_log_args(log_args: list[str]) -> tuple[list[str], list[str]]:
    if "--" not in log_args:
        return log_args, []

    separator_index = log_args.index("--")
    return log_args[:separator_index], log_args[separator_index + 1 :]


def unit_is_loaded(unit: str) -> bool:
    return unit_property(unit, "LoadState") == "loaded"


def blocked_dependency(project_name: str, dependencies: list[str]) -> tuple[str, str] | None:
    for dependency in dependencies:
        dependency_unit = unit_name(project_name, dependency)
        if not unit_is_loaded(dependency_unit):
            continue
        active_state = unit_property(dependency_unit, "ActiveState")
        if active_state not in READY_DEPENDENCY_STATES:
            return dependency_unit, active_state
    return None


def unit_property(unit: str, property_name: str) -> str:
    result = run_command_capture(
        ["systemctl", "--user", "show", f"--property={property_name}", "--value", f"{unit}.service"]
    )
    value = result.stdout.strip()
    if result.returncode == 0:
        return value
    if value == "not-found" or is_unit_not_loaded(result):
        return "not-found"
    emit_completed_process_output(result)
    raise SystemdComposeError(f"could not inspect unit {unit}.service")


def unit_properties(unit: str, property_names: list[str]) -> dict[str, str]:
    return unit_file_properties(f"{unit}.service", property_names)


def unit_file_properties(unit_file: str, property_names: list[str]) -> dict[str, str]:
    command = ["systemctl", "--user", "show"]
    command.extend(f"--property={property_name}" for property_name in property_names)
    command.append(unit_file)
    result = run_command_capture(command)
    if result.returncode != 0:
        if is_unit_not_loaded(result):
            return {property_name: "not-found" for property_name in property_names}
        emit_completed_process_output(result)
        raise SystemdComposeError(f"could not inspect unit {unit_file}")

    values = {property_name: "" for property_name in property_names}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in values:
            values[key] = value
    return values


class UnitInspection:
    def __init__(self, load_state: str, active_state: str, definition_hash: str | None) -> None:
        self.load_state = load_state
        self.active_state = active_state
        self.definition_hash = definition_hash


def inspect_unit(unit: str) -> UnitInspection:
    load_state = unit_property(unit, "LoadState")
    if load_state != "loaded":
        return UnitInspection(load_state=load_state, active_state="inactive", definition_hash=None)

    active_state = unit_property(unit, "ActiveState")
    description = unit_property(unit, "Description")
    return UnitInspection(
        load_state=load_state,
        active_state=active_state,
        definition_hash=extract_definition_hash(description),
    )


def should_skip_loaded_unit(unit: UnitInspection, desired_hash: str) -> bool:
    return unit.definition_hash == desired_hash and unit.active_state in RUNNING_SERVICE_STATES


def cleanup_unit(unit: str, *, reset_failed: bool) -> int:
    exit_code = 0
    stop_exit_code = stop_unit(unit)
    if stop_exit_code != 0:
        exit_code = stop_exit_code
    if reset_failed:
        reset_exit_code = reset_failed_unit(unit)
        if reset_exit_code != 0 and exit_code == 0:
            exit_code = reset_exit_code
    return exit_code


def start_health_unit(project_name: str, service_name: str, service: Service) -> int:
    if service.healthcheck is None or service.healthcheck.disabled:
        return 0
    return run_command(["systemctl", "--user", "start", f"{health_unit_name(project_name, service_name)}.timer"], check=False)


def stop_health_units(project_name: str, service_name: str, service: Service) -> int:
    if service.healthcheck is None or service.healthcheck.disabled:
        return 0
    return stop_unit_file(f"{health_unit_name(project_name, service_name)}.timer")


def cleanup_health_units(project_name: str, service_name: str, service: Service | None = None) -> int:
    if service is not None and (service.healthcheck is None or service.healthcheck.disabled):
        return 0
    exit_code = 0
    health_unit = health_unit_name(project_name, service_name)
    for unit_file in [f"{health_unit}.timer", f"{health_unit}.service"]:
        stop_exit_code = stop_unit_file(unit_file)
        if stop_exit_code != 0 and exit_code == 0:
            exit_code = stop_exit_code
        reset_exit_code = reset_failed_unit_file(unit_file)
        if reset_exit_code != 0 and exit_code == 0:
            exit_code = reset_exit_code
    return exit_code


def stop_unit(unit: str) -> int:
    return stop_unit_file(f"{unit}.service")


def stop_unit_file(unit_file: str) -> int:
    result = run_command_capture(["systemctl", "--user", "stop", unit_file])
    if is_unit_not_loaded(result):
        return 0
    emit_completed_process_output(result)
    return result.returncode


def reset_failed_unit(unit: str) -> int:
    return reset_failed_unit_file(f"{unit}.service")


def reset_failed_unit_file(unit_file: str) -> int:
    result = run_command_capture(["systemctl", "--user", "reset-failed", unit_file])
    if is_unit_not_loaded(result):
        return 0
    emit_completed_process_output(result)
    return result.returncode


def start_existing_unit(unit: str) -> int:
    return run_command(["systemctl", "--user", "start", f"{unit}.service"], check=False)


def restart_existing_unit(unit: str) -> int:
    return run_command(["systemctl", "--user", "restart", f"{unit}.service"], check=False)


def find_orphan_units(project_name: str, current_services: set[str]) -> list[str]:
    prefix = unit_prefix(project_name)
    result = run_command_capture(
        [
            "systemctl",
            "--user",
            "list-units",
            "--all",
            "--plain",
            "--no-legend",
            "--type=service",
        ]
    )
    if result.returncode != 0:
        emit_completed_process_output(result)
        raise SystemdComposeError("could not list user service units")

    current_units = {unit_name(project_name, service_name) for service_name in current_services}
    current_units.update(health_unit_name(project_name, service_name) for service_name in current_services)
    orphans: list[str] = []
    for line in result.stdout.splitlines():
        columns = line.split(maxsplit=1)
        unit = columns[0] if columns else ""
        if not unit.endswith(".service"):
            continue
        unit_without_suffix = unit.removesuffix(".service")
        if not unit_without_suffix.startswith(prefix):
            continue
        if unit_without_suffix in current_units:
            continue
        description = unit_property(unit_without_suffix, "Description")
        if extract_definition_hash(description) is not None:
            orphans.append(unit_without_suffix)

    return sorted(orphans)


def cleanup_orphan_units(orphans: list[str]) -> int:
    exit_code = 0
    for orphan in sorted(orphans, reverse=True):
        print(f"Removing orphan unit: {orphan}.service")
        if orphan.endswith("-health"):
            orphan_exit_code = cleanup_health_orphan_unit(orphan)
        else:
            orphan_exit_code = cleanup_unit(orphan, reset_failed=True)
        if orphan_exit_code != 0 and exit_code == 0:
            exit_code = orphan_exit_code
    return exit_code


def cleanup_health_orphan_unit(health_unit: str) -> int:
    exit_code = 0
    for unit_file in [f"{health_unit}.timer", f"{health_unit}.service"]:
        stop_exit_code = stop_unit_file(unit_file)
        if stop_exit_code != 0 and exit_code == 0:
            exit_code = stop_exit_code
        reset_exit_code = reset_failed_unit_file(unit_file)
        if reset_exit_code != 0 and exit_code == 0:
            exit_code = reset_exit_code
    return exit_code


def emit_completed_process_output(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)


def is_unit_not_loaded(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout}\n{result.stderr}".lower()
    return result.returncode == SYSTEMCTL_NOT_LOADED_EXIT_CODE or "not loaded" in output


if __name__ == "__main__":
    raise SystemExit(main())
