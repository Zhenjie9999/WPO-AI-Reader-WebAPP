from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re

from app.worldpanel.multitable import MultiKpiTable
from app.worldpanel.parser import KeyMeasuresTable


@dataclass(frozen=True)
class InterpretedQuestion:
    product: str | None
    metrics: list[str]
    date_label: str | None = None
    year: int | None = None
    month: int | None = None
    full_year: bool = False

    @property
    def metric(self) -> str:
        return self.metrics[0]


@dataclass(frozen=True)
class Answer:
    text: str
    value: int
    product: str
    date_label: str
    metric: str


PRODUCT_ALIASES = {
    "gold kiwifruit": "Gold kiwifruit",
    "green kiwifruit": "Green kiwifruit",
    "kiwifruit": "Kiwifruit",
    "apple": "Apple",
    "fruit": "Fruit",
    "金果": "Gold kiwifruit",
    "绿果": "Green kiwifruit",
    "奇异果": "Kiwifruit",
    "猕猴桃": "Kiwifruit",
    "苹果": "Apple",
    "水果": "Fruit",
}

METRIC_ALIASES = [
    ("Spend (RMB 000)", ["spend", "sales value", "销售额", "销额", "金额", "value"]),
    ("Volume (000 kg)", ["volume", "销量", "销售量"]),
    ("Penetration %", ["penetration", "渗透率", "渗透"]),
]


def interpret_question(question: str, products: list[str] | None = None) -> InterpretedQuestion:
    product = _find_product(question, products or [])
    date_label = _find_date_label(question)
    year, month = _find_year_month(question)
    metrics = _find_metrics(question)
    return InterpretedQuestion(
        product=product,
        metrics=metrics,
        date_label=date_label,
        year=year,
        month=month,
        full_year=_asks_full_year(question),
    )


def answer_question(question: str, table: KeyMeasuresTable | MultiKpiTable) -> Answer:
    products = table.products
    interpreted = interpret_question(question, products)
    if interpreted.product is None:
        available = "、".join(products[:12])
        requested = _likely_requested_product_text(question)
        if requested:
            raise ValueError(
                f"当前已读取报表中没有“{requested}”。当前表可选产品包括：{available}。"
                "请在左侧 Ready-to-Use 分类中切换到包含该产品/品牌的表后，再点击“读取所选报表”。"
            )
        raise ValueError(f"没有在当前报表中识别到产品。当前可选产品包括：{available}")

    first_table = _first_table(table)
    date_label = _resolve_date(interpreted, first_table)
    metric_tables = _metric_tables(interpreted.metrics, table)
    requested_available = [(metric, metric_table) for metric, metric_table in metric_tables if metric_table is not None]
    missing_metrics = [metric for metric, metric_table in metric_tables if metric_table is None]

    if not requested_available:
        missing_names = "、".join(_metric_label(metric) for metric in missing_metrics)
        raise KeyError(
            f"当前读取的报表只包含 {_available_metrics_text(table)}，没有包含 {missing_names}。"
            "请在刷新时勾选“读取全部 KPI”，或在 Data Explorer 中切换到包含这些指标的表后再查询。"
        )

    answer_lines = []
    first_value = 0
    first_metric = requested_available[0][0]
    for metric, metric_table in requested_available:
        assert metric_table is not None
        metric_date_label = _resolve_date(interpreted, metric_table)
        value = metric_table.value_for(
            product=interpreted.product,
            date_label=metric_date_label,
            metric=metric_table.metric,
        )
        if not answer_lines:
            first_value = value
            date_label = metric_date_label
        answer_lines.append(f"{_metric_label(metric)}：{value:,}（{metric_date_label}）")

    base = f"{interpreted.product} 在 {date_label}：\n" + "\n".join(answer_lines)

    if missing_metrics:
        missing_names = "、".join(_metric_label(metric) for metric in missing_metrics)
        base += (
            f"\n\n{missing_names} 暂时无法从当前缓存表中读取；"
            f"当前读取的报表只包含 {_available_metrics_text(table)}。"
            "请勾选“读取全部 KPI”后重新读取报表。"
        )

    return Answer(
        text=base,
        value=first_value,
        product=interpreted.product,
        date_label=date_label,
        metric=first_metric,
    )


def _first_table(table: KeyMeasuresTable | MultiKpiTable) -> KeyMeasuresTable:
    if isinstance(table, KeyMeasuresTable):
        return table
    for metric_table in table.tables.values():
        return metric_table
    raise KeyError("当前没有缓存表。")


def _metric_tables(
    metrics: list[str],
    table: KeyMeasuresTable | MultiKpiTable,
) -> list[tuple[str, KeyMeasuresTable | None]]:
    if isinstance(table, KeyMeasuresTable):
        return [(metric, table if _metric_matches(metric, table.metric) else None) for metric in metrics]
    return [(metric, table.table_for_metric(metric)) for metric in metrics]


def _available_metrics_text(table: KeyMeasuresTable | MultiKpiTable) -> str:
    if isinstance(table, KeyMeasuresTable):
        return table.metric
    return "、".join(table.metrics)


def _resolve_date(interpreted: InterpretedQuestion, table: KeyMeasuresTable) -> str:
    if interpreted.date_label:
        return interpreted.date_label
    if interpreted.year and interpreted.month:
        return table.date_for_year_month(interpreted.year, interpreted.month)
    if interpreted.year:
        return _date_for_year(table, interpreted.year)
    return table.dates[-1]


def _date_for_year(table: KeyMeasuresTable, year: int) -> str:
    matches = []
    for label in table.dates:
        parsed = datetime.strptime(label, "%d-%b-%y")
        if parsed.year == year:
            matches.append((parsed, label))
    if not matches:
        raise KeyError(f"No date found for {year}")
    return sorted(matches)[-1][1]


def _find_product(question: str, products: list[str]) -> str | None:
    normalized_question = _normalize(question)

    matches = [
        product
        for product in products
        if _normalize(product) and _normalize(product) in normalized_question
    ]
    if matches:
        return sorted(matches, key=len, reverse=True)[0]

    token_matches = _product_token_matches(question, products)
    if token_matches:
        return sorted(token_matches, key=len)[0]

    for alias, product in sorted(PRODUCT_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if _normalize(alias) in normalized_question:
            return product

    if products and len(products) == 1:
        return products[0]
    return None


def _product_token_matches(question: str, products: list[str]) -> list[str]:
    tokens = [
        _normalize(token)
        for token in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", question)
        if len(_normalize(token)) >= 3 and not re.fullmatch(r"20\d{2}p?\d*", token.lower())
    ]
    matches: list[str] = []
    for token in tokens:
        for product in products:
            candidate = _normalize(product)
            if candidate.startswith(token) or token in candidate:
                matches.append(product)
    return matches


def _likely_requested_product_text(question: str) -> str | None:
    protected = re.sub(r"20\d{2}\s*[Pp]?\s*\d*", " ", question)
    tokens = [
        token.strip()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9 ]*[A-Za-z0-9]", protected)
        if len(token.strip()) >= 3
    ]
    return sorted(tokens, key=len, reverse=True)[0] if tokens else None


def _find_metrics(question: str) -> list[str]:
    metrics: list[str] = []
    lowered = question.lower()
    for metric, aliases in METRIC_ALIASES:
        if any(alias.lower() in lowered for alias in aliases):
            metrics.append(metric)
    return metrics or ["Spend (RMB 000)"]


def _metric_label(metric: str) -> str:
    normalized = _normalize(metric)
    if "spend" in normalized or "value" in normalized:
        return "销额"
    if "volume" in normalized:
        return "销量"
    if "penetration" in normalized:
        return "渗透率"
    return metric


def _metric_matches(requested: str, available: str) -> bool:
    requested_normalized = _normalize(requested)
    available_normalized = _normalize(available)
    if (
        requested_normalized == available_normalized
        or requested_normalized in available_normalized
        or available_normalized in requested_normalized
    ):
        return True
    return any(keyword in available_normalized for keyword in _metric_keywords(requested))


def _metric_keywords(metric: str) -> list[str]:
    normalized = _normalize(metric)
    if "spend" in normalized or "value" in normalized or "销额" in normalized or "销售额" in normalized:
        return ["spend", "value"]
    if "volume" in normalized or "销量" in normalized or "销售量" in normalized:
        return ["volume"]
    if "penetration" in normalized or "渗透" in normalized:
        return ["penetration"]
    return [normalized]


def _find_date_label(question: str) -> str | None:
    match = re.search(r"\b\d{1,2}-[A-Za-z]{3}-\d{2}\b", question)
    if not match:
        return None
    day, month, year = match.group(0).split("-")
    return f"{int(day):02d}-{month.title()}-{year}"


def _find_year_month(question: str) -> tuple[int | None, int | None]:
    period = re.search(r"(20\d{2})\s*[Pp]\s*(\d{1,2})", question)
    if period:
        return int(period.group(1)), int(period.group(2))

    chinese = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月", question)
    if chinese:
        return int(chinese.group(1)), int(chinese.group(2))

    year = re.search(r"(20\d{2})\s*年?", question)
    if year:
        return int(year.group(1)), None

    english = re.search(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(20\d{2})",
        question,
        flags=re.IGNORECASE,
    )
    if english:
        month = _month_number(english.group(1))
        return int(english.group(2)), month

    return None, None


def _asks_full_year(question: str) -> bool:
    return any(token in question for token in ["全年", "年度", "整年"])


def _month_number(month_name: str) -> int:
    return {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }[month_name[:3].lower()]


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.lower())
