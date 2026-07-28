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
