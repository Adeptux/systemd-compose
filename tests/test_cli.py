from pathlib import Path
import subprocess

import systemd_compose.cli
from systemd_compose.builders import service_definition_hash
from systemd_compose.cli import main
from systemd_compose.parser import parse_compose_file


def write_compose_file(tmp_path: Path) -> Path:
    compose_file = tmp_path / "systemd-compose.yaml"
    compose_file.write_text(
        f"""
services:
  web:
    command: "python -m http.server 8000"
    environment:
      PORT: "8000"
    volumes:
      - "{tmp_path / 'site'}:/app:ro"
  db:
    command: "postgres -D /tmp/postgres-data"
""".lstrip(),
        encoding="utf-8",
    )
    return compose_file


def test_up_dry_run_prints_systemd_run_with_bwrap_payload(tmp_path: Path, capsys):
    compose_file = write_compose_file(tmp_path)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "up", "--dry-run"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "systemd-run --user --unit=demo-web" in output
    assert "/usr/bin/bwrap" in output
    assert f"--ro-bind {tmp_path / 'site'} /app" in output
    assert "--setenv PORT 8000" in output


def test_up_starts_units_that_are_not_loaded(tmp_path: Path, monkeypatch):
    compose_file = write_compose_file(tmp_path)
    captured_calls: list[list[str]] = []
    run_calls: list[list[str]] = []

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured_calls.append(command)
        if command[2] == "list-units":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "not-found\n", "")

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        run_calls.append(command)
        return 0

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)
    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "up"])

    assert exit_code == 0
    assert captured_calls == [
        ["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend", "--type=service"],
        ["systemctl", "--user", "show", "--property=LoadState", "--value", "demo-web.service"],
        ["systemctl", "--user", "show", "--property=LoadState", "--value", "demo-db.service"],
    ]
    assert [call[:3] for call in run_calls] == [
        ["systemd-run", "--user", "--unit=demo-web"],
        ["systemd-run", "--user", "--unit=demo-db"],
    ]


def test_up_creates_missing_host_volume_directories(tmp_path: Path, monkeypatch):
    compose_file = write_compose_file(tmp_path)
    site_dir = tmp_path / "site"

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[2] == "list-units":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "not-found\n", "")

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        return 0

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)
    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    assert not site_dir.exists()

    exit_code = main(["-f", str(compose_file), "-p", "demo", "up"])

    assert exit_code == 0
    assert site_dir.is_dir()


def test_up_dry_run_does_not_create_missing_host_volume_directories(tmp_path: Path):
    compose_file = write_compose_file(tmp_path)
    site_dir = tmp_path / "site"

    assert not site_dir.exists()

    exit_code = main(["-f", str(compose_file), "-p", "demo", "up", "--dry-run"])

    assert exit_code == 0
    assert not site_dir.exists()


def test_up_skips_loaded_units(tmp_path: Path, monkeypatch, capsys):
    compose_file = write_compose_file(tmp_path)
    config = parse_compose_file(compose_file)
    captured_calls: list[list[str]] = []
    run_calls: list[list[str]] = []

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured_calls.append(command)
        if command[2] == "list-units":
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[2] in {"stop", "reset-failed"}:
            return subprocess.CompletedProcess(command, 0, "", "")
        property_name = command[3].removeprefix("--property=")
        unit = command[-1]
        if property_name == "LoadState":
            return subprocess.CompletedProcess(command, 0, "loaded\n", "")
        if property_name == "ActiveState":
            return subprocess.CompletedProcess(command, 0, "active\n", "")
        service_name = unit.removeprefix("demo-").removesuffix(".service")
        description_hash = service_definition_hash("demo", service_name, config.services[service_name])
        return subprocess.CompletedProcess(
            command,
            0,
            f"systemd-compose: demo {service_name} systemd-compose-hash={description_hash}\n",
            "",
        )

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        run_calls.append(command)
        return 0

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)
    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "up"])

    assert exit_code == 0
    assert captured_calls == [
        ["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend", "--type=service"],
        ["systemctl", "--user", "show", "--property=LoadState", "--value", "demo-web.service"],
        ["systemctl", "--user", "show", "--property=ActiveState", "--value", "demo-web.service"],
        ["systemctl", "--user", "show", "--property=Description", "--value", "demo-web.service"],
        ["systemctl", "--user", "show", "--property=LoadState", "--value", "demo-db.service"],
        ["systemctl", "--user", "show", "--property=ActiveState", "--value", "demo-db.service"],
        ["systemctl", "--user", "show", "--property=Description", "--value", "demo-db.service"],
    ]
    assert run_calls == []
    assert capsys.readouterr().out == (
        "Skipping unchanged running unit: demo-web.service\n"
        "Skipping unchanged running unit: demo-db.service\n"
    )


def test_up_recreates_changed_running_unit(tmp_path: Path, monkeypatch, capsys):
    compose_file = tmp_path / "systemd-compose.yaml"
    compose_file.write_text(
        """
services:
  web:
    command: "python -m http.server 8000"
""".lstrip(),
        encoding="utf-8",
    )
    captured_calls: list[list[str]] = []
    run_calls: list[list[str]] = []

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured_calls.append(command)
        if command[2] == "list-units":
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[2] in {"stop", "reset-failed"}:
            return subprocess.CompletedProcess(command, 0, "", "")
        property_name = command[3].removeprefix("--property=")
        values = {
            "LoadState": "loaded\n",
            "ActiveState": "active\n",
            "Description": "systemd-compose: demo web systemd-compose-hash=oldhash\n",
        }
        return subprocess.CompletedProcess(command, 0, values[property_name], "")

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        run_calls.append(command)
        return 0

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)
    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "up"])

    assert exit_code == 0
    assert captured_calls == [
        ["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend", "--type=service"],
        ["systemctl", "--user", "show", "--property=LoadState", "--value", "demo-web.service"],
        ["systemctl", "--user", "show", "--property=ActiveState", "--value", "demo-web.service"],
        ["systemctl", "--user", "show", "--property=Description", "--value", "demo-web.service"],
        ["systemctl", "--user", "stop", "demo-web.service"],
        ["systemctl", "--user", "reset-failed", "demo-web.service"],
    ]
    assert [call[:3] for call in run_calls] == [["systemd-run", "--user", "--unit=demo-web"]]
    assert capsys.readouterr().out == "Recreating changed unit: demo-web.service\n"


def test_up_skips_service_when_loaded_dependency_failed(tmp_path: Path, monkeypatch, capsys):
    compose_file = tmp_path / "systemd-compose.yaml"
    compose_file.write_text(
        """
services:
  db:
    command: "postgres -D /tmp/postgres-data"
  web:
    command: "python -m http.server 8000"
    depends_on:
      - db
""".lstrip(),
        encoding="utf-8",
    )
    config = parse_compose_file(compose_file)
    captured_calls: list[list[str]] = []
    run_calls: list[list[str]] = []

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured_calls.append(command)
        if command[2] == "list-units":
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[2] in {"stop", "reset-failed"}:
            return subprocess.CompletedProcess(command, 0, "", "")
        property_name = command[3].removeprefix("--property=")
        unit = command[-1]
        service_name = unit.removeprefix("demo-").removesuffix(".service")
        db_hash = service_definition_hash("demo", "db", config.services["db"])
        values = {
            ("LoadState", "demo-db.service"): "loaded\n",
            ("LoadState", "demo-web.service"): "not-found\n",
            ("ActiveState", "demo-db.service"): "failed\n",
            ("Description", "demo-db.service"): f"systemd-compose: demo db systemd-compose-hash={db_hash}\n",
        }
        if property_name == "Description" and service_name != "db":
            raise AssertionError("web description should not be inspected when it is not loaded")
        return subprocess.CompletedProcess(command, 0, values[(property_name, unit)], "")

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        run_calls.append(command)
        return 0

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)
    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "up"])

    assert exit_code == 1
    assert run_calls == [
        ["systemctl", "--user", "start", "demo-db.service"],
    ]
    assert captured_calls == [
        ["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend", "--type=service"],
        ["systemctl", "--user", "show", "--property=LoadState", "--value", "demo-db.service"],
        ["systemctl", "--user", "show", "--property=ActiveState", "--value", "demo-db.service"],
        ["systemctl", "--user", "show", "--property=Description", "--value", "demo-db.service"],
        ["systemctl", "--user", "show", "--property=LoadState", "--value", "demo-web.service"],
        ["systemctl", "--user", "show", "--property=LoadState", "--value", "demo-db.service"],
        ["systemctl", "--user", "show", "--property=ActiveState", "--value", "demo-db.service"],
    ]
    assert capsys.readouterr().out == (
        "Starting unchanged non-running unit: demo-db.service (failed)\n"
        "Skipping demo-web.service: dependency demo-db.service is failed\n"
    )


def test_status_uses_user_systemctl_for_all_services(tmp_path: Path, monkeypatch):
    compose_file = write_compose_file(tmp_path)
    calls: list[tuple[list[str], bool]] = []

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        calls.append((command, check))
        return 3

    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "status"])

    assert exit_code == 3
    assert calls == [
        (
            ["systemctl", "--user", "--no-pager", "status", "demo-web.service", "demo-db.service"],
            False,
        ),
    ]


def test_status_uses_user_systemctl_for_one_service(monkeypatch):
    calls: list[tuple[list[str], bool]] = []

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        calls.append((command, check))
        return 0

    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    exit_code = main(["-p", "demo", "status", "web"])

    assert exit_code == 0
    assert calls == [
        (
            ["systemctl", "--user", "--no-pager", "status", "demo-web.service"],
            False,
        ),
    ]


def test_ps_prints_service_snapshot(tmp_path: Path, monkeypatch, capsys):
    compose_file = write_compose_file(tmp_path)
    captured_calls: list[list[str]] = []

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured_calls.append(command)
        if command[-1] == "demo-web.service":
            return subprocess.CompletedProcess(
                command,
                0,
                "\n".join(
                    [
                        "Id=demo-web.service",
                        "LoadState=loaded",
                        "ActiveState=active",
                        "SubState=running",
                        "MainPID=123",
                        "ExecMainStartTimestamp=Mon 2026-07-13 21:18:43 EDT",
                    ]
                )
                + "\n",
                "",
            )
        return subprocess.CompletedProcess(command, 5, "", "Unit demo-db.service not loaded.\n")

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "ps"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "NAME" in output
    assert "SERVICE" in output
    assert "demo-web" in output
    assert "Up (running)" in output
    assert "123" in output
    assert "demo-db" in output
    assert "not created" in output
    assert captured_calls == [
        [
            "systemctl",
            "--user",
            "show",
            "--property=Id",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--property=ExecMainStartTimestamp",
            "demo-web.service",
        ],
        [
            "systemctl",
            "--user",
            "show",
            "--property=Id",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--property=ExecMainStartTimestamp",
            "demo-db.service",
        ],
    ]


def test_stats_no_stream_prints_service_snapshot(tmp_path: Path, monkeypatch, capsys):
    compose_file = write_compose_file(tmp_path)

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[-1] == "demo-web.service":
            return subprocess.CompletedProcess(
                command,
                0,
                "\n".join(
                    [
                        "Id=demo-web.service",
                        "ActiveState=active",
                        "CPUUsageNSec=5000000000",
                        "MemoryCurrent=1048576",
                        "MemoryMax=2147483648",
                        "IPIngressBytes=1024",
                        "IPEgressBytes=2048",
                        "IOReadBytes=4096",
                        "IOWriteBytes=8192",
                        "TasksCurrent=4",
                    ]
                )
                + "\n",
                "",
            )
        return subprocess.CompletedProcess(command, 5, "", "Unit demo-db.service not loaded.\n")

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)
    monkeypatch.setattr(systemd_compose.cli.time, "sleep", lambda _interval: None)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "stats", "--no-stream"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "CPU %" in output
    assert "demo-web" in output
    assert "1.0MiB / 2.0GiB" in output
    assert "0.05%" in output
    assert "1.0KiB / 2.0KiB" in output
    assert "4.0KiB / 8.0KiB" in output
    assert "demo-db" in output
    assert " - " in output


def test_stats_falls_back_to_cgroup_files(tmp_path: Path, monkeypatch):
    cgroup_dir = tmp_path / "demo-web.service"
    cgroup_dir.mkdir()
    (cgroup_dir / "cpu.stat").write_text("usage_usec 3000000\n", encoding="utf-8")
    (cgroup_dir / "memory.current").write_text("1048576\n", encoding="utf-8")
    (cgroup_dir / "memory.max").write_text("2097152\n", encoding="utf-8")
    (cgroup_dir / "cgroup.threads").write_text("123\n124\n", encoding="utf-8")
    (cgroup_dir / "io.stat").write_text(
        "8:0 rbytes=1024 wbytes=2048 rios=1 wios=2\n"
        "8:16 rbytes=4096 wbytes=8192 rios=3 wios=4\n",
        encoding="utf-8",
    )
    properties = {
        "ControlGroup": "/demo-web.service",
        "CPUUsageNSec": "[not set]",
        "MemoryCurrent": "[not set]",
        "MemoryMax": "infinity",
        "TasksCurrent": "[not set]",
        "IOReadBytes": "[not set]",
        "IOWriteBytes": "[not set]",
    }

    monkeypatch.setattr(systemd_compose.cli, "CGROUP_ROOT", tmp_path)

    systemd_compose.cli.apply_cgroup_stats_fallbacks(properties)

    assert properties["CPUUsageNSec"] == "3000000000"
    assert properties["MemoryCurrent"] == "1048576"
    assert properties["MemoryMax"] == "2097152"
    assert properties["TasksCurrent"] == "2"
    assert properties["IOReadBytes"] == "5120"
    assert properties["IOWriteBytes"] == "10240"


def test_stats_falls_back_to_proc_files(tmp_path: Path, monkeypatch):
    cgroup_root = tmp_path / "cgroup"
    proc_root = tmp_path / "proc"
    cgroup_dir = cgroup_root / "demo-web.service"
    cgroup_dir.mkdir(parents=True)
    (cgroup_dir / "cgroup.procs").write_text("123\n124\n", encoding="utf-8")
    for pid, rss_kib, read_bytes, write_bytes in [
        ("123", 100, 1024, 2048),
        ("124", 200, 4096, 8192),
    ]:
        proc_dir = proc_root / pid
        proc_dir.mkdir(parents=True)
        (proc_dir / "status").write_text(f"Name:\ttest\nVmRSS:\t{rss_kib} kB\n", encoding="utf-8")
        (proc_dir / "io").write_text(
            f"read_bytes: {read_bytes}\nwrite_bytes: {write_bytes}\n",
            encoding="utf-8",
        )
    properties = {
        "ControlGroup": "/demo-web.service",
        "MemoryCurrent": "[not set]",
        "MemoryMax": "infinity",
        "TasksCurrent": "[not set]",
        "IOReadBytes": "[not set]",
        "IOWriteBytes": "[not set]",
    }

    monkeypatch.setattr(systemd_compose.cli, "CGROUP_ROOT", cgroup_root)
    monkeypatch.setattr(systemd_compose.cli, "PROC_ROOT", proc_root)

    systemd_compose.cli.apply_cgroup_stats_fallbacks(properties)

    assert properties["MemoryCurrent"] == str(300 * 1024)
    assert properties["TasksCurrent"] == "2"
    assert properties["IOReadBytes"] == "5120"
    assert properties["IOWriteBytes"] == "10240"


def test_render_stats_table_calculates_cpu_percent():
    previous = [{"Name": "demo-web", "CPUUsageNSec": "1000000000"}]
    current = [
        {
            "Name": "demo-web",
            "CPUUsageNSec": "3000000000",
            "MemoryCurrent": "0",
            "MemoryMax": "100",
            "IPIngressBytes": "0",
            "IPEgressBytes": "0",
            "IOReadBytes": "0",
            "IOWriteBytes": "0",
            "TasksCurrent": "1",
        }
    ]

    output = systemd_compose.cli.render_stats_table(current, previous, 11.0, 10.0, 2)

    assert "100.00%" in output


def test_start_starts_existing_non_running_unit(tmp_path: Path, monkeypatch):
    compose_file = write_compose_file(tmp_path)
    captured_calls: list[list[str]] = []
    run_calls: list[tuple[list[str], bool]] = []

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured_calls.append(command)
        if command[2] == "reset-failed":
            return subprocess.CompletedProcess(command, 0, "", "")
        property_name = command[3].removeprefix("--property=")
        values = {
            "LoadState": "loaded\n",
            "ActiveState": "inactive\n",
            "Description": "systemd-compose: demo web systemd-compose-hash=abc\n",
        }
        return subprocess.CompletedProcess(command, 0, values[property_name], "")

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        run_calls.append((command, check))
        return 0

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)
    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "start", "web"])

    assert exit_code == 0
    assert captured_calls == [
        ["systemctl", "--user", "show", "--property=LoadState", "--value", "demo-web.service"],
        ["systemctl", "--user", "show", "--property=ActiveState", "--value", "demo-web.service"],
        ["systemctl", "--user", "show", "--property=Description", "--value", "demo-web.service"],
    ]
    assert run_calls == [
        (["systemctl", "--user", "start", "demo-web.service"], False),
    ]


def test_start_failed_unit_does_not_reset_failed_before_starting(tmp_path: Path, monkeypatch):
    compose_file = write_compose_file(tmp_path)
    captured_calls: list[list[str]] = []
    run_calls: list[tuple[list[str], bool]] = []

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured_calls.append(command)
        property_name = command[3].removeprefix("--property=")
        values = {
            "LoadState": "loaded\n",
            "ActiveState": "failed\n",
            "Description": "systemd-compose: demo web systemd-compose-hash=abc\n",
        }
        return subprocess.CompletedProcess(command, 0, values[property_name], "")

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        run_calls.append((command, check))
        return 1

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)
    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "start", "web"])

    assert exit_code == 1
    assert captured_calls == [
        ["systemctl", "--user", "show", "--property=LoadState", "--value", "demo-web.service"],
        ["systemctl", "--user", "show", "--property=ActiveState", "--value", "demo-web.service"],
        ["systemctl", "--user", "show", "--property=Description", "--value", "demo-web.service"],
    ]
    assert run_calls == [
        (["systemctl", "--user", "start", "demo-web.service"], False),
    ]


def test_start_refuses_missing_unit(tmp_path: Path, monkeypatch, capsys):
    compose_file = write_compose_file(tmp_path)
    captured_calls: list[list[str]] = []
    run_calls: list[list[str]] = []

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured_calls.append(command)
        return subprocess.CompletedProcess(command, 0, "not-found\n", "")

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        run_calls.append(command)
        return 0

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)
    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "start", "web"])

    assert exit_code == 1
    assert captured_calls == [
        ["systemctl", "--user", "show", "--property=LoadState", "--value", "demo-web.service"],
    ]
    assert run_calls == []
    assert capsys.readouterr().err == (
        "Cannot start demo-web.service: service has not been created by up\n"
    )


def test_stop_only_stops_units_without_resetting(tmp_path: Path, monkeypatch):
    compose_file = write_compose_file(tmp_path)
    captured_calls: list[list[str]] = []

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured_calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "stop"])

    assert exit_code == 0
    assert captured_calls == [
        ["systemctl", "--user", "stop", "demo-db.service"],
        ["systemctl", "--user", "stop", "demo-web.service"],
    ]


def test_restart_restarts_existing_unit(tmp_path: Path, monkeypatch):
    compose_file = write_compose_file(tmp_path)
    captured_calls: list[list[str]] = []
    run_calls: list[tuple[list[str], bool]] = []

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured_calls.append(command)
        if command[2] == "reset-failed":
            return subprocess.CompletedProcess(command, 0, "", "")
        property_name = command[3].removeprefix("--property=")
        values = {
            "LoadState": "loaded\n",
            "ActiveState": "active\n",
            "Description": "systemd-compose: demo web systemd-compose-hash=abc\n",
        }
        return subprocess.CompletedProcess(command, 0, values[property_name], "")

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        run_calls.append((command, check))
        return 0

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)
    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "restart", "web"])

    assert exit_code == 0
    assert captured_calls == [
        ["systemctl", "--user", "show", "--property=LoadState", "--value", "demo-web.service"],
        ["systemctl", "--user", "show", "--property=ActiveState", "--value", "demo-web.service"],
        ["systemctl", "--user", "show", "--property=Description", "--value", "demo-web.service"],
    ]
    assert run_calls == [
        (["systemctl", "--user", "restart", "demo-web.service"], False),
    ]


def test_down_ignores_not_loaded_units_and_continues(tmp_path: Path, monkeypatch, capsys):
    compose_file = write_compose_file(tmp_path)
    calls: list[list[str]] = []

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 5, "", "not loaded")

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "down"])

    assert exit_code == 0
    assert calls == [
        ["systemctl", "--user", "stop", "demo-db.service"],
        ["systemctl", "--user", "reset-failed", "demo-db.service"],
        ["systemctl", "--user", "stop", "demo-web.service"],
        ["systemctl", "--user", "reset-failed", "demo-web.service"],
    ]
    assert capsys.readouterr().err == ""


def test_down_returns_first_real_stop_failure(tmp_path: Path, monkeypatch, capsys):
    compose_file = write_compose_file(tmp_path)
    calls: list[list[str]] = []

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[2] == "stop" and command[-1] == "demo-db.service":
            return subprocess.CompletedProcess(command, 1, "", "boom\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "down"])

    assert exit_code == 1
    assert calls == [
        ["systemctl", "--user", "stop", "demo-db.service"],
        ["systemctl", "--user", "reset-failed", "demo-db.service"],
        ["systemctl", "--user", "stop", "demo-web.service"],
        ["systemctl", "--user", "reset-failed", "demo-web.service"],
    ]
    assert capsys.readouterr().err == "boom\n"


def test_up_warns_about_orphan_units(tmp_path: Path, monkeypatch, capsys):
    compose_file = write_compose_file(tmp_path)
    captured_calls: list[list[str]] = []
    run_calls: list[list[str]] = []

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured_calls.append(command)
        if command[2] == "list-units":
            return subprocess.CompletedProcess(
                command,
                0,
                "demo-old.service loaded active running old\n"
                "demo-web.service loaded active running web\n"
                "unrelated.service loaded active running nope\n",
                "",
            )
        if command[2] == "show" and command[-1] == "demo-old.service":
            return subprocess.CompletedProcess(
                command,
                0,
                "systemd-compose: demo old systemd-compose-hash=oldhash\n",
                "",
            )
        return subprocess.CompletedProcess(command, 0, "not-found\n", "")

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        run_calls.append(command)
        return 0

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)
    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "up"])

    assert exit_code == 0
    assert run_calls
    assert "Found orphan unit(s): demo-old.service. Use --remove-orphans to stop them.\n" in capsys.readouterr().out


def test_down_remove_orphans_cleans_project_orphan_units(tmp_path: Path, monkeypatch):
    compose_file = write_compose_file(tmp_path)
    captured_calls: list[list[str]] = []

    def fake_run_command_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured_calls.append(command)
        if command[2] == "list-units":
            return subprocess.CompletedProcess(
                command,
                0,
                "demo-old.service loaded inactive dead old\n"
                "demo-web.service loaded inactive dead web\n"
                "other-old.service loaded inactive dead other\n",
                "",
            )
        if command[2] == "show":
            return subprocess.CompletedProcess(
                command,
                0,
                "systemd-compose: demo old systemd-compose-hash=oldhash\n",
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(systemd_compose.cli, "run_command_capture", fake_run_command_capture)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "down", "--remove-orphans"])

    assert exit_code == 0
    assert captured_calls == [
        ["systemctl", "--user", "stop", "demo-db.service"],
        ["systemctl", "--user", "reset-failed", "demo-db.service"],
        ["systemctl", "--user", "stop", "demo-web.service"],
        ["systemctl", "--user", "reset-failed", "demo-web.service"],
        ["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend", "--type=service"],
        ["systemctl", "--user", "show", "--property=Description", "--value", "demo-old.service"],
        ["systemctl", "--user", "stop", "demo-old.service"],
        ["systemctl", "--user", "reset-failed", "demo-old.service"],
    ]


def test_default_file_resolution_falls_back_to_yml(tmp_path: Path, monkeypatch, capsys):
    compose_file = write_compose_file(tmp_path)
    compose_file.rename(tmp_path / "systemd-compose.yml")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["-p", "demo", "up", "--dry-run"])

    assert exit_code == 0
    assert "systemd-run --user --unit=demo-web" in capsys.readouterr().out


def test_explicit_file_does_not_fall_back(tmp_path: Path, monkeypatch, capsys):
    compose_file = write_compose_file(tmp_path)
    compose_file.rename(tmp_path / "systemd-compose.yml")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["-f", "missing.yml", "up", "--dry-run"])

    assert exit_code == 2
    assert "compose file not found: missing.yml" in capsys.readouterr().err


def test_logs_defaults_to_all_services(tmp_path: Path, monkeypatch):
    compose_file = write_compose_file(tmp_path)
    calls: list[tuple[list[str], bool]] = []

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        calls.append((command, check))
        return 0

    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    exit_code = main(["-f", str(compose_file), "-p", "demo", "logs"])

    assert exit_code == 0
    assert calls == [
        (
            [
                "journalctl",
                "--user",
                "-f",
                "-u",
                "demo-web.service",
                "-u",
                "demo-db.service",
            ],
            True,
        ),
    ]


def test_logs_accepts_multiple_services(monkeypatch):
    calls: list[tuple[list[str], bool]] = []

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        calls.append((command, check))
        return 0

    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    exit_code = main(["-p", "demo", "logs", "web", "db"])

    assert exit_code == 0
    assert calls == [
        (
            [
                "journalctl",
                "--user",
                "-f",
                "-u",
                "demo-web.service",
                "-u",
                "demo-db.service",
            ],
            True,
        ),
    ]


def test_logs_passes_journalctl_args_after_separator(monkeypatch):
    calls: list[tuple[list[str], bool]] = []

    def fake_run_command(command: list[str], *, check: bool = True) -> int:
        calls.append((command, check))
        return 0

    monkeypatch.setattr(systemd_compose.cli, "run_command", fake_run_command)

    exit_code = main(["-p", "demo", "logs", "web", "--", "--since", "today", "--no-pager"])

    assert exit_code == 0
    assert calls == [
        (
            [
                "journalctl",
                "--user",
                "-f",
                "-u",
                "demo-web.service",
                "--since",
                "today",
                "--no-pager",
            ],
            True,
        ),
    ]
