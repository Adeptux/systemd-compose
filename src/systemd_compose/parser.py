from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

import yaml

from systemd_compose.errors import SystemdComposeError
from systemd_compose.models import ComposeConfig, Healthcheck, Resources, Service, Tmpfs, Volume

RESOURCE_KEYS = {"mem_limit", "cpus", "pids_limit"}
MEMORY_UNITS = {"b": "", "k": "K", "m": "M", "g": "G", "t": "T", "p": "P", "e": "E"}
HEALTHCHECK_KEYS = {"test", "interval", "timeout", "start_period", "retries", "disable"}
SERVICE_NAME_INVALID_RE = re.compile(r"[^A-Za-z0-9_.@-]+")
INTERPOLATION_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")
RESTART_POLICIES = {
    "no",
    "always",
    "on-success",
    "on-failure",
    "on-abnormal",
    "on-watchdog",
    "on-abort",
}


def parse_compose_file(path: str | Path, *, env_file: str | Path | None = None) -> ComposeConfig:
    compose_path = Path(path)
    try:
        content = compose_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SystemdComposeError(f"compose file not found: {compose_path}") from exc

    variables = load_interpolation_variables(compose_path, env_file=env_file)
    try:
        loaded = yaml.safe_load(interpolate_content(content, variables, source=str(compose_path)))
    except yaml.YAMLError as exc:
        raise SystemdComposeError(f"invalid YAML in {compose_path}: {exc}") from exc

    return parse_compose_data(loaded, source=str(compose_path))


def load_interpolation_variables(compose_path: Path, *, env_file: str | Path | None) -> dict[str, str]:
    if env_file is None:
        default_env_file = compose_path.parent / ".env"
        env_values = parse_env_file(default_env_file, required=False)
    else:
        env_values = parse_env_file(Path(env_file), required=True)
    return {**env_values, **{key: value for key, value in os.environ.items()}}


def parse_env_file(path: Path, *, required: bool) -> dict[str, str]:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if required:
            raise SystemdComposeError(f"env file not found: {path}") from exc
        return {}

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise SystemdComposeError(f"{path}:{line_number}: expected KEY=VALUE")
        values[key] = parse_env_value(value.strip())
    return values


def parse_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    comment_index = value.find(" #")
    if comment_index != -1:
        value = value[:comment_index].rstrip()
    return value


def interpolate_content(content: str, variables: dict[str, str], *, source: str) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        default = match.group(2)
        if key in variables:
            return variables[key]
        if default is not None:
            return default
        raise SystemdComposeError(f"{source}: missing interpolation variable {key!r}")

    return INTERPOLATION_RE.sub(replace, content)


def parse_compose_data(data: Any, *, source: str = "<compose>") -> ComposeConfig:
    if not isinstance(data, dict):
        raise SystemdComposeError(f"{source}: expected a mapping at document root")

    project_name = _parse_project_name(data.get("name"), source)
    raw_services = data.get("services")
    if not isinstance(raw_services, dict) or not raw_services:
        raise SystemdComposeError(f"{source}: expected a non-empty services mapping")

    services: dict[str, Service] = {}
    for name, raw_service in raw_services.items():
        _validate_service_name(name, source)
        if not isinstance(raw_service, dict):
            raise SystemdComposeError(f"{source}: service {name!r} must be a mapping")

        command = raw_service.get("command")
        if not isinstance(command, str | list) or command == "":
            raise SystemdComposeError(f"{source}: service {name!r} requires command")
        if isinstance(command, list) and not command:
            raise SystemdComposeError(f"{source}: service {name!r} command list cannot be empty")
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
            restart=_parse_restart(raw_service.get("restart"), source, name),
            resources=_parse_resources(raw_service, source, name),
            healthcheck=_parse_healthcheck(raw_service.get("healthcheck"), source, name),
        )

    for service in services.values():
        for dependency in service.depends_on:
            if dependency not in services:
                raise SystemdComposeError(
                    f"{source}: service {service.name!r} depends on unknown service {dependency!r}"
                )

    return ComposeConfig(services=services, name=project_name)


def _parse_project_name(value: Any, source: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise SystemdComposeError(f"{source}: name must be a non-empty string")


def _validate_service_name(value: Any, source: str) -> None:
    if not isinstance(value, str) or not value:
        raise SystemdComposeError(f"{source}: service names must be non-empty strings")
    cleaned = SERVICE_NAME_INVALID_RE.sub("-", value.strip()).strip("-")
    if not cleaned:
        raise SystemdComposeError(
            f"{source}: service name {value!r} must contain at least one valid unit-name character"
        )


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


def _parse_tmpfs(value: Any, source: str, service_name: str) -> list[Tmpfs]:
    if value is None:
        return []
    if isinstance(value, str) and value:
        return [_parse_tmpfs_entry(value, source, service_name)]
    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return [_parse_tmpfs_entry(item, source, service_name) for item in value]
    raise SystemdComposeError(
        f"{source}: service {service_name!r} tmpfs must be a non-empty string or list of non-empty strings"
    )


def _parse_tmpfs_entry(value: str, source: str, service_name: str) -> Tmpfs:
    target, separator, raw_options = value.partition(":")
    if not target:
        raise SystemdComposeError(f"{source}: service {service_name!r} tmpfs target cannot be empty")
    size: int | None = None
    mode: str | None = None
    if not separator:
        return Tmpfs(target=target)
    if not raw_options:
        raise SystemdComposeError(f"{source}: service {service_name!r} tmpfs options cannot be empty")

    for raw_option in raw_options.split(","):
        key, option_separator, option_value = raw_option.partition("=")
        if not option_separator or not key or not option_value:
            raise SystemdComposeError(
                f"{source}: service {service_name!r} tmpfs options must be key=value pairs"
            )
        if key == "size":
            size = _parse_tmpfs_size(option_value, source, service_name)
        elif key == "mode":
            mode = _parse_tmpfs_mode(option_value, source, service_name)
        else:
            raise SystemdComposeError(
                f"{source}: service {service_name!r} tmpfs option {key!r} is not supported"
            )
    return Tmpfs(target=target, size=size, mode=mode)


def _parse_tmpfs_size(value: str, source: str, service_name: str) -> int:
    raw_size = value.strip()
    numeric_value = raw_size
    multiplier = 1
    if raw_size and raw_size[-1].isalpha():
        unit_key = raw_size[-1].lower()
        multipliers = {
            "b": 1,
            "k": 1024,
            "m": 1024**2,
            "g": 1024**3,
            "t": 1024**4,
            "p": 1024**5,
            "e": 1024**6,
        }
        if unit_key not in multipliers:
            raise SystemdComposeError(f"{source}: service {service_name!r} tmpfs size must be a positive size")
        numeric_value = raw_size[:-1]
        multiplier = multipliers[unit_key]
    try:
        parsed = float(numeric_value)
    except ValueError as exc:
        raise SystemdComposeError(f"{source}: service {service_name!r} tmpfs size must be a positive size") from exc
    if parsed <= 0:
        raise SystemdComposeError(f"{source}: service {service_name!r} tmpfs size must be a positive size")
    return int(parsed * multiplier)


def _parse_tmpfs_mode(value: str, source: str, service_name: str) -> str:
    if not re.fullmatch(r"0?[0-7]{3,4}", value):
        raise SystemdComposeError(
            f"{source}: service {service_name!r} tmpfs mode must be an octal mode"
        )
    normalized = value if value.startswith("0") else f"0{value}"
    if len(normalized) > 5:
        raise SystemdComposeError(
            f"{source}: service {service_name!r} tmpfs mode must be an octal mode"
        )
    return normalized


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


def _parse_restart(value: Any, source: str, service_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SystemdComposeError(f"{source}: service {service_name!r} restart must be a non-empty string")
    if value in RESTART_POLICIES:
        return value
    if value == "unless-stopped":
        raise SystemdComposeError(
            f"{source}: service {service_name!r} restart value 'unless-stopped' is Docker Compose syntax "
            "and has no direct systemd equivalent"
        )
    supported = ", ".join(sorted(RESTART_POLICIES))
    raise SystemdComposeError(
        f"{source}: service {service_name!r} restart must be one of: {supported}"
    )


def _parse_resources(raw_service: dict[str, Any], source: str, service_name: str) -> Resources | None:
    if "resources" in raw_service:
        raise SystemdComposeError(
            f"{source}: service {service_name!r} resources is not supported; "
            "use mem_limit, cpus, or pids_limit"
        )

    values = {key: raw_service.get(key) for key in RESOURCE_KEYS if key in raw_service}
    if not values:
        return None

    return Resources(
        mem_limit=_parse_mem_limit(values.get("mem_limit"), source, service_name),
        cpus=_parse_cpus(values.get("cpus"), source, service_name),
        pids_limit=_parse_pids_limit(values.get("pids_limit"), source, service_name),
    )


def _parse_mem_limit(value: Any, source: str, service_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and value.strip():
        mem_limit = value.strip()
        numeric_value = mem_limit
        unit = ""
        if mem_limit[-1].isalpha():
            unit_key = mem_limit[-1].lower()
            if unit_key not in MEMORY_UNITS:
                raise SystemdComposeError(
                    f"{source}: service {service_name!r} resources.mem_limit must be a positive size"
                )
            numeric_value = mem_limit[:-1]
            unit = MEMORY_UNITS[unit_key]
        try:
            parsed = float(numeric_value)
        except ValueError as exc:
            raise SystemdComposeError(
                f"{source}: service {service_name!r} resources.mem_limit must be a positive size"
            ) from exc
        if parsed > 0:
            return f"{numeric_value}{unit}"
    raise SystemdComposeError(
        f"{source}: service {service_name!r} resources.mem_limit must be a positive size"
    )


def _parse_cpus(value: Any, source: str, service_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and value.strip():
        cpus = value.strip()
        numeric_value = cpus[:-1] if cpus.endswith("%") else cpus
        try:
            parsed = float(numeric_value)
        except ValueError as exc:
            raise SystemdComposeError(
                f"{source}: service {service_name!r} resources.cpus must be a positive number or percentage"
            ) from exc
        if parsed > 0:
            return cpus
    raise SystemdComposeError(
        f"{source}: service {service_name!r} resources.cpus must be a positive number or percentage"
    )


def _parse_pids_limit(value: Any, source: str, service_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raise SystemdComposeError(
        f"{source}: service {service_name!r} resources.pids_limit must be a positive integer"
    )


def _parse_healthcheck(value: Any, source: str, service_name: str) -> Healthcheck | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SystemdComposeError(f"{source}: service {service_name!r} healthcheck must be a mapping")

    unknown_keys = sorted(str(key) for key in value if key not in HEALTHCHECK_KEYS)
    if unknown_keys:
        keys = ", ".join(unknown_keys)
        raise SystemdComposeError(f"{source}: service {service_name!r} healthcheck has unknown key(s): {keys}")

    disabled = _parse_optional_bool(value.get("disable"), "healthcheck.disable", source, service_name)
    raw_test = value.get("test")
    if disabled:
        return Healthcheck(test=["NONE"], disabled=True)
    if raw_test is None:
        raise SystemdComposeError(f"{source}: service {service_name!r} healthcheck.test is required")
    test = _parse_healthcheck_test(raw_test, source, service_name)
    if isinstance(test, list) and test[0] == "NONE":
        return Healthcheck(test=test, disabled=True)

    return Healthcheck(
        test=test,
        interval=_parse_duration(value.get("interval"), "healthcheck.interval", source, service_name, default="30s"),
        timeout=_parse_duration(value.get("timeout"), "healthcheck.timeout", source, service_name, default="30s"),
        start_period=_parse_duration(
            value.get("start_period"), "healthcheck.start_period", source, service_name, default="0s"
        ),
        retries=_parse_positive_int(value.get("retries"), "healthcheck.retries", source, service_name, default=3),
    )


def _parse_healthcheck_test(value: Any, source: str, service_name: str) -> str | list[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
        mode = value[0]
        if mode not in {"CMD", "CMD-SHELL", "NONE"}:
            raise SystemdComposeError(
                f"{source}: service {service_name!r} healthcheck.test list must start with CMD, CMD-SHELL, or NONE"
            )
        if mode == "NONE" and len(value) != 1:
            raise SystemdComposeError(
                f"{source}: service {service_name!r} healthcheck.test NONE must not include a command"
            )
        if mode in {"CMD", "CMD-SHELL"} and len(value) < 2:
            raise SystemdComposeError(
                f"{source}: service {service_name!r} healthcheck.test {mode} requires a command"
            )
        return list(value)
    raise SystemdComposeError(
        f"{source}: service {service_name!r} healthcheck.test must be a non-empty string or list"
    )


def _parse_duration(value: Any, key: str, source: str, service_name: str, *, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
        return f"{value:g}s"
    if isinstance(value, str) and value.strip():
        duration = value.strip()
        suffixes = ("ms", "s", "m", "h")
        numeric_value = duration
        for suffix in suffixes:
            if duration.endswith(suffix):
                numeric_value = duration.removesuffix(suffix)
                break
        try:
            parsed = float(numeric_value)
        except ValueError as exc:
            raise SystemdComposeError(
                f"{source}: service {service_name!r} {key} must be a non-negative duration"
            ) from exc
        if parsed >= 0:
            return duration
    raise SystemdComposeError(f"{source}: service {service_name!r} {key} must be a non-negative duration")


def _parse_positive_int(value: Any, key: str, source: str, service_name: str, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raise SystemdComposeError(f"{source}: service {service_name!r} {key} must be a positive integer")


def _parse_optional_bool(value: Any, key: str, source: str, service_name: str) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise SystemdComposeError(f"{source}: service {service_name!r} {key} must be a boolean")
