import pytest

from systemd_compose.errors import SystemdComposeError
from systemd_compose.parser import parse_compose_data


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
    assert web.tmpfs == ["/var/lib/nginx"]
    assert web.depends_on == ["db"]
    assert web.working_dir == "/app"
    assert web.restart == "on-failure"


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
