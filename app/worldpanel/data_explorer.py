from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Iterable

from app.worldpanel.multitable import MultiKpiTable
from app.worldpanel.parser import KeyMeasuresTable


@dataclass(frozen=True)
class DataExplorerOption:
    label: str
    value: str


@dataclass(frozen=True)
class DataExplorerDimension:
    key: str
    label: str
    control_id: str
    current: str
    options: tuple[DataExplorerOption, ...]
    position: int = 0


@dataclass(frozen=True)
class DataExplorerSegment:
    label: str
    control_id: str
    onclick: str = ""


@dataclass(frozen=True)
class DataExplorerControls:
    dimensions: tuple[DataExplorerDimension, ...]
    pivot_button_id: str | None = None
    segments: tuple[DataExplorerSegment, ...] = ()
    pivot_slots: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class DataExplorerContext:
    report_set: str
    report_name: str
    report_parameter: str
    required_dimensions: tuple[str, ...] = ("channel",)
    dimensions: dict[str, DataExplorerDimension] = field(default_factory=dict)
    segments: tuple[DataExplorerSegment, ...] = ()
    pivot_slots: dict[str, tuple[str, ...]] = field(default_factory=dict)
    current_selections: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class QuerySpec:
    products: Iterable[str] = ()
    metrics: Iterable[str] = ("Spend (RMB 000)",)
    year: int | None = None
    month: int | None = None
    full_year: bool = False
    dimensions: dict[str, str] = field(default_factory=dict)
    segments: tuple[str, ...] = ()


@dataclass(frozen=True)
class Clarification:
    dimension_key: str
    question: str
    options: tuple[DataExplorerOption, ...]
    spec: QuerySpec


def parse_query_spec(question: str) -> QuerySpec:
    products = _find_products(question)
    metrics = _find_metrics(question)
    year, month = _find_year_month(question)
    dimensions = _find_dimensions(question)
    return QuerySpec(
        products=products,
        metrics=metrics,
        year=year,
        month=month,
        full_year=any(token in question for token in ["全年", "年度", "整年"]),
        dimensions=dimensions,
    )


def plan_query(spec: QuerySpec, context: DataExplorerContext) -> QuerySpec | Clarification:
    products = list(spec.products)
    product_dimension = context.dimensions.get("product")
    if products and not product_dimension:
        segment = _match_segment(products[0], context.segments)
        if segment:
            return _spec_with_segment(spec, segment.label)

    if products and product_dimension and not _match_dimension_option(products[0], product_dimension):
        segment = _match_segment(products[0], context.segments)
        if segment:
            return _spec_with_segment(spec, segment.label)
        return Clarification(
            dimension_key="product",
            question=f"请选择 {product_dimension.label}",
            options=product_dimension.options,
            spec=spec,
        )

    for key, requested in spec.dimensions.items():
        dimension = context.dimensions.get(key)
        if dimension and not _match_dimension_option(requested, dimension):
            return Clarification(
                dimension_key=key,
                question=f"请选择 {dimension.label}",
                options=dimension.options,
                spec=spec,
            )

    for key in context.required_dimensions:
        dimension = context.dimensions.get(key)
        if not dimension:
            continue
        requested = spec.dimensions.get(key)
        if requested and _match_dimension_option(requested, dimension):
            continue
        return Clarification(
            dimension_key=key,
            question=f"请选择 {dimension.label}",
            options=dimension.options,
            spec=spec,
        )
    return spec


def _match_segment(requested: str, segments: tuple[DataExplorerSegment, ...]) -> DataExplorerSegment | None:
    requested_normalized = _normalize(requested)
    for segment in segments:
        segment_normalized = _normalize(segment.label)
        if (
            requested_normalized == segment_normalized
            or requested_normalized in segment_normalized
            or segment_normalized in requested_normalized
        ):
            return segment
    return None


def _spec_with_segment(spec: QuerySpec, segment: str) -> QuerySpec:
    segments = tuple(dict.fromkeys((*spec.segments, segment)))
    return QuerySpec(
        products=(),
        metrics=spec.metrics,
        year=spec.year,
        month=spec.month,
        full_year=spec.full_year,
        dimensions=spec.dimensions,
        segments=segments,
    )


def _match_dimension_option(requested: str, dimension: DataExplorerDimension) -> DataExplorerOption | None:
    requested_normalized = _normalize(requested)
    for option in dimension.options:
        option_normalized = _normalize(option.label)
        if (
            requested_normalized == option_normalized
            or requested_normalized in option_normalized
            or option_normalized in requested_normalized
        ):
            return option
    return None


class DataExplorerCache:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._tables: dict[tuple[object, ...], KeyMeasuresTable | MultiKpiTable] = {}
        self._load()

    def get(self, report_set: str, report_name: str, spec: QuerySpec) -> KeyMeasuresTable | MultiKpiTable | None:
        return self._tables.get(cache_key(report_set, report_name, spec))

    def set(self, report_set: str, report_name: str, spec: QuerySpec, table: KeyMeasuresTable | MultiKpiTable) -> None:
        self._tables[cache_key(report_set, report_name, spec)] = table
        self._save()

    @property
    def size(self) -> int:
        return len(self._tables)

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for record in payload.get("entries", []):
            try:
                key = _freeze_json_key(record["key"])
                self._tables[key] = _table_from_payload(record["table"])
            except (KeyError, TypeError, ValueError):
                continue

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "entries": [
                {"key": list(key), "table": _table_to_payload(table)}
                for key, table in self._tables.items()
            ],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cache_key(report_set: str, report_name: str, spec: QuerySpec) -> tuple[object, ...]:
    return (
        _normalize(report_set),
        _normalize(report_name),
        tuple(sorted(_normalize(value) for value in spec.products)),
        tuple(sorted(_normalize(value) for value in spec.metrics)),
        spec.year,
        spec.month,
        spec.full_year,
        tuple(sorted((_normalize(key), _normalize(value)) for key, value in spec.dimensions.items())),
        tuple(_normalize(value) for value in spec.segments),
    )


def _table_to_payload(table: KeyMeasuresTable | MultiKpiTable) -> dict[str, object]:
    if isinstance(table, MultiKpiTable):
        return {
            "kind": "multi",
            "tables": {metric: _table_to_payload(metric_table) for metric, metric_table in table.tables.items()},
        }
    return {
        "kind": "key_measures",
        "title": table.title,
        "metric": table.metric,
        "products": table.products,
        "dates": table.dates,
        "rows": table.rows,
    }


def _table_from_payload(payload: dict[str, object]) -> KeyMeasuresTable | MultiKpiTable:
    if payload.get("kind") == "multi":
        tables_payload = payload.get("tables")
        if not isinstance(tables_payload, dict):
            raise ValueError("Invalid multi table payload")
        return MultiKpiTable(
            tables={
                str(metric): _as_key_measures_table(_table_from_payload(table_payload))
                for metric, table_payload in tables_payload.items()
                if isinstance(table_payload, dict)
            }
        )
    rows_payload = payload.get("rows")
    if not isinstance(rows_payload, dict):
        raise ValueError("Invalid key measures payload")
    return KeyMeasuresTable(
        title=str(payload.get("title") or "Key Measures Data Table"),
        metric=str(payload.get("metric") or ""),
        products=[str(value) for value in payload.get("products", [])],
        dates=[str(value) for value in payload.get("dates", [])],
        rows={
            str(date): {str(product): int(value) for product, value in values.items()}
            for date, values in rows_payload.items()
            if isinstance(values, dict)
        },
    )


def _as_key_measures_table(table: KeyMeasuresTable | MultiKpiTable) -> KeyMeasuresTable:
    if isinstance(table, KeyMeasuresTable):
        return table
    raise ValueError("Nested multi KPI tables are not supported")


def _freeze_json_key(value: object) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise ValueError("Invalid cache key")
    return tuple(_freeze_json_value(item) for item in value)


def _freeze_json_value(value: object) -> object:
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def discover_controls_from_html(html: str) -> DataExplorerControls:
    parser = _ControlParser()
    parser.feed(html)
    return parser.controls()


def apply_clarification(spec: QuerySpec, dimension_key: str, value: str) -> QuerySpec:
    products = (value,) if dimension_key == "product" else spec.products
    dimensions = dict(spec.dimensions)
    if dimension_key != "product":
        dimensions[dimension_key] = value
    return QuerySpec(
        products=products,
        metrics=spec.metrics,
        year=spec.year,
        month=spec.month,
        full_year=spec.full_year,
        dimensions=dimensions,
        segments=spec.segments,
    )


def _find_products(question: str) -> list[str]:
    normalized = _normalize(question)
    known = [
        "Coke TM",
        "Coke Zero",
        "Coke Regular",
        "TTL SPKL",
        "TCCC SPKL",
        "Kiwifruit",
        "Gold kiwifruit",
        "Green kiwifruit",
    ]
    for product in known:
        if _normalize(product) in normalized:
            return [product]

    candidates = [
        token.strip()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9 ]*[A-Za-z0-9]", question)
        if len(token.strip()) >= 3 and token.strip().lower() not in {"hyper", "cvs", "all"}
    ]
    return [candidates[0]] if candidates else []


def _find_metrics(question: str) -> list[str]:
    metrics: list[str] = []
    lowered = question.lower()
    if any(token in lowered for token in ["spend", "value", "销额", "销售额", "金额"]):
        metrics.append("Spend (RMB 000)")
    if any(token in lowered for token in ["volume", "销量", "销售量"]):
        metrics.append("Volume (000 kg)")
    if any(token in lowered for token in ["penetration", "渗透率", "渗透"]):
        metrics.append("Penetration %")
    return metrics or ["Spend (RMB 000)"]


def _find_year_month(question: str) -> tuple[int | None, int | None]:
    period = re.search(r"(20\d{2})\s*[Pp]\s*(\d{1,2})", question)
    if period:
        return int(period.group(1)), int(period.group(2))
    chinese_month = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月", question)
    if chinese_month:
        return int(chinese_month.group(1)), int(chinese_month.group(2))
    year = re.search(r"(20\d{2})", question)
    if year:
        return int(year.group(1)), None
    return None, None


def _find_dimensions(question: str) -> dict[str, str]:
    dimensions: dict[str, str] = {}
    for key, aliases, values in [
        ("channel", ["渠道", "channel"], ["Hyper", "CVS", "All", "Online", "Offline"]),
        ("category", ["品类", "category"], ["Sparkling", "NARTD", "SPKL", "P.Water"]),
        ("classification", ["产品分类", "classification"], ["Manufacturer", "Pack Type", "Unit Size"]),
    ]:
        lowered = question.lower()
        if any(alias.lower() in lowered for alias in aliases):
            for value in values:
                if _normalize(value) in _normalize(question):
                    dimensions[key] = value
                    break
    return dimensions


def _normalize(value: object) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value).lower())


class _ControlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._selects: list[dict[str, object]] = []
        self._current_select: dict[str, object] | None = None
        self._current_option: dict[str, object] | None = None
        self._pivot_button_id: str | None = None
        self._segments: list[DataExplorerSegment] = []
        self._capture_segment: dict[str, str] | None = None
        self._next_segment_control: dict[str, str] | None = None
        self._pivot_slot_stack: list[tuple[str, str]] = []
        self._pivot_slots: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {key.lower(): value or "" for key, value in attrs}
        slot = _pivot_slot_from_attrs(attr)
        if slot:
            self._pivot_slot_stack.append((tag, slot))
        if tag == "select":
            self._current_select = {
                "id": attr.get("id") or attr.get("name") or f"select-{len(self._selects)}",
                "name": attr.get("name") or "",
                "options": [],
                "pivot_slot": slot or self._current_pivot_slot(),
            }
        elif tag == "option" and self._current_select is not None:
            self._current_option = {
                "label": "",
                "value": attr.get("value", ""),
                "selected": "selected" in attr,
            }
        else:
            textish = " ".join([attr.get("id", ""), attr.get("value", ""), attr.get("title", ""), attr.get("onclick", "")])
            if "pivot" in textish.lower() and not self._pivot_button_id:
                self._pivot_button_id = attr.get("id") or attr.get("name") or ""
            onclick = attr.get("onclick", "")
            class_name = attr.get("class", "")
            if "rtplus" in class_name.lower() or "plus" in class_name.lower():
                self._next_segment_control = {"id": attr.get("id") or attr.get("data-node-id") or "", "onclick": onclick}
            if (
                "+" in attr.get("value", "")
                or "segment" in onclick.lower()
                or "expand" in onclick.lower()
                or "treeview" in onclick.lower()
            ):
                self._capture_segment = {
                    "id": attr.get("id") or attr.get("data-node-id") or "",
                    "onclick": onclick,
                    "label": attr.get("value", ""),
                }

    def handle_data(self, data: str) -> None:
        if self._current_option is not None:
            self._current_option["label"] = str(self._current_option["label"]) + data
        if self._capture_segment is not None:
            self._capture_segment["label"] += data
        elif self._next_segment_control is not None and _clean_label(data):
            self._segments.append(
                DataExplorerSegment(
                    label=_clean_label(data).lstrip("+").strip(),
                    control_id=self._next_segment_control["id"],
                    onclick=self._next_segment_control["onclick"],
                )
            )
            self._next_segment_control = None

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "option" and self._current_option is not None and self._current_select is not None:
            options = self._current_select["options"]
            assert isinstance(options, list)
            options.append(self._current_option)
            self._current_option = None
        elif tag == "select" and self._current_select is not None:
            self._selects.append(self._current_select)
            self._current_select = None
        elif self._capture_segment is not None:
            label = _clean_label(self._capture_segment["label"])
            if _looks_like_segment_label(label):
                self._segments.append(
                    DataExplorerSegment(
                        label=label.lstrip("+").strip(),
                        control_id=self._capture_segment["id"],
                        onclick=self._capture_segment["onclick"],
                    )
                )
            self._capture_segment = None
        if self._pivot_slot_stack and self._pivot_slot_stack[-1][0] == tag:
            self._pivot_slot_stack.pop()

    def controls(self) -> DataExplorerControls:
        dimensions = []
        for index, record in enumerate(self._selects):
            options = tuple(
                DataExplorerOption(label=_clean_label(option["label"]), value=str(option["value"]) or _clean_label(option["label"]))
                for option in record["options"]  # type: ignore[index]
            )
            key = _dimension_key_from_options(
                index,
                str(record["id"]),
                str(record["name"]),
                tuple(option.label for option in options),
            )
            selected = next(
                (_clean_label(option["label"]) for option in record["options"] if option.get("selected")),  # type: ignore[union-attr]
                options[0].label if options else "",
            )
            pivot_slot = str(record.get("pivot_slot") or "")
            if pivot_slot:
                self._pivot_slots.setdefault(pivot_slot, [])
                self._pivot_slots[pivot_slot].extend(option.label for option in options if option.label)
            dimensions.append(
                DataExplorerDimension(
                    key=key,
                    label=_dimension_label_for_key(key, str(record["id"]), str(record["name"]), index),
                    control_id=str(record["id"]),
                    current=selected,
                    options=options,
                    position=index,
                )
            )
        return DataExplorerControls(
            dimensions=tuple(dimensions),
            pivot_button_id=self._pivot_button_id,
            segments=tuple(self._segments),
            pivot_slots={key: tuple(dict.fromkeys(values)) for key, values in self._pivot_slots.items()},
        )

    def _current_pivot_slot(self) -> str | None:
        if not self._pivot_slot_stack:
            return None
        return self._pivot_slot_stack[-1][1]


def _pivot_slot_from_attrs(attr: dict[str, str]) -> str | None:
    value = _normalize(" ".join([attr.get("id", ""), attr.get("name", ""), attr.get("class", ""), attr.get("title", "")]))
    if "pivotcolumn" in value or "columndim" in value or value in {"column", "columns"}:
        return "column"
    if "pivotrow" in value or "rowdim" in value or value in {"row", "rows"}:
        return "row"
    return None


def _dimension_key_from_options(index: int, control_id: str, name: str, option_labels: tuple[str, ...]) -> str:
    combined = _normalize(" ".join(option_labels))
    if any(token in combined for token in ["spend", "volume", "penetration", "buyers", "frequency"]):
        return "kpi"
    if any(token in combined for token in ["actualyryr", "yronyr", "periodonperiod", "difference"]):
        return "calculation"
    if any(token in combined for token in ["ttlchannel", "hyper", "supermini", "cvs", "gt", "restaurant"]):
        return "channel"
    if any(token in combined for token in ["urbanchina", "sccl", "cbl", "region", "city"]):
        return "market"
    if any(token in combined for token in ["52we", "24we", "12we", "ytd"]):
        return "period"
    if any(token in combined for token in ["coketm", "ttlspkl", "tcccspkl", "regular", "zero"]):
        return "product"
    if any(token in combined for token in ["unitsize", "399ml", "600ml", "packtype", "manufacturer"]):
        return "classification"
    return _dimension_key(index, control_id, name)


def _dimension_label_for_key(key: str, control_id: str, name: str, index: int) -> str:
    return {
        "kpi": "KPI",
        "product": "Product",
        "period": "Time",
        "category": "Category",
        "channel": "Channel",
        "classification": "Product Classification",
        "calculation": "Calculation",
        "market": "Market",
    }.get(key, control_id or name or f"Dimension {index + 1}")


def _dimension_key(index: int, control_id: str, name: str) -> str:
    value = f"{control_id} {name}".lower()
    if "kpi" in value or "measure" in value:
        return "kpi"
    if "product" in value:
        return "product"
    if "period" in value or "time" in value:
        return "period"
    if "category" in value:
        return "category"
    if "channel" in value or "retailer" in value:
        return "channel"
    if "classification" in value or "class" in value:
        return "classification"
    if index == 0:
        return "kpi"
    return f"dimension_{index}"


def _clean_label(value: object) -> str:
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def _looks_like_segment_label(value: str) -> bool:
    label = value.lstrip("+").strip()
    if not label:
        return False
    if len(label) > 80:
        return False
    if re.fullmatch(r"[A-Za-z0-9+/=]{24,}", label):
        return False
    if sum(ch in "+/=" for ch in label) >= 3 and len(label) > 16:
        return False
    return bool(re.search(r"[A-Za-z0-9\u4e00-\u9fff]", label))
