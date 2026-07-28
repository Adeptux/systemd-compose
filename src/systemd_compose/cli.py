from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time

from systemd_compose.builders import (
    build_health_systemd_run_command,
    build_systemd_run_command,
    health_unit_name,
    service_definition_hash,
    unit_name,
)
from systemd_compose.config import (
    DEFAULT_COMPOSE_FILE,
    ensure_volume_host_paths,
    resolve_compose_file,
    resolve_project_name,
    selected_service_names,
    split_log_args,
)
from systemd_compose.errors import SystemdComposeError
from systemd_compose.parser import parse_compose_file
from systemd_compose.runner import run_command
from systemd_compose.stats import collect_stats_snapshot, render_stats_table
from systemd_compose.status import build_health_row, build_ps_row
from systemd_compose.systemd_units import (
    RUNNING_SERVICE_STATES,
    blocked_dependency,
    cleanup_health_units,
    cleanup_orphan_units,
    cleanup_unit,
    find_orphan_units,
    inspect_unit,
    restart_existing_unit,
    should_skip_loaded_unit,
    start_existing_unit,
    start_health_unit,
    stop_health_units,
    stop_unit,
)
from systemd_compose.tables import print_table


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
        "--health",
        action="store_true",
        help="show healthcheck sidecar logs instead of service logs",
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
    print_table(["NAME", "SERVICE", "STATUS", "HEALTH", "PID", "STARTED", "COMMAND"], rows)


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
        if args.health:
            service_names = [
                service_name
                for service_name, service in config.services.items()
                if service.healthcheck is not None and not service.healthcheck.disabled
            ]
        else:
            service_names = list(config.services)

    command = ["journalctl", "--user"]
    if args.follow:
        command.append("-f")
    for service_name in service_names:
        if args.health:
            command.extend(["-u", f"{health_unit_name(project_name, service_name)}.service"])
        else:
            command.extend(["-u", f"{unit_name(project_name, service_name)}.service"])
    command.extend(journal_args)
    run_command(command)


if __name__ == "__main__":
    raise SystemExit(main())
