from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


class DataStore:
    """SQLite-backed record of every numeric cell the tool has pulled.

    First step of the "data lands locally, answers query locally"
    architecture: each successful pivot query appends its cells here, so
    conversation exports survive process restarts and follow-up aggregation
    can eventually be answered without re-driving the browser.

    One row per (account, report_set, report, metric, member, date);
    re-pulling the same cell overwrites it with the freshest value. Data is
    isolated per Worldpanel account — one user never sees another's rows.
    """

    def __init__(self, path: str = "runtime/wpo-data.sqlite3"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS facts (
                account TEXT NOT NULL,
                report_set TEXT NOT NULL,
                report TEXT NOT NULL,
                metric TEXT NOT NULL,
                member TEXT NOT NULL,
                date TEXT NOT NULL,
                value REAL NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (account, report_set, report, metric, member, date)
            )
            """
        )
        self._connection.commit()

    def record(
        self,
        account: str,
        report_set: str,
        report: str,
        metric: str,
        rows: Iterable[tuple[str, str, float]],
    ) -> int:
        """Store (member, date, value) cells; returns how many were written."""
        stamp = datetime.now(timezone.utc).isoformat()
        payload = [
            (account, report_set, report, metric, member, date, float(value), stamp)
            for member, date, value in rows
            if value is not None
        ]
        if not payload:
            return 0
        with self._lock:
            self._connection.executemany(
                "INSERT OR REPLACE INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            self._connection.commit()
        return len(payload)

    def catalog(self, account: str, member_cap: int = 300) -> dict[str, object]:
        """What this account's local data can answer: the distinct metrics,
        members, and date labels on record, plus freshness. Used to let the
        LLM decide whether a question is answerable without the browser."""
        with self._lock:
            metrics = [
                row[0]
                for row in self._connection.execute(
                    "SELECT DISTINCT metric FROM facts WHERE account = ? ORDER BY metric",
                    (account,),
                )
            ]
            members = [
                row[0]
                for row in self._connection.execute(
                    "SELECT DISTINCT member FROM facts WHERE account = ? ORDER BY member"
                    " LIMIT ?",
                    (account, member_cap + 1),
                )
            ]
            dates = [
                row[0]
                for row in self._connection.execute(
                    "SELECT DISTINCT date FROM facts WHERE account = ?",
                    (account,),
                )
            ]
            updated = self._connection.execute(
                "SELECT MAX(recorded_at) FROM facts WHERE account = ?",
                (account,),
            ).fetchone()[0]
        truncated = len(members) > member_cap
        return {
            "metrics": metrics,
            "members": members[:member_cap],
            "members_truncated": truncated,
            "dates": dates,
            "updated_at": updated,
        }

    def fetch_cells(
        self,
        account: str,
        metric: str,
        members: list[str],
        dates: list[str],
    ) -> dict[tuple[str, str], float] | None:
        """Return {(member, date): value} for the requested grid.

        Returns None when the same cell exists under different reports with
        conflicting values — that ambiguity must fall back to a live pull."""
        if not members or not dates:
            return None
        member_marks = ",".join("?" for _ in members)
        date_marks = ",".join("?" for _ in dates)
        with self._lock:
            rows = self._connection.execute(
                "SELECT member, date, value FROM facts"
                f" WHERE account = ? AND metric = ? AND member IN ({member_marks})"
                f" AND date IN ({date_marks})",
                (account, metric, *members, *dates),
            ).fetchall()
        cells: dict[tuple[str, str], float] = {}
        for member, date, value in rows:
            key = (member, date)
            if key in cells and cells[key] != value:
                return None
            cells[key] = value
        return cells

    def export_rows(self, account: str) -> list[tuple[str, str, str, str, str, float]]:
        """Every (report_set, report, metric, member, date, value) this
        account has ever pulled, for whole-history CSV export."""
        with self._lock:
            cursor = self._connection.execute(
                "SELECT report_set, report, metric, member, date, value FROM facts"
                " WHERE account = ?"
                " ORDER BY report_set, report, metric, member, date",
                (account,),
            )
            return [tuple(row) for row in cursor.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._connection.close()
