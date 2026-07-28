from __future__ import annotations

import hashlib
import json
import re
import shlex

from systemd_compose.errors import SystemdComposeError
from systemd_compose.models import Resources, Service

BWRAP_PATH = "/usr/bin/bwrap"
DESCRIPTION_HASH_PREFIX = "systemd-compose-hash="
ACCOUNTING_PROPERTIES = [
    "CPUAccounting=yes",
    "MemoryAccounting=yes",
    "TasksAccounting=yes",
    "IOAccounting=yes",
    "IPAccounting=yes",
]

BASE_BWRAP_ARGS = [
    "--dev-bind",
    "/dev",
    "/dev",
    "--ro-bind",
    "/usr",
    "/usr",
    "--ro-bind",
    "/bin",
    "/bin",
    "--ro-bind",
    "/lib",
    "/lib",
    "--ro-bind",
    "/lib64",
    "/lib64",
    "--ro-bind",
    "/etc",
    "/etc",
    "--proc",
    "/proc",
    "--ro-bind",
    "/sys",
    "/sys",
    "--tmpfs",
    "/tmp",
    "--tmpfs",
    "/run",
]

_UNIT_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.@-]+")


def build_bwrap_args(service: Service) -> list[str]:
    args = list(BASE_BWRAP_ARGS)

    for volume in service.volumes:
        bind_arg = "--ro-bind" if volume.read_only else "--bind"
        args.extend([bind_arg, volume.host_path, volume.sandbox_path])

    for tmpfs_path in service.tmpfs:
        args.extend(["--tmpfs", tmpfs_path])

    for key, value in service.environment.items():
        if not key:
            raise SystemdComposeError(f"service {service.name!r} has an empty environment key")
        args.extend(["--setenv", key, value])

    if service.working_dir is not None:
        args.extend(["--chdir", service.working_dir])

    args.append("--")
    args.extend(_command_argv(service))
    return args


def build_service_payload(service: Service) -> list[str]:
    return [BWRAP_PATH, *build_bwrap_args(service)]


def build_systemd_run_command(project_name: str, service_name: str, service: Service) -> list[str]:
    unit = unit_name(project_name, service_name)
    command = [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        f"--description={build_description(project_name, service_name, service)}",
        "-p",
        f"SyslogIdentifier={unit}",
    ]
    for property_value in ACCOUNTING_PROPERTIES:
        command.extend(["-p", property_value])

    for dependency in service.depends_on:
        dependency_unit = f"{unit_name(project_name, dependency)}.service"
        command.extend(["-p", f"Requires={dependency_unit}"])
        command.extend(["-p", f"After={dependency_unit}"])
        command.extend(["-p", f"BindsTo={dependency_unit}"])

    if service.restart is not None:
        command.extend(["-p", f"Restart={service.restart}"])

    for property_value in build_resource_properties(service.resources):
        command.extend(["-p", property_value])

    command.extend(build_service_payload(service))
    return command


def build_description(project_name: str, service_name: str, service: Service) -> str:
    return (
        f"systemd-compose: {project_name} {service_name} "
        f"{DESCRIPTION_HASH_PREFIX}{service_definition_hash(project_name, service_name, service)}"
    )


def service_definition_hash(project_name: str, service_name: str, service: Service) -> str:
    data = {
        "unit": unit_name(project_name, service_name),
        "syslog_identifier": unit_name(project_name, service_name),
        "accounting": ACCOUNTING_PROPERTIES,
        "dependencies": [f"{unit_name(project_name, dependency)}.service" for dependency in service.depends_on],
        "restart": service.restart,
        "resources": build_resource_properties(service.resources),
        "payload": build_service_payload(service),
    }
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def extract_definition_hash(description: str) -> str | None:
    marker_index = description.find(DESCRIPTION_HASH_PREFIX)
    if marker_index == -1:
        return None
    start = marker_index + len(DESCRIPTION_HASH_PREFIX)
    return description[start:].split(maxsplit=1)[0]


def unit_name(project_name: str, service_name: str) -> str:
    return f"{_clean_unit_component(project_name)}-{_clean_unit_component(service_name)}"


def unit_prefix(project_name: str) -> str:
    return f"{_clean_unit_component(project_name)}-"


def _clean_unit_component(value: str) -> str:
    cleaned = _UNIT_COMPONENT_RE.sub("-", value.strip())
    cleaned = cleaned.strip("-")
    if not cleaned:
        raise SystemdComposeError("unit name components cannot be empty")
    return cleaned


def _command_argv(service: Service) -> list[str]:
    if isinstance(service.command, list):
        if not service.command:
            raise SystemdComposeError(f"service {service.name!r} command cannot be empty")
        return list(service.command)

    try:
        argv = shlex.split(service.command)
    except ValueError as exc:
        raise SystemdComposeError(f"service {service.name!r} command is invalid: {exc}") from exc

    if not argv:
        raise SystemdComposeError(f"service {service.name!r} command cannot be empty")
    return argv


def build_resource_properties(resources: Resources | None) -> list[str]:
    if resources is None:
        return []

    properties: list[str] = []
    if resources.mem_limit is not None:
        properties.append(f"MemoryMax={resources.mem_limit}")
    if resources.cpus is not None:
        properties.append(f"CPUQuota={_cpu_quota(resources.cpus)}")
    if resources.pids_limit is not None:
        properties.append(f"TasksMax={resources.pids_limit}")
    return properties


def _cpu_quota(cpus: str) -> str:
    if cpus.endswith("%"):
        return cpus
    return f"{float(cpus) * 100:g}%"
