from __future__ import annotations

from dataclasses import dataclass

from app.worldpanel.parser import KeyMeasuresTable


@dataclass(frozen=True)
class DataIssue:
    kind: str
    severity: str
    message: str
    product: str | None = None
    date_label: str | None = None
    value: int | None = None


@dataclass(frozen=True)
class CheckResult:
    status: str
    summary: str
    issues: list[DataIssue]


def check_table(table: KeyMeasuresTable) -> CheckResult:
    issues: list[DataIssue] = []
    issues.extend(_negative_value_issues(table))
    issues.extend(_large_change_issues(table))

    status = "pass" if not issues else "warning"
    summary = (
        f"检查完成：{len(table.products)} 个产品，{len(table.dates)} 个日期，发现 {len(issues)} 个需要关注的问题。"
        if issues
        else f"检查完成：{len(table.products)} 个产品，{len(table.dates)} 个日期，未发现明显问题。"
    )
    return CheckResult(status=status, summary=summary, issues=issues)


def _negative_value_issues(table: KeyMeasuresTable) -> list[DataIssue]:
    issues: list[DataIssue] = []
    for date_label, values in table.rows.items():
        for product, value in values.items():
            if value < 0:
                issues.append(
                    DataIssue(
                        kind="negative_value",
                        severity="high",
                        message=f"{product} 在 {date_label} 出现负值 {value:,}。",
                        product=product,
                        date_label=date_label,
                        value=value,
                    )
                )
    return issues


def _large_change_issues(table: KeyMeasuresTable) -> list[DataIssue]:
    issues: list[DataIssue] = []
    if len(table.dates) < 2:
        return issues

    for product in table.products:
        previous_value: int | None = None
        previous_date: str | None = None
        for date_label in table.dates:
            value = table.rows[date_label][product]
            if previous_value and previous_value > 0:
                ratio = abs(value - previous_value) / previous_value
                if ratio >= 1.5:
                    issues.append(
                        DataIssue(
                            kind="large_change",
                            severity="medium",
                            message=(
                                f"{product} 从 {previous_date} 到 {date_label} 变化幅度较大："
                                f"{previous_value:,} -> {value:,}。"
                            ),
                            product=product,
                            date_label=date_label,
                            value=value,
                        )
                    )
            previous_value = value
            previous_date = date_label
    return issues
