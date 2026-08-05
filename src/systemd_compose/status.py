from __future__ import annotations

import shlex

from systemd_compose.builders import health_unit_name, unit_name
from systemd_compose.models import Service
from systemd_compose.systemd_units import (
    MISSING_SYSTEMD_VALUES,
    RUNNING_SERVICE_STATES,
    unit_file_properties,
    unit_properties,
)

PS_PROPERTIES = [
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "ExecMainStartTimestamp",
]
HEALTH_PROPERTIES = [
    "LoadState",
    "ActiveState",
    "Result",
    "ExecMainStatus",
    "InactiveExitTimestamp",
]


def build_ps_row(project_name: str, service_name: str, service: Service, *, system: bool = False) -> list[str]:
    unit = unit_name(project_name, service_name)
    properties = unit_properties(unit, PS_PROPERTIES, system=system)
    return [
        unit,
        service_name,
        format_ps_status(properties),
        build_health_cell(project_name, service_name, service, properties, system=system),
        format_pid(properties.get("MainPID", "")),
        format_systemd_timestamp(properties.get("ExecMainStartTimestamp", "")),
        format_command(service),
    ]

def build_health_row(project_name: str, service_name: str, service: Service, *, system: bool = False) -> list[str]:
    unit = unit_name(project_name, service_name)
    health, last_check = collect_health_status(project_name, service_name, service, system=system)
    return [unit, service_name, health, last_check]

def build_health_cell(
    project_name: str,
    service_name: str,
    service: Service,
    main_properties: dict[str, str],
    *,
    system: bool = False,
) -> str:
    health, _last_check = collect_health_status(project_name, service_name, service, main_properties, system=system)
    return health

def collect_health_status(
    project_name: str,
    service_name: str,
    service: Service,
    main_properties: dict[str, str] | None = None,
    *,
    system: bool = False,
) -> tuple[str, str]:
    unit = unit_name(project_name, service_name)
    if service.healthcheck is None:
        return ("none", "-")
    if service.healthcheck.disabled:
        return ("disabled", "-")

    if main_properties is None:
        main_properties = unit_properties(unit, ["LoadState", "ActiveState"], system=system)
    if main_properties.get("LoadState") != "loaded":
        return ("not created", "-")
    if main_properties.get("ActiveState") not in RUNNING_SERVICE_STATES:
        return (main_properties.get("ActiveState", "inactive") or "inactive", "-")

    health_unit = health_unit_name(project_name, service_name)
    properties = unit_file_properties(f"{health_unit}.service", HEALTH_PROPERTIES, system=system)
    health = format_health_status(properties)
    return (health, format_systemd_timestamp(properties.get("InactiveExitTimestamp", "")))

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
