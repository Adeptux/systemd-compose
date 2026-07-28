from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from systemd_compose.errors import SystemdComposeError
from systemd_compose.models import ComposeConfig, Service, Volume


def parse_compose_file(path: str | Path) -> ComposeConfig:
    compose_path = Path(path)
    try:
        loaded = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemdComposeError(f"compose file not found: {compose_path}") from exc
    except yaml.YAMLError as exc:
        raise SystemdComposeError(f"invalid YAML in {compose_path}: {exc}") from exc

    return parse_compose_data(loaded, source=str(compose_path))


def parse_compose_data(data: Any, *, source: str = "<compose>") -> ComposeConfig:
    if not isinstance(data, dict):
        raise SystemdComposeError(f"{source}: expected a mapping at document root")

    raw_services = data.get("services")
    if not isinstance(raw_services, dict) or not raw_services:
        raise SystemdComposeError(f"{source}: expected a non-empty services mapping")

    services: dict[str, Service] = {}
    for name, raw_service in raw_services.items():
        if not isinstance(name, str) or not name:
            raise SystemdComposeError(f"{source}: service names must be non-empty strings")
        if not isinstance(raw_service, dict):
            raise SystemdComposeError(f"{source}: service {name!r} must be a mapping")

        command = raw_service.get("command")
        if not isinstance(command, str | list) or command == "":
            raise SystemdComposeError(f"{source}: service {name!r} requires command")
        if isinstance(command, list) and not all(isinstance(part, str) for part in command):
            raise SystemdComposeError(f"{source}: service {name!r} command list must contain strings")

        services[name] = Service(
            name=name,
            command=command,
            environment=_parse_environment(raw_service.get("environment"), source, name),
            volumes=_parse_volumes(raw_service.get("volumes"), source, name),
            tmpfs=_parse_tmpfs(raw_service.get("tmpfs"), source, name),
            depends_on=_parse_depends_on(raw_service.get("depends_on"), source, name),
            working_dir=_optional_string(raw_service, "working_dir", source, name),
            restart=_optional_string(raw_service, "restart", source, name),
        )

    for service in services.values():
        for dependency in service.depends_on:
            if dependency not in services:
                raise SystemdComposeError(
                    f"{source}: service {service.name!r} depends on unknown service {dependency!r}"
                )

    return ComposeConfig(services=services)


def _parse_environment(value: Any, source: str, service_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    if isinstance(value, list):
        environment: dict[str, str] = {}
        for item in value:
            if not isinstance(item, str) or "=" not in item:
                raise SystemdComposeError(
                    f"{source}: service {service_name!r} environment entries must be KEY=VALUE strings"
                )
            key, item_value = item.split("=", 1)
            if not key:
                raise SystemdComposeError(
                    f"{source}: service {service_name!r} environment key cannot be empty"
                )
            environment[key] = item_value
        return environment
    raise SystemdComposeError(
        f"{source}: service {service_name!r} environment must be a mapping or list"
    )


def _parse_volumes(value: Any, source: str, service_name: str) -> list[Volume]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SystemdComposeError(f"{source}: service {service_name!r} volumes must be a list")

    volumes: list[Volume] = []
    for item in value:
        if not isinstance(item, str):
            raise SystemdComposeError(
                f"{source}: service {service_name!r} volume entries must be strings"
            )
        parts = item.split(":")
        if len(parts) not in {2, 3} or not parts[0] or not parts[1]:
            raise SystemdComposeError(
                f"{source}: service {service_name!r} volume must be HOST:SANDBOX[:ro]"
            )
        if len(parts) == 3 and parts[2] != "ro":
            raise SystemdComposeError(
                f"{source}: service {service_name!r} only :ro volume mode is supported"
            )
        volumes.append(Volume(host_path=parts[0], sandbox_path=parts[1], read_only=len(parts) == 3))
    return volumes


def _parse_tmpfs(value: Any, source: str, service_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return list(value)
    raise SystemdComposeError(
        f"{source}: service {service_name!r} tmpfs must be a non-empty string or list of non-empty strings"
    )


def _parse_depends_on(value: Any, source: str, service_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return list(value)
    raise SystemdComposeError(
        f"{source}: service {service_name!r} depends_on must be a list or mapping"
    )


def _optional_string(raw_service: dict[str, Any], key: str, source: str, service_name: str) -> str | None:
    value = raw_service.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    raise SystemdComposeError(f"{source}: service {service_name!r} {key} must be a non-empty string")
