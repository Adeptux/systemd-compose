from __future__ import annotations

from pathlib import Path

from systemd_compose.builders import unit_name
from systemd_compose.systemd_units import MISSING_SYSTEMD_VALUES, unit_properties
from systemd_compose.tables import format_table

STATS_PROPERTIES = [
    "Id",
    "ActiveState",
    "ControlGroup",
    "CPUUsageNSec",
    "MemoryCurrent",
    "MemoryMax",
    "IPIngressBytes",
    "IPEgressBytes",
    "IOReadBytes",
    "IOWriteBytes",
    "TasksCurrent",
]
CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_ROOT = Path("/proc")


def collect_stats_snapshot(project_name: str, service_name: str, *, system: bool = False) -> dict[str, str]:
    unit = unit_name(project_name, service_name)
    properties = unit_properties(unit, STATS_PROPERTIES, system=system)
    properties["Name"] = unit
    properties["Service"] = service_name
    apply_cgroup_stats_fallbacks(properties)
    return properties

def apply_cgroup_stats_fallbacks(properties: dict[str, str]) -> None:
    cgroup = properties.get("ControlGroup", "")
    if cgroup in MISSING_SYSTEMD_VALUES:
        return
    cgroup_path = CGROUP_ROOT / cgroup.lstrip("/")
    if not cgroup_path.is_dir():
        return

    cpu_usage = read_cgroup_keyed_value(cgroup_path / "cpu.stat", "usage_usec")
    if is_missing_stats_value(properties.get("CPUUsageNSec", "")) and cpu_usage is not None:
        properties["CPUUsageNSec"] = str(cpu_usage * 1000)

    memory_current = read_cgroup_value(cgroup_path / "memory.current")
    if memory_current is None:
        memory_current = read_process_rss_total(cgroup_path / "cgroup.procs")
    if is_missing_stats_value(properties.get("MemoryCurrent", "")) and memory_current is not None:
        properties["MemoryCurrent"] = memory_current

    memory_max = read_cgroup_value(cgroup_path / "memory.max")
    if is_missing_stats_value(properties.get("MemoryMax", "")) and memory_max is not None:
        properties["MemoryMax"] = memory_max

    pids_current = read_cgroup_value(cgroup_path / "pids.current")
    if pids_current is None:
        pids_current = count_cgroup_lines(cgroup_path / "cgroup.threads") or count_cgroup_lines(cgroup_path / "cgroup.procs")
    if is_missing_stats_value(properties.get("TasksCurrent", "")) and pids_current is not None:
        properties["TasksCurrent"] = pids_current

    io_totals = read_cgroup_io_totals(cgroup_path / "io.stat")
    if io_totals is None:
        io_totals = read_process_io_totals(cgroup_path / "cgroup.procs")
    if io_totals is not None:
        read_bytes, write_bytes = io_totals
        if is_missing_stats_value(properties.get("IOReadBytes", "")):
            properties["IOReadBytes"] = str(read_bytes)
        if is_missing_stats_value(properties.get("IOWriteBytes", "")):
            properties["IOWriteBytes"] = str(write_bytes)

def render_stats_table(
    current: list[dict[str, str]],
    previous: list[dict[str, str]] | None,
    current_time: float,
    previous_time: float | None,
    cpu_count: int,
) -> str:
    previous_by_name = {row["Name"]: row for row in previous or []}
    rows: list[list[str]] = []
    for row in current:
        previous_row = previous_by_name.get(row["Name"])
        rows.append(
            [
                row["Name"],
                format_cpu_percent(row, previous_row, current_time, previous_time, cpu_count),
                format_memory_usage(row.get("MemoryCurrent", ""), row.get("MemoryMax", "")),
                format_memory_percent(row.get("MemoryCurrent", ""), row.get("MemoryMax", "")),
                format_io_pair(row.get("IPIngressBytes", ""), row.get("IPEgressBytes", "")),
                format_io_pair(row.get("IOReadBytes", ""), row.get("IOWriteBytes", "")),
                format_count(row.get("TasksCurrent", "")),
            ]
        )
    return format_table(["NAME", "CPU %", "MEM USAGE / LIMIT", "MEM %", "NET I/O", "BLOCK I/O", "PIDS"], rows)

def format_cpu_percent(
    current: dict[str, str],
    previous: dict[str, str] | None,
    current_time: float,
    previous_time: float | None,
    cpu_count: int,
) -> str:
    if previous is None or previous_time is None:
        return "-"
    current_cpu = parse_systemd_int(current.get("CPUUsageNSec", ""))
    previous_cpu = parse_systemd_int(previous.get("CPUUsageNSec", ""))
    if current_cpu is None or previous_cpu is None:
        return "-"
    elapsed = current_time - previous_time
    if elapsed <= 0:
        return "-"
    percent = ((current_cpu - previous_cpu) / 1_000_000_000) / elapsed / cpu_count * 100
    if percent < 0:
        return "-"
    return f"{percent:.2f}%"

def format_memory_usage(current_value: str, max_value: str) -> str:
    current = parse_systemd_int(current_value)
    maximum = parse_systemd_int(max_value)
    if current is None:
        return "-"
    if maximum is None:
        return f"{format_bytes(current)} / -"
    return f"{format_bytes(current)} / {format_bytes(maximum)}"

def format_memory_percent(current_value: str, max_value: str) -> str:
    current = parse_systemd_int(current_value)
    maximum = parse_systemd_int(max_value)
    if current is None or maximum is None or maximum <= 0:
        return "-"
    return f"{current / maximum * 100:.2f}%"

def format_io_pair(read_value: str, write_value: str) -> str:
    read = parse_systemd_int(read_value)
    write = parse_systemd_int(write_value)
    if read is None and write is None:
        return "-"
    return f"{format_bytes(read or 0)} / {format_bytes(write or 0)}"

def format_count(value: str) -> str:
    parsed = parse_systemd_int(value)
    return str(parsed) if parsed is not None else "-"

def parse_systemd_int(value: str) -> int | None:
    if value in MISSING_SYSTEMD_VALUES:
        return None
    try:
        return int(value)
    except ValueError:
        return None

def format_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)}B"
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{value}B"

def is_missing_stats_value(value: str | None) -> bool:
    return value is None or value in MISSING_SYSTEMD_VALUES

def read_cgroup_value(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None

def read_cgroup_keyed_value(path: Path, key: str) -> int | None:
    content = read_cgroup_value(path)
    if content is None:
        return None
    for line in content.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key:
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None

def read_cgroup_io_totals(path: Path) -> tuple[int, int] | None:
    content = read_cgroup_value(path)
    if content is None:
        return None
    read_total = 0
    write_total = 0
    found = False
    for line in content.splitlines():
        for field in line.split()[1:]:
            key, separator, value = field.partition("=")
            if not separator:
                continue
            try:
                parsed = int(value)
            except ValueError:
                continue
            if key == "rbytes":
                read_total += parsed
                found = True
            elif key == "wbytes":
                write_total += parsed
                found = True
    return (read_total, write_total) if found else None

def count_cgroup_lines(path: Path) -> str | None:
    content = read_cgroup_value(path)
    if content is None:
        return None
    return str(sum(1 for line in content.splitlines() if line.strip()))

def read_cgroup_pids(path: Path) -> list[str]:
    content = read_cgroup_value(path)
    if content is None:
        return []
    return [line.strip() for line in content.splitlines() if line.strip()]

def read_process_rss_total(cgroup_procs_path: Path) -> str | None:
    total_kib = 0
    found = False
    for pid in read_cgroup_pids(cgroup_procs_path):
        status = read_cgroup_value(PROC_ROOT / pid / "status")
        if status is None:
            continue
        for line in status.splitlines():
            key, _separator, value = line.partition(":")
            if key != "VmRSS":
                continue
            parts = value.split()
            if not parts:
                continue
            try:
                total_kib += int(parts[0])
            except ValueError:
                continue
            found = True
            break
    return str(total_kib * 1024) if found else None

def read_process_io_totals(cgroup_procs_path: Path) -> tuple[int, int] | None:
    read_total = 0
    write_total = 0
    found = False
    for pid in read_cgroup_pids(cgroup_procs_path):
        io_content = read_cgroup_value(PROC_ROOT / pid / "io")
        if io_content is None:
            continue
        for line in io_content.splitlines():
            key, _separator, value = line.partition(":")
            try:
                parsed = int(value.strip())
            except ValueError:
                continue
            if key == "read_bytes":
                read_total += parsed
                found = True
            elif key == "write_bytes":
                write_total += parsed
                found = True
    return (read_total, write_total) if found else None
