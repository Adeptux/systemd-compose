from __future__ import annotations

import subprocess
import sys

from systemd_compose.builders import extract_definition_hash, health_unit_name, unit_name, unit_prefix
from systemd_compose.errors import SystemdComposeError
from systemd_compose.models import Service
from systemd_compose.runner import run_command, run_command_capture

SYSTEMCTL_NOT_LOADED_EXIT_CODE = 5
READY_DEPENDENCY_STATES = {"active", "activating", "reloading"}
RUNNING_SERVICE_STATES = {"active", "activating", "reloading"}
MISSING_SYSTEMD_VALUES = {"", "[not set]", "[no data]", "infinity", "max", "18446744073709551615"}


def systemctl_command(*, system: bool = False) -> list[str]:
    return ["systemctl"] if system else ["systemctl", "--user"]


def unit_is_loaded(unit: str, *, system: bool = False) -> bool:
    return unit_property(unit, "LoadState", system=system) == "loaded"

def blocked_dependency(project_name: str, dependencies: list[str]) -> tuple[str, str] | None:
    for dependency in dependencies:
        dependency_unit = unit_name(project_name, dependency)
        if not unit_is_loaded(dependency_unit):
            continue
        active_state = unit_property(dependency_unit, "ActiveState")
        if active_state not in READY_DEPENDENCY_STATES:
            return dependency_unit, active_state
    return None

def unit_property(unit: str, property_name: str, *, system: bool = False) -> str:
    result = run_command_capture(
        [*systemctl_command(system=system), "show", f"--property={property_name}", "--value", f"{unit}.service"]
    )
    value = result.stdout.strip()
    if result.returncode == 0:
        return value
    if value == "not-found" or is_unit_not_loaded(result):
        return "not-found"
    emit_completed_process_output(result)
    raise SystemdComposeError(f"could not inspect unit {unit}.service")

def unit_properties(unit: str, property_names: list[str], *, system: bool = False) -> dict[str, str]:
    return unit_file_properties(f"{unit}.service", property_names, system=system)

def unit_file_properties(unit_file: str, property_names: list[str], *, system: bool = False) -> dict[str, str]:
    command = [*systemctl_command(system=system), "show"]
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

def inspect_unit(unit: str, *, system: bool = False) -> UnitInspection:
    load_state = unit_property(unit, "LoadState", system=system)
    if load_state != "loaded":
        return UnitInspection(load_state=load_state, active_state="inactive", definition_hash=None)

    active_state = unit_property(unit, "ActiveState", system=system)
    description = unit_property(unit, "Description", system=system)
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
