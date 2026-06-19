from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re

from app.worldpanel.parser import KeyMeasuresTable


DATE_RE = re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{2}\b")
_TITLE = "Key Measures Data Table"
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


class PivotResultError(ValueError):
    """Raised when a rendered pivot table cannot be parsed or resolved."""


@dataclass(frozen=True)
class PivotResultTable:
    """Orientation-agnostic parse of one rendered Data Explorer table.

    Unlike `KeyMeasuresTable`, no axis is assumed: rows may be dates, products,
    channels, or anything else the pivot produced.
    """

    title: str
    metric: str
    column_labels: tuple[str, ...]
    row_labels: tuple[str, ...]
    cells: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def date_axis(self) -> str | None:
        if self.row_labels and all(DATE_RE.fullmatch(label) for label in self.row_labels):
            return "row"
        if self.column_labels and all(DATE_RE.fullmatch(label) for label in self.column_labels):
            return "column"
        return None

    @property
    def dates(self) -> tuple[str, ...]:
        if self.date_axis == "row":
            return self.row_labels
        if self.date_axis == "column":
            return self.column_labels
        return ()

    def value(self, first_term: str, second_term: str) -> tuple[float, str, str]:
        """Look up one cell by two labels, in either orientation.

        Returns (value, matched_row_label, matched_column_label).
        """
        for row_term, column_term in ((first_term, second_term), (second_term, first_term)):
            row = _match_label(row_term, self.row_labels)
            column = _match_label(column_term, self.column_labels)
            if row is not None and column is not None:
                if column not in self.cells.get(row, {}):
                    raise PivotResultError(f"Cell '{row}' x '{column}' is empty (no data)")
                return self.cells[row][column], row, column
        raise PivotResultError(
            f"No cell found for '{first_term}' x '{second_term}'. "
            f"Rows: {', '.join(self.row_labels[:12])}. Columns: {', '.join(self.column_labels[:12])}."
        )

    def to_key_measures(self) -> KeyMeasuresTable:
        """Convert to the legacy date-rows/product-columns shape when possible."""
        axis = self.date_axis
        if axis is None:
            raise PivotResultError("Table has no date axis; cannot convert to KeyMeasuresTable")
        # Preserve the raw values (do NOT round): decimal KPIs like Penetration
        # %, Average Price, and Frequency must keep their decimals downstream
        # (chat answer, CSV export, data checker).
        if axis == "row":
            dates, products = self.row_labels, self.column_labels
            rows = {
                date: {
                    product: self.cells[date][product]
                    for product in products
                    if product in self.cells.get(date, {})
                }
                for date in dates
            }
        else:
            dates, products = self.column_labels, self.row_labels
            rows = {
                date: {
                    product: self.cells[product][date]
                    for product in products
                    if date in self.cells.get(product, {})
                }
                for date in dates
            }
        return KeyMeasuresTable(
            title=self.title,
            metric=self.metric,
            products=list(products),
            dates=list(dates),
            rows=rows,
        )


def table_from_grid(
    columns: list[str],
    rows: list[tuple[str, list[str | None]]],
    *,
    metric: str,
    title: str = _TITLE,
) -> PivotResultTable:
    """Build a result table from a structured DOM grid extraction.

    `rows` is a list of (row_label, raw_cell_values) where each raw value is the
    cell text or None. Empty/"." cells are treated as missing (not zero), so a
    null Year-on-Yr cell is never confused with a real 0.
    """
    column_labels = tuple(_clean(column) for column in columns)
    row_labels: list[str] = []
    cells: dict[str, dict[str, float]] = {}
    for raw_label, raw_values in rows:
        label = _clean(raw_label)
        if not label:
            continue
        row_cells: dict[str, float] = {}
        for column, raw_value in zip(column_labels, raw_values, strict=False):
            if raw_value is None:
                continue
            cleaned = _clean(raw_value)
            if not _looks_numeric(cleaned):
                continue
            row_cells[column] = _to_float(cleaned)
        if label not in cells:
            row_labels.append(label)
        cells[label] = row_cells
    if not cells:
        raise PivotResultError("Report grid contained no data rows")
    return PivotResultTable(
        title=title,
        metric=metric,
        column_labels=column_labels,
        row_labels=tuple(row_labels),
        cells=cells,
    )


def parse_pivot_text(text: str, metric_override: str | None = None) -> PivotResultTable:
    """Parse rendered report text without assuming which dimension is on rows.

    The rendered text is a flat sequence: title and control labels, then the
    column headers, then repeating groups of one row label followed by exactly
    one numeric value per column.
    """
    lines = [_clean(line) for line in text.splitlines()]
    lines = [line for line in lines if line]

    start = _first_data_row_index(lines)
    width = _row_width(lines, start)
    if width == 0:
        raise PivotResultError("Could not find any numeric data rows in report text")

    headers = _column_headers(lines, start, width)
    metric = metric_override or _detect_metric(lines[:start], headers)

    row_labels: list[str] = []
    cells: dict[str, dict[str, float]] = {}
    index = start
    while index < len(lines):
        label = lines[index]
        if _looks_numeric(label):
            index += 1
            continue
        values: list[float] = []
        cursor = index + 1
        while cursor < len(lines) and len(values) < width and _looks_numeric(lines[cursor]):
            values.append(_to_float(lines[cursor]))
            cursor += 1
        if len(values) == width:
            if label not in cells:
                row_labels.append(label)
            cells[label] = dict(zip(headers, values, strict=True))
            index = cursor
        else:
            index += 1

    if not cells:
        raise PivotResultError("Could not find any complete data rows in report text")
    return PivotResultTable(
        title=_TITLE if any(_TITLE.casefold() in line.casefold() for line in lines) else lines[0],
        metric=metric,
        column_labels=tuple(headers),
        row_labels=tuple(row_labels),
        cells=cells,
    )


def resolve_period(expected: str, table: PivotResultTable) -> str:
    """Resolve a requested period like '2025 P6' or '2025 全年' to an actual
    date label present in the table. Raises if it cannot be verified."""
    labels = table.dates
    if not labels:
        raise PivotResultError(
            f"Requested period '{expected}' cannot be verified: the table has no date axis"
        )
    parsed = [(datetime.strptime(label, "%d-%b-%y"), label) for label in labels]

    exact = next((label for label in labels if label.casefold() == expected.strip().casefold()), None)
    if exact:
        return exact

    match = re.search(r"(20\d{2})\s*[Pp]\s*(\d{1,2})", expected)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        candidates = [label for stamp, label in parsed if stamp.year == year and stamp.month == month]
        if not candidates:
            raise PivotResultError(f"No date in the table matches period '{expected}'")
        return candidates[-1]

    match = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月", expected)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        candidates = [label for stamp, label in parsed if stamp.year == year and stamp.month == month]
        if not candidates:
            raise PivotResultError(f"No date in the table matches period '{expected}'")
        return candidates[-1]

    english = re.search(
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(20\d{2})"
        r"|(20\d{2})\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*",
        expected,
        flags=re.IGNORECASE,
    )
    if english:
        month_name = (english.group(1) or english.group(4)).lower()
        year = int(english.group(2) or english.group(3))
        month = _MONTHS.index(month_name) + 1
        candidates = [label for stamp, label in parsed if stamp.year == year and stamp.month == month]
        if not candidates:
            raise PivotResultError(f"No date in the table matches period '{expected}'")
        return candidates[-1]

    match = re.search(r"20\d{2}", expected)
    if match:
        year = int(match.group(0))
        candidates = sorted((stamp, label) for stamp, label in parsed if stamp.year == year)
        if not candidates:
            raise PivotResultError(f"No date in the table matches year '{expected}'")
        return candidates[-1][1]

    raise PivotResultError(f"Could not interpret requested period '{expected}'")


def answer_from_pivot_tables(
    tables: dict[str, PivotResultTable],
    member_leaves: list[str],
    period_label: str | None,
) -> str:
    """Build an answer for a pivot result whose shape may be any cross-tab.

    `member_leaves` are leaf labels of the requested member selections; the
    period label is the resolved actual date label, when one was requested.
    """
    if not tables:
        raise PivotResultError("No pivot tables were parsed")
    lines: list[str] = []
    for metric, table in tables.items():
        terms = [leaf for leaf in member_leaves if _label_in_table(leaf, table)]
        period = period_label or (table.dates[-1] if table.dates else None)
        fmt = _formatter_for(metric)
        if terms and period:
            # Report every requested member (e.g. "Fruit and 4 Premium Fruits").
            parts = []
            for term in terms:
                value, row, column = table.value(term, period)
                member = column if _match_label(period, (row,)) else row
                parts.append(f"{member} {fmt(value)}")
            lines.append(f"{metric}：" + "；".join(parts) + f"（{period}）")
        elif terms and len(terms) >= 2:
            value, row, column = table.value(terms[0], terms[1])
            lines.append(f"{metric}：{fmt(value)}（{row} × {column}）")
        elif period and table.date_axis in ("row", "column"):
            # No specific member requested: report every column at this period.
            row = _match_label(period, table.row_labels)
            if row is not None:
                cells = table.cells.get(row, {})
                parts = [f"{column} {fmt(cells[column])}" for column in table.column_labels if column in cells]
                lines.append(f"{metric}（{row}）：" + "；".join(parts) if parts else f"{metric}（{row}）：无数据")
            else:
                column = _match_label(period, table.column_labels)
                parts = [
                    f"{label} {fmt(table.cells[label][column])}"
                    for label in table.row_labels
                    if column and column in table.cells.get(label, {})
                ]
                lines.append(f"{metric}（{column}）：" + "；".join(parts) if parts else f"{metric}：无数据")
        else:
            preview = "、".join(table.row_labels[:8])
            lines.append(f"{metric}：表格已刷新（{len(table.row_labels)} 行：{preview}…）")
    return "\n".join(lines)


def format_number(value: float) -> str:
    """Format a value with a thousands separator, preserving up to 2 decimals
    and trimming trailing zeros, so 7.3 -> '7.3', 46.27 -> '46.27', and whole
    numbers like 2931643.0 -> '2,931,643' (no spurious '.0')."""
    if value is None:
        return ""
    if abs(value - round(value)) < 1e-9:
        return f"{round(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def format_plain(value: float) -> str:
    """Plain numeric string (no thousands separators) for CSV/data, decimals
    preserved and trailing zeros trimmed: 4000 -> '4000', 7.3 -> '7.3'."""
    if value is None:
        return ""
    if abs(value - round(value)) < 1e-9:
        return str(round(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _formatter_for(metric: str):
    lowered = metric.casefold()
    is_change = any(token in lowered for token in ("change", "yr on yr", "同比", "环比", "difference"))
    is_level_percent = ("%" in metric) and not is_change

    def fmt(value: float) -> str:
        if is_change:
            sign = "+" if value >= 0 else ""
            return f"{sign}{format_number(value)}%"
        if is_level_percent:
            return f"{format_number(value)}%"
        return format_number(value)

    return fmt


def _label_in_table(term: str, table: PivotResultTable) -> bool:
    return (
        _match_label(term, table.row_labels) is not None
        or _match_label(term, table.column_labels) is not None
    )


def _match_label(term: str, labels: tuple[str, ...]) -> str | None:
    normalized = _normalize(term)
    if not normalized:
        return None
    for label in labels:
        if _normalize(label) == normalized:
            return label
    for label in labels:
        candidate = _normalize(label)
        if normalized in candidate or candidate in normalized:
            return label
    return None


def _first_data_row_index(lines: list[str]) -> int:
    for index in range(len(lines) - 1):
        if not _looks_numeric(lines[index]) and _looks_numeric(lines[index + 1]):
            return index
    raise PivotResultError("Could not find a data row in report text")


def _row_width(lines: list[str], start: int) -> int:
    width = 0
    for line in lines[start + 1 :]:
        if _looks_numeric(line):
            width += 1
        else:
            break
    return width


def _column_headers(lines: list[str], start: int, width: int) -> list[str]:
    candidates = [
        line
        for line in lines[:start]
        if not _looks_numeric(line) and _TITLE.casefold() not in line.casefold()
    ]
    if len(candidates) < width:
        raise PivotResultError(
            f"Found {len(candidates)} header lines but the first data row has {width} values"
        )
    return candidates[-width:]


def _detect_metric(header_lines: list[str], column_headers: list[str]) -> str:
    consumed = set(column_headers)
    for line in reversed(header_lines):
        if line in consumed or _TITLE.casefold() in line.casefold():
            continue
        if ("(" in line and ")" in line) or "%" in line:
            return line
    return "Unknown KPI"


def _clean(line: str) -> str:
    return re.sub(r"\s+", " ", line.replace("\xa0", " ")).strip()


def _looks_numeric(value: str) -> bool:
    return bool(re.fullmatch(r"-?[\d,]+(?:\.\d+)?%?", value))


def _to_float(value: str) -> float:
    return float(value.replace(",", "").rstrip("%"))


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", value.casefold())
