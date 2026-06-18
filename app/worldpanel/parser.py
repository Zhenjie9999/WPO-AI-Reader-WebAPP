from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re


DATE_RE = re.compile(r"\b\d{2}-[A-Za-z]{3}-\d{2}\b")


@dataclass(frozen=True)
class KeyMeasuresTable:
    title: str
    metric: str
    products: list[str]
    dates: list[str]
    # Values may be int (Spend) or float (Penetration %, Average Price, ...).
    rows: dict[str, dict[str, float]]

    def value_for(self, product: str, date_label: str, metric: str | None = None) -> float:
        if metric and metric.lower() != self.metric.lower():
            raise KeyError(f"Metric not available: {metric}")
        canonical_product = _match_product(product, self.products)
        canonical_date = _match_date(date_label, self.dates)
        return self.rows[canonical_date][canonical_product]

    def date_for_year_month(self, year: int, month: int) -> str:
        matches = []
        for label in self.dates:
            parsed = datetime.strptime(label, "%d-%b-%y")
            if parsed.year == year and parsed.month == month:
                matches.append(label)
        if not matches:
            raise KeyError(f"No date found for {year}-{month:02d}")
        return matches[-1]


def parse_key_measures_text(text: str, metric_override: str | None = None) -> KeyMeasuresTable:
    lines = [_clean_line(line) for line in text.splitlines()]
    lines = [line for line in lines if line]

    metric = metric_override or _first_metric(lines)
    products = _extract_products(lines)
    rows = _extract_rows(lines, products)

    return KeyMeasuresTable(
        title=_first_matching(lines, "Key Measures Data Table"),
        metric=metric,
        products=products,
        dates=list(rows.keys()),
        rows=rows,
    )


def _clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.replace("\xa0", " ")).strip()


def _first_matching(lines: list[str], default: str) -> str:
    for line in lines:
        if default.lower() in line.lower():
            return default
    return default


def _first_metric(lines: list[str]) -> str:
    for line in lines:
        if "Spend" in line and "RMB" in line:
            return "Spend (RMB 000)"
    raise ValueError("Could not find supported metric 'Spend (RMB 000)' in report text")


def _extract_products(lines: list[str]) -> list[str]:
    first_date_index = _first_date_index(lines)
    product_count = _first_date_value_count(lines, first_date_index)
    headers = [
        line
        for line in lines[:first_date_index]
        if _looks_like_product_header(line)
    ]
    products = headers[-product_count:] if product_count else []
    if not products:
        raise ValueError("Could not find product headers in report text")
    return products


def _first_date_index(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if DATE_RE.fullmatch(line):
            return index
    raise ValueError("Could not find date rows in report text")


def _first_date_value_count(lines: list[str], first_date_index: int) -> int:
    count = 0
    for line in lines[first_date_index + 1 :]:
        if DATE_RE.fullmatch(line):
            break
        if _looks_numeric(line):
            count += 1
    return count


def _looks_like_product_header(line: str) -> bool:
    if _looks_numeric(line) or DATE_RE.fullmatch(line):
        return False
    excluded = {
        "STD",
        "52 w/e",
        "12 w/e",
        "4 w/e",
        "YTD",
        "Actual Yr on Yr",
        "Yr on Yr % Change",
        "Yr on Yr Difference",
        "Period on Period % Change~",
        "Period on Period Difference~",
        "Key Measures Data Table",
    }
    if line in excluded:
        return False
    if "Spend" in line and "RMB" in line:
        return False
    if "%" in line:
        return False
    return True


def _extract_rows(lines: list[str], products: list[str]) -> dict[str, dict[str, int]]:
    rows: dict[str, dict[str, int]] = {}
    i = 0
    while i < len(lines):
        if not DATE_RE.fullmatch(lines[i]):
            i += 1
            continue

        date_label = lines[i]
        values: list[int] = []
        j = i + 1
        while j < len(lines) and len(values) < len(products):
            if _looks_numeric(lines[j]):
                values.append(_to_int(lines[j]))
            j += 1

        if len(values) == len(products):
            rows[date_label] = dict(zip(products, values, strict=True))
            i = j
        else:
            i += 1

    if not rows:
        raise ValueError("Could not find date rows in report text")
    return rows


def _looks_numeric(value: str) -> bool:
    return bool(re.fullmatch(r"-?[\d,]+(?:\.\d+)?", value))


def _to_int(value: str) -> int:
    return round(float(value.replace(",", "")))


def _match_product(product: str, products: list[str]) -> str:
    normalized = _normalize_product(product)
    for candidate in products:
        if _normalize_product(candidate) == normalized:
            return candidate
    for candidate in products:
        candidate_normalized = _normalize_product(candidate)
        if normalized in candidate_normalized or candidate_normalized in normalized:
            return candidate
    raise KeyError(f"Product not available: {product}")


def _normalize_product(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.lower())


def _match_date(date_label: str, dates: list[str]) -> str:
    normalized = date_label.lower().strip()
    for candidate in dates:
        if candidate.lower() == normalized:
            return candidate
    raise KeyError(f"Date not available: {date_label}")
