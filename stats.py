"""Обезличенная статистика использования.

Храним ТОЛЬКО агрегированные счётчики по дням: сколько запросов, сколько
загрузок, сколько байт отдано. Никаких URL, заголовков, IP, User-Agent —
ничего, что связывало бы событие с человеком. Это совместимо с обещанием
«мы не храним вашу историю»: по этим числам нельзя восстановить, кто и что
скачивал.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path

import config

_DB_PATH = config.DATA_DIR / "stats.sqlite3"
_lock = threading.Lock()

# счётчики, которые умеем инкрементировать
COUNTERS = ("api_info", "downloads_started", "downloads_done",
            "downloads_failed", "files_served", "bytes_served")

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS daily (
    day TEXT PRIMARY KEY,
    {", ".join(f"{c} INTEGER NOT NULL DEFAULT 0" for c in COUNTERS)}
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)


def bump(counter: str, amount: int = 1) -> None:
    """Увеличить счётчик за сегодня. Статистика не должна ронять запрос,
    поэтому любые ошибки БД проглатываются."""
    if counter not in COUNTERS or amount <= 0:
        return
    today = date.today().isoformat()
    try:
        with _lock, _connect() as conn:
            conn.execute("INSERT OR IGNORE INTO daily(day) VALUES (?)", (today,))
            conn.execute(
                f"UPDATE daily SET {counter} = {counter} + ? WHERE day = ?",
                (amount, today))
    except sqlite3.Error:
        pass


def _sum_since(conn: sqlite3.Connection, since: str | None) -> dict:
    cols = ", ".join(f"COALESCE(SUM({c}), 0)" for c in COUNTERS)
    if since:
        row = conn.execute(f"SELECT {cols} FROM daily WHERE day >= ?", (since,)).fetchone()
    else:
        row = conn.execute(f"SELECT {cols} FROM daily").fetchone()
    return dict(zip(COUNTERS, row))


def summary() -> dict:
    """Итоги за сегодня / неделю / месяц / всё время + ряд по дням."""
    today = date.today()
    try:
        with _lock, _connect() as conn:
            result = {
                "today": _sum_since(conn, today.isoformat()),
                "week": _sum_since(conn, (today - timedelta(days=6)).isoformat()),
                "month": _sum_since(conn, (today - timedelta(days=29)).isoformat()),
                "all": _sum_since(conn, None),
            }
            # ряд за 30 дней для графика (пропуски заполняем нулями)
            since = (today - timedelta(days=29)).isoformat()
            rows = {r[0]: r[1:] for r in conn.execute(
                f"SELECT day, {', '.join(COUNTERS)} FROM daily "
                "WHERE day >= ? ORDER BY day", (since,))}
            series = []
            for i in range(30):
                d = (today - timedelta(days=29 - i)).isoformat()
                vals = rows.get(d)
                series.append({"day": d, **(dict(zip(COUNTERS, vals)) if vals
                                            else {c: 0 for c in COUNTERS})})
            result["series"] = series
            first = conn.execute("SELECT MIN(day) FROM daily").fetchone()[0]
            result["since"] = first
            return result
    except sqlite3.Error:
        empty = {c: 0 for c in COUNTERS}
        return {"today": empty, "week": empty, "month": empty, "all": empty,
                "series": [], "since": None}
