import pytest

from systemd_compose.errors import SystemdComposeError
from systemd_compose.parser import parse_compose_data, parse_compose_file


def test_parse_compose_data_supports_core_service_fields():
    config = parse_compose_data(
        {
            "name": "demo",
            "services": {
                "web": {
                    "command": "python -m http.server 8000",
                    "environment": {"PORT": 8000},
                    "volumes": ["/srv/site:/app:ro"],
                    "tmpfs": ["/var/lib/nginx"],
                    "depends_on": ["db"],
                    "working_dir": "/app",
                    "restart": "on-failure",
                },
                "db": {
                    "command": ["postgres", "-D", "/var/lib/postgresql/data"],
                    "environment": ["PGDATA=/var/lib/postgresql/data"],
                },
            }
        }
    )

    assert config.name == "demo"
    web = config.services["web"]

    assert web.command == "python -m http.server 8000"
    assert web.environment == {"PORT": "8000"}
    assert web.volumes[0].host_path == "/srv/site"
    assert web.volumes[0].sandbox_path == "/app"
    assert web.volumes[0].read_only is True
    assert web.tmpfs[0].target == "/var/lib/nginx"
    assert web.tmpfs[0].size is None
    assert web.tmpfs[0].mode is None
    assert web.depends_on == ["db"]
    assert web.working_dir == "/app"
    assert web.restart == "on-failure"


def test_parse_compose_file_interpolates_default_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    compose_file = tmp_path / "systemd-compose.yaml"
    compose_file.write_text(
        """
services:
  web:
    command: "python -m http.server ${PORT}"
    environment:
      FROM_ENV_FILE: "${PORT}"
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("PORT=9000\n", encoding="utf-8")

    config = parse_compose_file(compose_file)

    assert config.services["web"].command == "python -m http.server 9000"
    assert config.services["web"].environment == {"FROM_ENV_FILE": "9000"}


def test_parse_compose_file_uses_explicit_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    compose_file = tmp_path / "systemd-compose.yaml"
    env_file = tmp_path / "custom.env"
    compose_file.write_text(
        """
services:
  web:
    command: "python -m http.server ${PORT}"
""".lstrip(),
        encoding="utf-8",
    )
    env_file.write_text("PORT=7000\n", encoding="utf-8")

    config = parse_compose_file(compose_file, env_file=env_file)

    assert config.services["web"].command == "python -m http.server 7000"


def test_parse_compose_file_process_environment_overrides_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "6000")
    compose_file = tmp_path / "systemd-compose.yaml"
    compose_file.write_text(
        """
services:
  web:
    command: "python -m http.server ${PORT}"
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("PORT=9000\n", encoding="utf-8")

    config = parse_compose_file(compose_file)

    assert config.services["web"].command == "python -m http.server 6000"


def test_parse_compose_file_supports_interpolation_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    compose_file = tmp_path / "systemd-compose.yaml"
    compose_file.write_text(
        """
services:
  web:
    command: "python -m http.server ${PORT:-8080}"
""".lstrip(),
        encoding="utf-8",
    )

    config = parse_compose_file(compose_file)

    assert config.services["web"].command == "python -m http.server 8080"


def test_parse_compose_file_rejects_missing_interpolation_variables(tmp_path, monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    compose_file = tmp_path / "systemd-compose.yaml"
    compose_file.write_text(
        """
services:
  web:
    command: "python -m http.server ${PORT}"
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(SystemdComposeError, match="missing interpolation variable 'PORT'"):
        parse_compose_file(compose_file)


def test_parse_compose_file_rejects_missing_explicit_env_file(tmp_path):
    compose_file = tmp_path / "systemd-compose.yaml"
    compose_file.write_text(
        """
services:
  web:
    command: "true"
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(SystemdComposeError, match="env file not found"):
        parse_compose_file(compose_file, env_file=tmp_path / "missing.env")


def test_parse_compose_file_rejects_malformed_env_file(tmp_path):
    compose_file = tmp_path / "systemd-compose.yaml"
    env_file = tmp_path / ".env"
    compose_file.write_text(
        """
services:
  web:
    command: "true"
""".lstrip(),
        encoding="utf-8",
    )
    env_file.write_text("export PORT=9000\n", encoding="utf-8")

    with pytest.raises(SystemdComposeError, match="expected KEY=VALUE"):
        parse_compose_file(compose_file, env_file=env_file)


def test_env_file_values_are_not_automatically_injected_into_service_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    compose_file = tmp_path / "systemd-compose.yaml"
    compose_file.write_text(
        """
services:
  web:
    command: "python -m http.server ${PORT}"
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("PORT=9000\n", encoding="utf-8")

    config = parse_compose_file(compose_file)

    assert config.services["web"].environment == {}


@pytest.mark.parametrize("name", ["", "   ", 123, True])
def test_parse_compose_data_rejects_invalid_name(name):
    with pytest.raises(SystemdComposeError, match="name must be a non-empty string"):
        parse_compose_data(
            {
                "name": name,
                "services": {
                    "web": {
                        "command": "python -m http.server 8000",
                    },
                },
            }
        )


def test_parse_compose_data_supports_docker_like_resources():
    config = parse_compose_data(
        {
            "services": {
                "web": {
                    "command": "python -m http.server 8000",
                    "mem_limit": "512m",
                    "cpus": 0.5,
                    "pids_limit": 128,
                },
            }
        }
    )

    resources = config.services["web"].resources

    assert resources is not None
    assert resources.mem_limit == "512M"
    assert resources.cpus == "0.5"
    assert resources.pids_limit == 128


def test_parse_compose_data_supports_docker_like_healthcheck():
    config = parse_compose_data(
        {
            "services": {
                "web": {
                    "command": "python -m http.server 8000",
                    "healthcheck": {
                        "test": ["CMD", "curl", "-f", "http://127.0.0.1:8000"],
                        "interval": "10s",
                        "timeout": "2s",
                        "start_period": "5s",
                        "retries": 2,
                    },
                },
            }
        }
    )

    healthcheck = config.services["web"].healthcheck

    assert healthcheck is not None
    assert healthcheck.test == ["CMD", "curl", "-f", "http://127.0.0.1:8000"]
    assert healthcheck.interval == "10s"
    assert healthcheck.timeout == "2s"
    assert healthcheck.start_period == "5s"
    assert healthcheck.retries == 2
    assert healthcheck.disabled is False


@pytest.mark.parametrize(
    "restart",
    [
        "no",
        "always",
        "on-success",
        "on-failure",
        "on-abnormal",
        "on-watchdog",
        "on-abort",
    ],
)
def test_parse_compose_data_supports_systemd_restart_policies(restart):
    config = parse_compose_data(
        {
            "services": {
                "web": {
                    "command": "python -m http.server 8000",
                    "restart": restart,
                },
            }
        }
    )

    assert config.services["web"].restart == restart


@pytest.mark.parametrize(
    ("restart", "message"),
    [
        ("unless-stopped", "Docker Compose syntax"),
        ("sometimes", "restart must be one of"),
        ("", "restart must be a non-empty string"),
        (True, "restart must be a non-empty string"),
    ],
)
def test_parse_compose_data_rejects_invalid_restart_policy(restart, message):
    with pytest.raises(SystemdComposeError, match=message):
        parse_compose_data(
            {
                "services": {
                    "web": {
                        "command": "python -m http.server 8000",
                        "restart": restart,
                    },
                }
            }
        )


def test_parse_compose_data_supports_disabled_healthcheck():
    config = parse_compose_data(
        {
            "services": {
                "web": {
                    "command": "python -m http.server 8000",
                    "healthcheck": {"disable": True},
                },
            }
        }
    )

    healthcheck = config.services["web"].healthcheck

    assert healthcheck is not None
    assert healthcheck.disabled is True


def test_parse_compose_data_supports_none_healthcheck_test():
    config = parse_compose_data(
        {
            "services": {
                "web": {
                    "command": "python -m http.server 8000",
                    "healthcheck": {"test": ["NONE"]},
                },
            }
        }
    )

    healthcheck = config.services["web"].healthcheck

    assert healthcheck is not None
    assert healthcheck.disabled is True


@pytest.mark.parametrize(
    ("service_name", "message"),
    [
        ("", "service names must be non-empty strings"),
        (123, "service names must be non-empty strings"),
        ("!!!", "must contain at least one valid unit-name character"),
    ],
)
def test_parse_compose_data_rejects_invalid_service_names(service_name, message):
    with pytest.raises(SystemdComposeError, match=message):
        parse_compose_data(
            {
                "services": {
                    service_name: {
                        "command": "python -m http.server 8000",
                    },
                }
            }
        )


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ([], "command list cannot be empty"),
        (["python", 8000], "command list must contain strings"),
    ],
)
def test_parse_compose_data_rejects_invalid_command_lists(command, message):
    with pytest.raises(SystemdComposeError, match=message):
        parse_compose_data(
            {
                "services": {
                    "web": {
                        "command": command,
                    },
                }
            }
        )


@pytest.mark.parametrize(
    ("volumes", "message"),
    [
        ("/srv/site:/app", "volumes must be a list"),
        ([123], "volume entries must be strings"),
        (["/srv/site"], "volume must be HOST:SANDBOX"),
        ([":/app"], "volume must be HOST:SANDBOX"),
        (["/srv/site:/app:rw"], "only :ro volume mode is supported"),
    ],
)
def test_parse_compose_data_rejects_invalid_volumes(volumes, message):
    with pytest.raises(SystemdComposeError, match=message):
        parse_compose_data(
            {
                "services": {
                    "web": {
                        "command": "python -m http.server 8000",
                        "volumes": volumes,
                    },
                }
            }
        )


@pytest.mark.parametrize(
    "tmpfs",
    ["", ["/run", ""], [123], {"target": "/run"}],
)
def test_parse_compose_data_rejects_invalid_tmpfs(tmpfs):
    with pytest.raises(SystemdComposeError, match="tmpfs must be a non-empty string"):
        parse_compose_data(
            {
                "services": {
                    "web": {
                        "command": "python -m http.server 8000",
                        "tmpfs": tmpfs,
                    },
                }
            }
        )


def test_parse_compose_data_supports_tmpfs_options():
    config = parse_compose_data(
        {
            "services": {
                "web": {
                    "command": "python -m http.server 8000",
                    "tmpfs": [
                        "/run/cache:size=256m,mode=1777",
                        "/run/private:mode=700,size=1k",
                    ],
                },
            }
        }
    )

    cache, private = config.services["web"].tmpfs

    assert cache.target == "/run/cache"
    assert cache.size == 256 * 1024 * 1024
    assert cache.mode == "01777"
    assert private.target == "/run/private"
    assert private.size == 1024
    assert private.mode == "0700"


@pytest.mark.parametrize(
    ("tmpfs", "message"),
    [
        (":size=1m", "tmpfs target cannot be empty"),
        ("/run/cache:", "tmpfs options cannot be empty"),
        ("/run/cache:size", "tmpfs options must be key=value pairs"),
        ("/run/cache:size=0", "tmpfs size must be a positive size"),
        ("/run/cache:size=bad", "tmpfs size must be a positive size"),
        ("/run/cache:mode=888", "tmpfs mode must be an octal mode"),
        ("/run/cache:uid=1000", "tmpfs option 'uid' is not supported"),
    ],
)
def test_parse_compose_data_rejects_invalid_tmpfs_options(tmpfs, message):
    with pytest.raises(SystemdComposeError, match=message):
        parse_compose_data(
            {
                "services": {
                    "web": {
                        "command": "python -m http.server 8000",
                        "tmpfs": [tmpfs],
                    },
                }
            }
        )


def test_parse_compose_data_rejects_unknown_dependencies():
    with pytest.raises(SystemdComposeError, match="depends on unknown service 'db'"):
        parse_compose_data(
            {
                "services": {
                    "web": {
                        "command": "python -m http.server 8000",
                        "depends_on": ["db"],
                    },
                }
            }
        )


@pytest.mark.parametrize(
    ("service_values", "message"),
    [
        ({"cpus": "half"}, "resources.cpus must be a positive number or percentage"),
        ({"cpus": 0}, "resources.cpus must be a positive number or percentage"),
        ({"mem_limit": "half"}, "resources.mem_limit must be a positive size"),
        ({"mem_limit": 0}, "resources.mem_limit must be a positive size"),
        ({"pids_limit": "128"}, "resources.pids_limit must be a positive integer"),
        ({"pids_limit": True}, "resources.pids_limit must be a positive integer"),
        ({"resources": {"mem_limit": "512M"}}, "resources is not supported"),
    ],
)
def test_parse_compose_data_rejects_invalid_resources(service_values, message):
    service = {
        "command": "python -m http.server 8000",
        **service_values,
    }

    with pytest.raises(SystemdComposeError, match=message):
        parse_compose_data(
            {
                "services": {
                    "web": service,
                }
            }
        )


@pytest.mark.parametrize(
    ("healthcheck", "message"),
    [
        ("curl -f http://127.0.0.1:8000", "healthcheck must be a mapping"),
        ({}, "healthcheck.test is required"),
        ({"test": ["SHELL", "curl -f http://127.0.0.1:8000"]}, "must start with CMD"),
        ({"test": ["CMD"]}, "CMD requires a command"),
        ({"test": ["NONE", "curl"]}, "NONE must not include a command"),
        ({"test": "true", "interval": "-1s"}, "healthcheck.interval must be a non-negative duration"),
        ({"test": "true", "retries": 0}, "healthcheck.retries must be a positive integer"),
        ({"test": "true", "disable": "yes"}, "healthcheck.disable must be a boolean"),
    ],
)
def test_parse_compose_data_rejects_invalid_healthcheck(healthcheck, message):
    with pytest.raises(SystemdComposeError, match=message):
        parse_compose_data(
            {
                "services": {
                    "web": {
                        "command": "python -m http.server 8000",
                        "healthcheck": healthcheck,
                    },
                }
            }
        )
