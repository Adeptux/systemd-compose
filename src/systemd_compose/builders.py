from __future__ import annotations

import hashlib
import json
import re
import shlex

from systemd_compose.errors import SystemdComposeError
from systemd_compose.models import Healthcheck, Resources, Service

BWRAP_PATH = "/usr/bin/bwrap"
DESCRIPTION_HASH_PREFIX = "systemd-compose-hash="
ACCOUNTING_PROPERTIES = [
    "CPUAccounting=yes",
    "MemoryAccounting=yes",
    "TasksAccounting=yes",
    "IOAccounting=yes",
    "IPAccounting=yes",
]
GENERATED_UNIT_MARKER = "X-SystemdCompose=1"

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


def build_health_systemd_run_command(project_name: str, service_name: str, service: Service) -> list[str] | None:
    if service.healthcheck is None or service.healthcheck.disabled:
        return None

    unit = unit_name(project_name, service_name)
    health_unit = health_unit_name(project_name, service_name)
    command = [
        "systemd-run",
        "--user",
        f"--unit={health_unit}",
        f"--description={build_health_description(project_name, service_name, service)}",
        f"--on-active={service.healthcheck.start_period}",
        f"--on-unit-active={service.healthcheck.interval}",
        "--timer-property",
        f"Requires={unit}.service",
        "--timer-property",
        f"After={unit}.service",
        "--timer-property",
        f"BindsTo={unit}.service",
        "-p",
        "Type=oneshot",
        "-p",
        f"SyslogIdentifier={health_unit}",
        "-p",
        f"Requires={unit}.service",
        "-p",
        f"After={unit}.service",
        "-p",
        f"BindsTo={unit}.service",
        "-p",
        f"TimeoutStartSec={service.healthcheck.timeout}",
    ]
    command.extend(build_health_service_payload(service))
    return command


def build_project_target_unit(project_name: str, wanted_units: list[str], *, system: bool = False) -> str:
    wanted_by = "multi-user.target" if system else "default.target"
    return _unit_text(
        [
            "[Unit]",
            f"Description=systemd-compose project: {project_name}",
            GENERATED_UNIT_MARKER,
            f"Wants={' '.join(wanted_units)}" if wanted_units else "",
            "",
            "[Install]",
            f"WantedBy={wanted_by}",
        ]
    )


def build_installed_service_unit(project_name: str, service_name: str, service: Service) -> str:
    unit = unit_name(project_name, service_name)
    lines = [
        "[Unit]",
        f"Description={build_description(project_name, service_name, service)}",
        GENERATED_UNIT_MARKER,
    ]
    for dependency in service.depends_on:
        dependency_unit = f"{unit_name(project_name, dependency)}.service"
        lines.append(f"Requires={dependency_unit}")
        lines.append(f"After={dependency_unit}")
        lines.append(f"BindsTo={dependency_unit}")
    lines.extend(
        [
            "",
            "[Service]",
            f"SyslogIdentifier={unit}",
            *ACCOUNTING_PROPERTIES,
        ]
    )
    if service.restart is not None:
        lines.append(f"Restart={service.restart}")
    lines.extend(build_resource_properties(service.resources))
    lines.append(f"ExecStart={_systemd_exec(build_service_payload(service))}")
    lines.extend(
        [
            "",
            "[Install]",
            f"WantedBy={unit_prefix(project_name).removesuffix('-')}.target",
        ]
    )
    return _unit_text(lines)


def build_installed_health_service_unit(project_name: str, service_name: str, service: Service) -> str | None:
    if service.healthcheck is None or service.healthcheck.disabled:
        return None

    unit = unit_name(project_name, service_name)
    health_unit = health_unit_name(project_name, service_name)
    return _unit_text(
        [
            "[Unit]",
            f"Description={build_health_description(project_name, service_name, service)}",
            GENERATED_UNIT_MARKER,
            f"Requires={unit}.service",
            f"After={unit}.service",
            f"BindsTo={unit}.service",
            "",
            "[Service]",
            "Type=oneshot",
            f"SyslogIdentifier={health_unit}",
            f"TimeoutStartSec={service.healthcheck.timeout}",
            f"ExecStart={_systemd_exec(build_health_service_payload(service))}",
        ]
    )


def build_installed_health_timer_unit(project_name: str, service_name: str, service: Service) -> str | None:
    if service.healthcheck is None or service.healthcheck.disabled:
        return None

    unit = unit_name(project_name, service_name)
    health_unit = health_unit_name(project_name, service_name)
    return _unit_text(
        [
            "[Unit]",
            f"Description=systemd-compose healthcheck timer: {project_name} {service_name}",
            GENERATED_UNIT_MARKER,
            f"Requires={unit}.service",
            f"After={unit}.service",
            f"BindsTo={unit}.service",
            "",
            "[Timer]",
            f"OnActiveSec={service.healthcheck.start_period}",
            f"OnUnitActiveSec={service.healthcheck.interval}",
            f"Unit={health_unit}.service",
            "",
            "[Install]",
            f"WantedBy={unit_prefix(project_name).removesuffix('-')}.target",
        ]
    )


def build_health_service_payload(service: Service) -> list[str]:
    if service.healthcheck is None or service.healthcheck.disabled:
        raise SystemdComposeError(f"service {service.name!r} does not have an enabled healthcheck")
    health_service = Service(
        name=service.name,
        command=_healthcheck_argv(service.healthcheck),
        environment=service.environment,
        volumes=service.volumes,
        tmpfs=service.tmpfs,
        working_dir=service.working_dir,
    )
    return build_service_payload(health_service)


def build_description(project_name: str, service_name: str, service: Service) -> str:
    return (
        f"systemd-compose: {project_name} {service_name} "
        f"{DESCRIPTION_HASH_PREFIX}{service_definition_hash(project_name, service_name, service)}"
    )


def build_health_description(project_name: str, service_name: str, service: Service) -> str:
    return (
        f"systemd-compose healthcheck: {project_name} {service_name} "
        f"{DESCRIPTION_HASH_PREFIX}{health_definition_hash(project_name, service_name, service)}"
    )


def service_definition_hash(project_name: str, service_name: str, service: Service) -> str:
    data = {
        "unit": unit_name(project_name, service_name),
        "syslog_identifier": unit_name(project_name, service_name),
        "accounting": ACCOUNTING_PROPERTIES,
        "dependencies": [f"{unit_name(project_name, dependency)}.service" for dependency in service.depends_on],
        "restart": service.restart,
        "resources": build_resource_properties(service.resources),
        "healthcheck": health_definition_hash(project_name, service_name, service) if service.healthcheck else None,
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


def health_unit_name(project_name: str, service_name: str) -> str:
    return f"{unit_name(project_name, service_name)}-health"


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


def _systemd_exec(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def _unit_text(lines: list[str]) -> str:
    return "\n".join(line for line in lines if line != "") + "\n"


def health_definition_data(project_name: str, service_name: str, service: Service) -> dict[str, object] | None:
    if service.healthcheck is None or service.healthcheck.disabled:
        return None
    return {
        "unit": health_unit_name(project_name, service_name),
        "main_unit": unit_name(project_name, service_name),
        "test": service.healthcheck.test,
        "interval": service.healthcheck.interval,
        "timeout": service.healthcheck.timeout,
        "start_period": service.healthcheck.start_period,
        "retries": service.healthcheck.retries,
        "payload": build_health_service_payload(service),
    }


def health_definition_hash(project_name: str, service_name: str, service: Service) -> str | None:
    data = health_definition_data(project_name, service_name, service)
    if data is None:
        return None
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _healthcheck_argv(healthcheck: Healthcheck) -> list[str]:
    if isinstance(healthcheck.test, str):
        return ["/bin/sh", "-c", healthcheck.test]
    mode = healthcheck.test[0]
    if mode == "CMD":
        return list(healthcheck.test[1:])
    if mode == "CMD-SHELL":
        return ["/bin/sh", "-c", " ".join(healthcheck.test[1:])]
    raise SystemdComposeError("disabled healthchecks cannot be executed")
