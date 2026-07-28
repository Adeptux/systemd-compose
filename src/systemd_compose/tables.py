from __future__ import annotations


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    print(format_table(headers, rows))

def format_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [
        max(len(row[index]) for row in [headers, *rows])
        for index in range(len(headers))
    ]
    lines = [format_table_row(headers, widths)]
    lines.extend(format_table_row(row, widths) for row in rows)
    return "\n".join(lines)

def format_table_row(row: list[str], widths: list[int]) -> str:
    return "   ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()
