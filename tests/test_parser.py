import pytest

from systemd_compose.errors import SystemdComposeError
from systemd_compose.parser import parse_compose_data


def test_parse_compose_data_supports_core_service_fields():
    config = parse_compose_data(
        {
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
