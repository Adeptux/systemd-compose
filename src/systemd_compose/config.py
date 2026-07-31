from __future__ import annotations

import os
from pathlib import Path

from systemd_compose.errors import SystemdComposeError
from systemd_compose.models import Service

DEFAULT_COMPOSE_FILE = "systemd-compose.yaml"
DEFAULT_COMPOSE_FILES = (DEFAULT_COMPOSE_FILE, "systemd-compose.yml")


def resolve_project_name(project_name: str | None, config_project_name: str | None = None) -> str:
    if project_name:
        return project_name
    if config_project_name:
        return config_project_name
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
