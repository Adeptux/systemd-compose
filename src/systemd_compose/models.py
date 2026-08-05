from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Volume:
    host_path: str
    sandbox_path: str
    read_only: bool = False


@dataclass(frozen=True)
class Tmpfs:
    target: str
    size: int | None = None
    mode: str | None = None


@dataclass(frozen=True)
class Resources:
    mem_limit: str | None = None
    cpus: str | None = None
    pids_limit: int | None = None


@dataclass(frozen=True)
class Healthcheck:
    test: str | list[str]
    interval: str = "30s"
    timeout: str = "30s"
    start_period: str = "0s"
    retries: int = 3
    disabled: bool = False


@dataclass(frozen=True)
class Service:
    name: str
    command: str | list[str]
    environment: dict[str, str] = field(default_factory=dict)
    volumes: list[Volume] = field(default_factory=list)
    tmpfs: list[Tmpfs] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    working_dir: str | None = None
    restart: str | None = None
    resources: Resources | None = None
    healthcheck: Healthcheck | None = None


@dataclass(frozen=True)
class ComposeConfig:
    services: dict[str, Service]
    name: str | None = None
