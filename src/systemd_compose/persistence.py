from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from systemd_compose.builders import (
    GENERATED_UNIT_MARKER,
    build_installed_health_service_unit,
    build_installed_health_timer_unit,
    build_installed_service_unit,
    build_project_target_unit,
    health_unit_name,
    unit_name,
    unit_prefix,
)
from systemd_compose.errors import SystemdComposeError
from systemd_compose.models import Service

SYSTEM_UNIT_DIR = Path("/etc/systemd/system")


@dataclass(frozen=True)
class UnitFileChange:
    path: Path
    action: str


def unit_dir(*, system: bool = False) -> Path:
    if system:
        return SYSTEM_UNIT_DIR
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def target_unit_name(project_name: str) -> str:
    return f"{unit_prefix(project_name).removesuffix('-')}.target"


def target_unit_path(project_name: str, *, system: bool = False) -> Path:
    return unit_dir(system=system) / target_unit_name(project_name)


def project_is_installed(project_name: str, *, system: bool = False) -> bool:
    return _is_generated_unit(target_unit_path(project_name, system=system))


def desired_unit_files(
    project_name: str,
    services: dict[str, Service],
    *,
    system: bool = False,
) -> dict[str, str]:
    wanted_units = [f"{unit_name(project_name, service_name)}.service" for service_name in services]
    for service_name, service in services.items():
        if service.healthcheck is not None and not service.healthcheck.disabled:
            wanted_units.append(f"{health_unit_name(project_name, service_name)}.timer")
    files = {target_unit_name(project_name): build_project_target_unit(project_name, wanted_units, system=system)}
    for service_name, service in services.items():
        files[f"{unit_name(project_name, service_name)}.service"] = build_installed_service_unit(
            project_name,
            service_name,
            service,
        )
        health_service = build_installed_health_service_unit(project_name, service_name, service)
        health_timer = build_installed_health_timer_unit(project_name, service_name, service)
        if health_service is not None:
            files[f"{health_unit_name(project_name, service_name)}.service"] = health_service
        if health_timer is not None:
            files[f"{health_unit_name(project_name, service_name)}.timer"] = health_timer
    return files


def sync_unit_files(
    project_name: str,
    services: dict[str, Service],
    *,
    system: bool = False,
) -> list[UnitFileChange]:
    directory = unit_dir(system=system)
    directory.mkdir(parents=True, exist_ok=True)
    changes: list[UnitFileChange] = []
    for filename, content in desired_unit_files(project_name, services, system=system).items():
        path = directory / filename
        previous = path.read_text(encoding="utf-8") if path.exists() else None
        if previous == content:
            continue
        path.write_text(content, encoding="utf-8")
        changes.append(UnitFileChange(path, "created" if previous is None else "updated"))
    return changes


def remove_unit_files(project_name: str, *, system: bool = False) -> list[UnitFileChange]:
    changes: list[UnitFileChange] = []
    for path in installed_project_unit_files(project_name, system=system):
        path.unlink()
        changes.append(UnitFileChange(path, "removed"))
    return changes


def remove_orphan_unit_files(
    project_name: str,
    current_services: set[str],
    *,
    system: bool = False,
) -> list[UnitFileChange]:
    changes: list[UnitFileChange] = []
    for path in installed_orphan_unit_files(project_name, current_services, system=system):
        path.unlink()
        changes.append(UnitFileChange(path, "removed"))
    return changes


def installed_orphan_unit_files(
    project_name: str,
    current_services: set[str],
    *,
    system: bool = False,
) -> list[Path]:
    expected = {target_unit_name(project_name)}
    for service_name in current_services:
        expected.add(f"{unit_name(project_name, service_name)}.service")
        expected.add(f"{health_unit_name(project_name, service_name)}.service")
        expected.add(f"{health_unit_name(project_name, service_name)}.timer")
    return [
        path
        for path in installed_project_unit_files(project_name, system=system)
        if path.name not in expected
    ]


def installed_project_unit_files(project_name: str, *, system: bool = False) -> list[Path]:
    directory = unit_dir(system=system)
    if not directory.exists():
        return []
    prefix = unit_prefix(project_name)
    target_name = target_unit_name(project_name)
    paths = [
        path
        for path in directory.iterdir()
        if (
            path.is_file()
            and (path.name == target_name or path.name.startswith(prefix))
            and path.suffix in {".service", ".target", ".timer"}
            and _is_generated_unit(path)
        )
    ]
    return sorted(paths)


def installed_orphan_unit_names(
    project_name: str,
    current_services: set[str],
    *,
    system: bool = False,
) -> list[str]:
    return [path.name for path in installed_orphan_unit_files(project_name, current_services, system=system)]


def systemctl_command(*, system: bool = False) -> list[str]:
    return ["systemctl"] if system else ["systemctl", "--user"]


def require_system_privileges(*, system: bool = False) -> None:
    if system and hasattr(os, "geteuid") and os.geteuid() != 0:
        raise SystemdComposeError("--system must be run as root")


def _is_generated_unit(path: Path) -> bool:
    try:
        return GENERATED_UNIT_MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False
