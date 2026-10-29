"""Plain terminal tables: full names, exact numbers and CJK cell alignment."""

from itertools import zip_longest
import unicodedata


def cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def wrap_cell(text: str, width: int) -> list[str]:
    lines, line, used = [], "", 0
    for character in text:
        size = cell_width(character)
        if line and used + size > width:
            lines.append(line)
            line, used = "", 0
        line += character
        used += size
    return [*lines, line]


def table(headers, rows, *, numeric=(), columns=76) -> list[str]:
    """Wrap only names; use labelled rows when whole numeric columns cannot fit.

    Amounts are already formatted exactly by the caller. Never round, shorten
    or split an amount to make a table fit a narrow terminal.
    """
    rows = [[str(value) for value in row] for row in rows]
    if not rows:
        return []
    widths = [max(cell_width(row[i]) for row in [headers, *rows]) for i in range(len(headers))]
    name_width = columns - 2 - sum(widths[1:]) - 2 * (len(headers) - 1)
    if name_width < max(8, cell_width(headers[0])):
        lines = []
        for row in rows:
            lines.extend("  " + line for line in wrap_cell(row[0], max(2, columns - 2)))
            for header, value in zip(headers[1:], row[1:]):
                label = f"    {header}："
                if cell_width(label + value) > columns:
                    lines.extend([label.rstrip(), "    " + value])
                else:
                    lines.append(label + value)
        return lines
    widths[0] = min(widths[0], name_width)

    def render(row):
        parts = []
        for i, (value, width) in enumerate(zip(row, widths)):
            padding = " " * (width - cell_width(value))
            parts.append(padding + value if i in numeric else value + padding)
        return "  " + "  ".join(parts).rstrip()

    lines = [render(headers), "  " + "  ".join("-" * width for width in widths)]
    for row in rows:
        wrapped = [wrap_cell(row[0], widths[0]), *[[value] for value in row[1:]]]
        lines.extend(render(line) for line in zip_longest(*wrapped, fillvalue=""))
    return lines
