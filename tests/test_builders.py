from systemd_compose.builders import (
    build_bwrap_args,
    build_health_service_payload,
    build_health_systemd_run_command,
    build_service_payload,
    build_systemd_run_command,
    service_definition_hash,
)
from systemd_compose.models import Healthcheck, Resources, Service, Volume


def test_service_payload_always_starts_with_bwrap():
    service = Service(name="web", command="python -m http.server")

    assert build_service_payload(service)[:2] == ["/usr/bin/bwrap", "--dev-bind"]


def test_bwrap_args_include_mounts_environment_workdir_and_command():
    service = Service(
        name="web",
        command="python -m http.server 8000",
        environment={"PORT": "8000"},
        volumes=[
            Volume("/srv/site", "/app", read_only=True),
            Volume("/var/cache/site", "/cache"),
        ],
        tmpfs=["/var/lib/nginx"],
        working_dir="/app",
    )

    args = build_bwrap_args(service)

    assert ["--ro-bind", "/srv/site", "/app"] == args[args.index("/srv/site") - 1 : args.index("/app") + 1]
    assert ["--bind", "/var/cache/site", "/cache"] == args[
        args.index("/var/cache/site") - 1 : args.index("/cache") + 1
    ]
    assert ["--tmpfs", "/var/lib/nginx"] == args[
        args.index("--tmpfs", args.index("/cache")) : args.index("/var/lib/nginx") + 1
    ]
    assert ["--setenv", "PORT", "8000"] == args[args.index("PORT") - 1 : args.index("8000") + 1]
    assert ["--chdir", "/app"] == args[args.index("--chdir") : args.index("--chdir") + 2]
    assert args[-5:] == ["--", "python", "-m", "http.server", "8000"]


def test_bwrap_base_args_do_not_include_unsupported_sysfs_option():
    args = build_bwrap_args(Service(name="smoke", command="true"))

    assert "--sysfs" not in args
    assert ["--ro-bind", "/sys", "/sys"] == args[args.index("/sys") - 1 : args.index("/sys") + 2]


def test_systemd_run_command_includes_dependencies_restart_and_bwrap_payload():
    service = Service(
        name="web",
        command="python -m http.server",
        depends_on=["db"],
        restart="on-failure",
    )

    command = build_systemd_run_command("demo", "web", service)

    expected_hash = service_definition_hash("demo", "web", service)
    assert command[:3] == [
        "systemd-run",
        "--user",
        "--unit=demo-web",
    ]
    assert command[3] == f"--description=systemd-compose: demo web systemd-compose-hash={expected_hash}"
    assert ["-p", "SyslogIdentifier=demo-web"] == command[
        command.index("SyslogIdentifier=demo-web") - 1 : command.index("SyslogIdentifier=demo-web") + 1
    ]
    assert ["-p", "CPUAccounting=yes"] == command[
        command.index("CPUAccounting=yes") - 1 : command.index("CPUAccounting=yes") + 1
    ]
    assert ["-p", "MemoryAccounting=yes"] == command[
        command.index("MemoryAccounting=yes") - 1 : command.index("MemoryAccounting=yes") + 1
    ]
    assert ["-p", "TasksAccounting=yes"] == command[
        command.index("TasksAccounting=yes") - 1 : command.index("TasksAccounting=yes") + 1
    ]
    assert ["-p", "IOAccounting=yes"] == command[
        command.index("IOAccounting=yes") - 1 : command.index("IOAccounting=yes") + 1
    ]
    assert ["-p", "IPAccounting=yes"] == command[
        command.index("IPAccounting=yes") - 1 : command.index("IPAccounting=yes") + 1
    ]
    assert ["-p", "Requires=demo-db.service"] == command[
        command.index("Requires=demo-db.service") - 1 : command.index("Requires=demo-db.service") + 1
    ]
    assert ["-p", "After=demo-db.service"] == command[
        command.index("After=demo-db.service") - 1 : command.index("After=demo-db.service") + 1
    ]
    assert ["-p", "BindsTo=demo-db.service"] == command[
        command.index("BindsTo=demo-db.service") - 1 : command.index("BindsTo=demo-db.service") + 1
    ]
    assert ["-p", "Restart=on-failure"] == command[
        command.index("Restart=on-failure") - 1 : command.index("Restart=on-failure") + 1
    ]
    assert "/usr/bin/bwrap" in command


def test_systemd_run_command_includes_resource_limits():
    service = Service(
        name="web",
        command="python -m http.server",
        resources=Resources(mem_limit="512M", cpus="0.5", pids_limit=128),
    )

    command = build_systemd_run_command("demo", "web", service)

    assert ["-p", "MemoryMax=512M"] == command[
        command.index("MemoryMax=512M") - 1 : command.index("MemoryMax=512M") + 1
    ]
    assert ["-p", "CPUQuota=50%"] == command[
        command.index("CPUQuota=50%") - 1 : command.index("CPUQuota=50%") + 1
    ]
    assert ["-p", "TasksMax=128"] == command[
        command.index("TasksMax=128") - 1 : command.index("TasksMax=128") + 1
    ]


def test_service_definition_hash_changes_when_resource_limits_change():
    base_service = Service(name="web", command="python -m http.server")
    limited_service = Service(
        name="web",
        command="python -m http.server",
        resources=Resources(mem_limit="512M"),
    )

    assert service_definition_hash("demo", "web", base_service) != service_definition_hash(
        "demo", "web", limited_service
    )


def test_health_systemd_run_command_uses_timer_and_bwrap_payload():
    service = Service(
        name="web",
        command="python -m http.server 8000",
        healthcheck=Healthcheck(
            test=["CMD", "curl", "-f", "http://127.0.0.1:8000"],
            interval="10s",
            timeout="2s",
            start_period="5s",
        ),
    )

    command = build_health_systemd_run_command("demo", "web", service)

    assert command is not None
    assert command[:3] == ["systemd-run", "--user", "--unit=demo-web-health"]
    assert "--on-active=5s" in command
    assert "--on-unit-active=10s" in command
    assert ["-p", "TimeoutStartSec=2s"] == command[
        command.index("TimeoutStartSec=2s") - 1 : command.index("TimeoutStartSec=2s") + 1
    ]
    pairs = list(zip(command, command[1:], strict=False))
    assert ("--timer-property", "Requires=demo-web.service") in pairs
    assert ("-p", "Requires=demo-web.service") in pairs
    assert "/usr/bin/bwrap" in command
    assert command[-4:] == ["--", "curl", "-f", "http://127.0.0.1:8000"]


def test_health_service_payload_supports_shell_form():
    service = Service(
        name="web",
        command="python -m http.server 8000",
        healthcheck=Healthcheck(test="curl -f http://127.0.0.1:8000"),
    )

    payload = build_health_service_payload(service)

    assert payload[-3:] == ["/bin/sh", "-c", "curl -f http://127.0.0.1:8000"]


def test_service_definition_hash_changes_when_healthcheck_changes():
    base_service = Service(name="web", command="python -m http.server")
    checked_service = Service(
        name="web",
        command="python -m http.server",
        healthcheck=Healthcheck(test="true"),
    )

    assert service_definition_hash("demo", "web", base_service) != service_definition_hash(
        "demo", "web", checked_service
    )
