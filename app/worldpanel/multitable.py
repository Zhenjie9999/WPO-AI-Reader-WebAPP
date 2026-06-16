from __future__ import annotations

from dataclasses import dataclass

from app.worldpanel.parser import KeyMeasuresTable


@dataclass(frozen=True)
class MultiKpiTable:
    tables: dict[str, KeyMeasuresTable]

    @property
    def products(self) -> list[str]:
        for table in self.tables.values():
            return table.products
        return []

    @property
    def dates(self) -> list[str]:
        for table in self.tables.values():
            return table.dates
        return []

    @property
    def metrics(self) -> list[str]:
        return list(self.tables.keys())

    def table_for_metric(self, metric: str) -> KeyMeasuresTable | None:
        normalized = _normalize(metric)
        keywords = _metric_keywords(metric)
        for label, table in self.tables.items():
            label_normalized = _normalize(label)
            if normalized == label_normalized or normalized in label_normalized or label_normalized in normalized:
                return table
            if any(keyword in label_normalized for keyword in keywords):
                return table
        return None


def _normalize(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _metric_keywords(metric: str) -> list[str]:
    normalized = _normalize(metric)
    if "spend" in normalized or "value" in normalized or "销额" in normalized or "销售额" in normalized:
        return ["spend", "value"]
    if "volume" in normalized or "销量" in normalized or "销售量" in normalized:
        return ["volume"]
    if "penetration" in normalized or "渗透" in normalized:
        return ["penetration"]
    return [normalized]
