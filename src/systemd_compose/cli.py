from __future__ import annotations

import argparse
import os
import shutil
import shlex
import subprocess
import sys
import time

from systemd_compose.builders import (
    BWRAP_PATH,
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
from systemd_compose.models import Service
from systemd_compose.parser import parse_compose_file
from systemd_compose.persistence import (
    installed_orphan_unit_names,
    project_is_installed,
    require_no_opposite_scope_project,
    remove_orphan_unit_files,
    remove_unit_files,
    require_system_privileges,
    sync_unit_files,
    systemctl_command,
    target_unit_name,
)
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
        preflight_required_host_tools()
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


def preflight_required_host_tools() -> None:
    missing: list[str] = []
    if not os.path.isfile(BWRAP_PATH):
        missing.append(BWRAP_PATH)
    for tool in ["systemd-run", "systemctl", "journalctl"]:
        if shutil.which(tool) is None:
            missing.append(tool)

    if missing:
        raise SystemdComposeError(
            "missing required host tool(s): "
            f"{', '.join(missing)}. "
            "Install bubblewrap and systemd tools before running systemd-compose."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="systemd-compose",
        description="Run Compose-like services as systemd units with mandatory bwrap sandboxes.",
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
        help="project name used as the unit name prefix (default: compose name, then current directory name)",
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
    up_parser.add_argument(
        "--system",
        action="store_true",
        help="operate on installed system units instead of user units",
    )
    up_parser.add_argument("services", nargs="*", help="service names; omit to submit every service")
    up_parser.set_defaults(handler=handle_up)

    start_parser = subparsers.add_parser("start", help="start existing submitted services")
    start_parser.add_argument("--system", action="store_true", help="operate on installed system units")
    start_parser.add_argument("services", nargs="*", help="service names; omit to start every service")
    start_parser.set_defaults(handler=handle_start)

    stop_parser = subparsers.add_parser("stop", help="stop submitted services without resetting them")
    stop_parser.add_argument("--system", action="store_true", help="operate on installed system units")
    stop_parser.add_argument("services", nargs="*", help="service names; omit to stop every service")
    stop_parser.set_defaults(handler=handle_stop)

    restart_parser = subparsers.add_parser("restart", help="restart existing submitted services")
    restart_parser.add_argument("--system", action="store_true", help="operate on installed system units")
    restart_parser.add_argument("services", nargs="*", help="service names; omit to restart every service")
    restart_parser.set_defaults(handler=handle_restart)

    down_parser = subparsers.add_parser("down", help="stop submitted services")
    down_parser.add_argument(
        "--remove-orphans",
        action="store_true",
        help="also stop project units that are no longer present in the compose file",
    )
    down_parser.add_argument("--system", action="store_true", help="operate on installed system units")
    down_parser.add_argument("services", nargs="*", help="service names; omit to stop every service")
    down_parser.set_defaults(handler=handle_down)

    install_parser = subparsers.add_parser("install", help="install persistent systemd units")
    install_parser.add_argument("--system", action="store_true", help="install system units instead of user units")
    install_parser.add_argument("--now", action="store_true", help="start the installed project after enabling it")
    install_parser.add_argument(
        "--remove-orphans",
        action="store_true",
        help="remove installed project units that are no longer present in the compose file",
    )
    install_parser.set_defaults(handler=handle_install)

    uninstall_parser = subparsers.add_parser("uninstall", help="remove persistent systemd units")
    uninstall_parser.add_argument("--system", action="store_true", help="remove system units instead of user units")
    uninstall_parser.add_argument("--now", action="store_true", help="stop the installed project before removing it")
    uninstall_parser.set_defaults(handler=handle_uninstall)

    status_parser = subparsers.add_parser("status", help="show systemd status for submitted services")
    status_parser.add_argument(
        "services",
        nargs="*",
        help="service names; omit to show every service from the compose file",
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


def load_config_for_project_name(args: argparse.Namespace):
    if args.project_name is not None:
        return None
    compose_file = resolve_compose_file(args.file)
    if args.file == DEFAULT_COMPOSE_FILE and not os.path.exists(compose_file):
        return None
    return parse_compose_file(compose_file)


def handle_up(args: argparse.Namespace) -> int | None:
    config = parse_compose_file(resolve_compose_file(args.file))
    project_name = resolve_project_name(args.project_name, config.name)
    service_names = selected_service_names(config.services, args.services)
    require_system_privileges(system=args.system)
    if not args.dry_run and project_is_installed(project_name, system=args.system):
        return reconcile_installed_project(
            project_name,
            config.services,
            selected_service_names=service_names if args.services else [],
            current_service_names=set(config.services),
            remove_orphans=args.remove_orphans,
            system=args.system,
            restart_changed=True,
        )
    if args.system:
        raise SystemdComposeError("up --system requires an installed system project; run install --system first")
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
        command = build_systemd_run_command(project_name, service_name, service, config.services)
        health_command = build_health_systemd_run_command(project_name, service_name, service)
        if args.dry_run:
            print(shlex.join(command))
            if health_command is not None:
                print(shlex.join(health_command))
        else:
            loaded_unit = inspect_unit(unit)
            desired_hash = service_definition_hash(project_name, service_name, service, config.services)
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


def handle_install(args: argparse.Namespace) -> int:
    require_system_privileges(system=args.system)
    config = parse_compose_file(resolve_compose_file(args.file))
    project_name = resolve_project_name(args.project_name, config.name)
    require_no_opposite_scope_project(project_name, system=args.system)
    ensure_all_volume_host_paths(config.services)
    changes = sync_unit_files(project_name, config.services, system=args.system)
    if args.remove_orphans:
        changes.extend(remove_orphan_unit_files(project_name, set(config.services), system=args.system))
    elif orphan_names := installed_orphan_unit_names(project_name, set(config.services), system=args.system):
        print(
            "Found installed orphan unit(s): "
            f"{', '.join(orphan_names)}. "
            "Use install --remove-orphans to remove them."
        )
    ctl = systemctl_command(system=args.system)
    if changes:
        run_command([*ctl, "daemon-reload"])
    run_command([*ctl, "enable", target_unit_name(project_name)])
    if args.now:
        return start_installed_units(project_name, [], system=args.system)
    return 0


def handle_uninstall(args: argparse.Namespace) -> int:
    require_system_privileges(system=args.system)
    config = load_config_for_project_name(args)
    project_name = resolve_project_name(args.project_name, config.name if config is not None else None)
    ctl = systemctl_command(system=args.system)
    target = target_unit_name(project_name)
    exit_code = 0
    if args.now:
        exit_code = run_command([*ctl, "stop", target], check=False)
    disable_exit_code = run_command([*ctl, "disable", target], check=False)
    if disable_exit_code != 0 and exit_code == 0:
        exit_code = disable_exit_code
    changes = remove_unit_files(project_name, system=args.system)
    if changes:
        run_command([*ctl, "daemon-reload"])
    return exit_code


def reconcile_installed_project(
    project_name: str,
    services: dict[str, Service],
    *,
    selected_service_names: list[str],
    current_service_names: set[str],
    remove_orphans: bool,
    system: bool,
    restart_changed: bool,
) -> int:
    ensure_all_volume_host_paths(services)
    changes = sync_unit_files(project_name, services, system=system)
    orphan_names = installed_orphan_unit_names(project_name, current_service_names, system=system)
    ctl = systemctl_command(system=system)
    exit_code = 0

    if remove_orphans:
        for orphan in orphan_names:
            stop_exit_code = run_command([*ctl, "stop", orphan], check=False)
            if stop_exit_code != 0 and exit_code == 0:
                exit_code = stop_exit_code
        changes.extend(remove_orphan_unit_files(project_name, current_service_names, system=system))
    elif orphan_names:
        print(
            "Found installed orphan unit(s): "
            f"{', '.join(orphan_names)}. "
            "Use up --remove-orphans to stop, disable, and remove them."
        )

    if changes:
        run_command([*ctl, "daemon-reload"])

    if restart_changed:
        selected_units = {
            f"{unit_name(project_name, service_name)}.service"
            for service_name in selected_service_names
        }
        for change in changes:
            if change.action != "updated" or not change.path.name.endswith(".service"):
                continue
            if change.path.name.endswith("-health.service"):
                continue
            if selected_units and change.path.name not in selected_units:
                continue
            restart_exit_code = run_command([*ctl, "restart", change.path.name], check=False)
            if restart_exit_code != 0 and exit_code == 0:
                exit_code = restart_exit_code

    start_exit_code = start_installed_units(project_name, selected_service_names, system=system)
    if start_exit_code != 0 and exit_code == 0:
        exit_code = start_exit_code
    return exit_code


def start_installed_units(project_name: str, service_names: list[str], *, system: bool) -> int:
    ctl = systemctl_command(system=system)
    if not service_names:
        return run_command([*ctl, "start", target_unit_name(project_name)], check=False)
    units = [f"{unit_name(project_name, service_name)}.service" for service_name in service_names]
    return run_command([*ctl, "start", *units], check=False)


def stop_installed_units(project_name: str, service_names: list[str], *, system: bool) -> int:
    ctl = systemctl_command(system=system)
    if not service_names:
        return run_command([*ctl, "stop", target_unit_name(project_name)], check=False)
    units = [f"{unit_name(project_name, service_name)}.service" for service_name in reversed(service_names)]
    return run_command([*ctl, "stop", *units], check=False)


def restart_installed_units(project_name: str, service_names: list[str], *, system: bool) -> int:
    ctl = systemctl_command(system=system)
    if not service_names:
        return run_command([*ctl, "restart", target_unit_name(project_name)], check=False)
    units = [f"{unit_name(project_name, service_name)}.service" for service_name in service_names]
    return run_command([*ctl, "restart", *units], check=False)


def ensure_all_volume_host_paths(services: dict[str, Service]) -> None:
    for service in services.values():
        ensure_volume_host_paths(service)


def resolve_inspection_system_scope(project_name: str) -> bool:
    if project_is_installed(project_name, system=False):
        return False
    if project_is_installed(project_name, system=True):
        return True
    return False


def handle_start(args: argparse.Namespace) -> int:
    config = parse_compose_file(resolve_compose_file(args.file))
    project_name = resolve_project_name(args.project_name, config.name)
    service_names = selected_service_names(config.services, args.services)
    require_system_privileges(system=args.system)
    if project_is_installed(project_name, system=args.system):
        ensure_all_volume_host_paths({name: config.services[name] for name in service_names})
        return start_installed_units(project_name, service_names if args.services else [], system=args.system)
    if args.system:
        raise SystemdComposeError("start --system requires an installed system project")
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
    project_name = resolve_project_name(args.project_name, config.name)
    service_names = selected_service_names(config.services, args.services)
    require_system_privileges(system=args.system)
    if project_is_installed(project_name, system=args.system):
        return stop_installed_units(project_name, service_names if args.services else [], system=args.system)
    if args.system:
        raise SystemdComposeError("stop --system requires an installed system project")
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
    project_name = resolve_project_name(args.project_name, config.name)
    service_names = selected_service_names(config.services, args.services)
    require_system_privileges(system=args.system)
    if project_is_installed(project_name, system=args.system):
        ensure_all_volume_host_paths({name: config.services[name] for name in service_names})
        return restart_installed_units(project_name, service_names if args.services else [], system=args.system)
    if args.system:
        raise SystemdComposeError("restart --system requires an installed system project")
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
    project_name = resolve_project_name(args.project_name, config.name)
    service_names = selected_service_names(config.services, args.services)
    require_system_privileges(system=args.system)
    if project_is_installed(project_name, system=args.system):
        exit_code = stop_installed_units(project_name, service_names if args.services else [], system=args.system)
        if args.remove_orphans:
            ctl = systemctl_command(system=args.system)
            orphan_names = installed_orphan_unit_names(project_name, set(config.services), system=args.system)
            for orphan in orphan_names:
                orphan_exit_code = run_command([*ctl, "stop", orphan], check=False)
                if orphan_exit_code != 0 and exit_code == 0:
                    exit_code = orphan_exit_code
            if remove_orphan_unit_files(project_name, set(config.services), system=args.system):
                run_command([*ctl, "daemon-reload"])
        return exit_code
    if args.system:
        raise SystemdComposeError("down --system requires an installed system project")
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
    if args.services:
        compose_file = resolve_compose_file(args.file)
        should_load_config = (
            args.file != DEFAULT_COMPOSE_FILE
            or (args.project_name is None and os.path.exists(compose_file))
        )
        config = parse_compose_file(compose_file) if should_load_config else None
        service_names = (
            selected_service_names(config.services, args.services)
            if config is not None
            else args.services
        )
    else:
        config = parse_compose_file(resolve_compose_file(args.file))
        service_names = list(config.services)
    project_name = resolve_project_name(args.project_name, config.name if config is not None else None)
    system = resolve_inspection_system_scope(project_name)

    units = [f"{unit_name(project_name, service_name)}.service" for service_name in service_names]
    return run_command([*systemctl_command(system=system), "--no-pager", "status", *units], check=False)


def handle_ps(args: argparse.Namespace) -> None:
    config = parse_compose_file(resolve_compose_file(args.file))
    project_name = resolve_project_name(args.project_name, config.name)
    system = resolve_inspection_system_scope(project_name)
    service_names = selected_service_names(config.services, args.services)
    rows = [
        build_ps_row(project_name, service_name, config.services[service_name], system=system)
        for service_name in service_names
    ]
    print_table(["NAME", "SERVICE", "STATUS", "HEALTH", "PID", "STARTED", "COMMAND"], rows)


def handle_stats(args: argparse.Namespace) -> None:
    config = parse_compose_file(resolve_compose_file(args.file))
    project_name = resolve_project_name(args.project_name, config.name)
    system = resolve_inspection_system_scope(project_name)
    service_names = selected_service_names(config.services, args.services)
    cpu_count = os.cpu_count() or 1
    if args.interval < 0:
        raise SystemdComposeError("stats --interval must be greater than or equal to 0")

    if args.no_stream:
        previous_time = time.monotonic()
        previous = [
            collect_stats_snapshot(project_name, service_name, system=system)
            for service_name in service_names
        ]
        time.sleep(args.interval)
        current_time = time.monotonic()
        current = [
            collect_stats_snapshot(project_name, service_name, system=system)
            for service_name in service_names
        ]
        print(render_stats_table(current, previous, current_time, previous_time, cpu_count))
        return None

    previous: list[dict[str, str]] | None = None
    previous_time: float | None = None

    while True:
        current_time = time.monotonic()
        current = [
            collect_stats_snapshot(project_name, service_name, system=system)
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
    project_name = resolve_project_name(args.project_name, config.name)
    system = resolve_inspection_system_scope(project_name)
    service_names = selected_service_names(config.services, args.services)
    rows = [
        build_health_row(project_name, service_name, config.services[service_name], system=system)
        for service_name in service_names
    ]
    print_table(["NAME", "SERVICE", "HEALTH", "LAST CHECK"], rows)


def handle_logs(args: argparse.Namespace) -> None:
    service_names, journal_args = split_log_args(args.log_args)
    config = None
    if args.project_name is None or not service_names:
        config = (
            load_config_for_project_name(args)
            if service_names
            else parse_compose_file(resolve_compose_file(args.file))
        )
    if not service_names:
        if args.health:
            service_names = [
                service_name
                for service_name, service in config.services.items()
                if service.healthcheck is not None and not service.healthcheck.disabled
            ]
        else:
            service_names = list(config.services)
    project_name = resolve_project_name(args.project_name, config.name if config is not None else None)
    system = resolve_inspection_system_scope(project_name)

    command = ["journalctl"]
    if not system:
        command.append("--user")
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
