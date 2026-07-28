from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Volume:
    host_path: str
    sandbox_path: str
    read_only: bool = False


@dataclass(frozen=True)
class Service:
    name: str
    command: str | list[str]
    environment: dict[str, str] = field(default_factory=dict)
    volumes: list[Volume] = field(default_factory=list)
    tmpfs: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    working_dir: str | None = None
    restart: str | None = None


@dataclass(frozen=True)
class ComposeConfig:
    services: dict[str, Service]
