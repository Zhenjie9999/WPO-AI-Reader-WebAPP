from __future__ import annotations

from html.parser import HTMLParser
import json
import re

from app.worldpanel.pivot_models import DimensionTag, MemberNode, PivotLayout


def parse_pivot_layout(html: str) -> PivotLayout:
    parser = _PivotListParser()
    parser.feed(html)
    return PivotLayout(
        rows=tuple(parser.axes["row"]),
        columns=tuple(parser.axes["column"]),
        filters=tuple(parser.axes["filter"]),
        available=tuple(parser.axes["available"]),
    )


def parse_dimension_tags(html: str) -> tuple[DimensionTag, ...]:
    parser = _PivotListParser()
    parser.feed(html)
    tags: list[DimensionTag] = []
    for axis, labels in parser.raw_axes.items():
        for position, label in enumerate(labels):
            count_match = re.search(r"\[(\d+)\]\s*$", label)
            tags.append(
                DimensionTag(
                    label=re.sub(r"\[\d+\]\s*$", "", label).strip(),
                    dimension_id=parser.ids.get((axis, position), ""),
                    axis=axis,  # type: ignore[arg-type]
                    position=position,
                    member_count=int(count_match.group(1)) if count_match else None,
                )
            )
    return tuple(tags)


def parse_member_tree(html: str) -> tuple[MemberNode, ...]:
    parser = _TreeParser()
    parser.feed(html)
    return tuple(parser.nodes)


def selected_member_paths(html: str) -> tuple[tuple[str, ...], ...]:
    return tuple(node.path for node in parse_member_tree(html) if node.selected or node.checked)


class _PivotListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.axes: dict[str, list[str]] = {"row": [], "column": [], "filter": [], "available": []}
        self.raw_axes: dict[str, list[str]] = {"row": [], "column": [], "filter": [], "available": []}
        self.ids: dict[tuple[str, int], str] = {}
        self._axis: str | None = None
        self._item_id = ""
        self._capture = False
        self._text = ""
        self._title = ""
        self._capture_text = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.casefold(): value or "" for key, value in attrs}
        identifier = " ".join((attr.get("id", ""), attr.get("name", ""), attr.get("class", ""))).casefold()
        if "rowhierarch" in identifier:
            self._axis = "row"
        elif "columnhierarch" in identifier:
            self._axis = "column"
        elif "pagehierarch" in identifier or "filter" in identifier:
            self._axis = "filter"
        elif "available" in identifier or "unused" in identifier:
            self._axis = "available"
        if tag.casefold() == "li" and "rlbitem" in attr.get("class", "").casefold():
            self._capture = True
            self._text = ""
            self._title = attr.get("title", "")
            self._item_id = attr.get("data-value", "") or attr.get("value", "")
        if self._capture and "rlbtext" in attr.get("class", "").casefold():
            self._capture_text = True
        if tag.casefold() == "img" and self._capture:
            self._item_id = attr.get("dimid", "") or self._item_id

    def handle_data(self, data: str) -> None:
        if self._capture and self._capture_text:
            self._text += data

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "li" and self._capture:
            label = " ".join((self._text or self._title).split())
            if label:
                axis = self._axis or "available"
                position = len(self.axes[axis])
                self.raw_axes[axis].append(label)
                self.axes[axis].append(re.sub(r"\[\d+\]\s*$", "", label).strip())
                self.ids[(axis, position)] = self._item_id
            self._capture = False
            self._text = ""
            self._title = ""
            self._item_id = ""
        elif self._capture_text and tag.casefold() == "span":
            self._capture_text = False


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[MemberNode] = []
        self._stack: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "li" and "rtli" in attr.get("class", "").casefold():
            self._stack.append({
                "label": "",
                "value": attr.get("data-value", ""),
                "has_children": False,
                "expanded": False,
                "checked": "checked" in attr.get("class", "").casefold(),
                "selected": "selected" in attr.get("class", "").casefold(),
            })
        elif self._stack:
            current = self._stack[-1]
            classes = attr.get("class", "").casefold()
            if "rtplus" in classes:
                current["has_children"] = True
            if "rtminus" in classes:
                current["has_children"] = True
                current["expanded"] = True
            if "rtselected" in classes:
                current["selected"] = True
            if tag.casefold() == "input" and attr.get("type", "").casefold() == "checkbox":
                current["checked"] = "checked" in attr
            if "rtin" in classes:
                current["label"] = attr.get("title", "")
                current["value"] = attr.get("data-value", "") or current["value"]

    def handle_data(self, data: str) -> None:
        if self._stack and not self._stack[-1]["label"] and data.strip():
            self._stack[-1]["label"] = " ".join(data.split())

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "li" or not self._stack:
            return
        current = self._stack.pop()
        label = str(current["label"]).strip()
        if label:
            path = (*[str(parent["label"]).strip() for parent in self._stack if parent["label"]], label)
            self.nodes.append(
                MemberNode(
                    label=label,
                    value=str(current["value"]) or label,
                    path=path,
                    level=len(path) - 1,
                    has_children=bool(current["has_children"]),
                    expanded=bool(current["expanded"]),
                    checked=bool(current["checked"]),
                    selected=bool(current["selected"]),
                )
            )
