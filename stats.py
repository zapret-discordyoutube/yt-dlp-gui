"""Обезличенная статистика использования.

Храним ТОЛЬКО агрегированные счётчики по дням: сколько запросов, сколько
загрузок, сколько байт отдано. Никаких URL, заголовков, IP, User-Agent —
ничего, что связывало бы событие с человеком. Это совместимо с обещанием
«мы не храним вашу историю»: по этим числам нельзя восстановить, кто и что
скачивал.
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
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

-- Публичная лента «что скачивают».
-- Сознательно храним ТОЛЬКО адрес, даты и счётчик: ни названия, ни обложки,
-- ни размера, ни выбранного формата. Название и картинку показывает браузер,
-- забирая их напрямую с источника. И, разумеется, никакой связи с тем,
-- КТО скачивал — ни IP, ни сессии.
CREATE TABLE IF NOT EXISTS feed (
    url        TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS feed_last  ON feed(last_seen DESC);
CREATE INDEX IF NOT EXISTS feed_count ON feed(count DESC);
"""


@contextlib.contextmanager
def _connect():
    """Соединение с гарантированным закрытием.

    sqlite3.Connection.__exit__ фиксирует транзакцию, но НЕ закрывает
    соединение: дескрипторы держались до сборки мусора и при нагрузке
    упирались в лимит открытых файлов процесса.
    """
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        with conn:
            yield conn
    finally:
        conn.close()


def init() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock, _connect() as conn:
        # WAL — персистентное свойство файла, задавать его на каждом
        # соединении не нужно.
        conn.execute("PRAGMA journal_mode=WAL")
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


def record_download(url: str) -> None:
    """Отметить факт скачивания ролика. Сохраняем адрес, дату и счётчик."""
    if not config.FEED_ENABLED or not url:
        return
    # Точность до микросекунд, а не до секунды: иначе записи, сделанные в
    # одну секунду, получают одинаковую метку, и порядок «недавних»
    # становится произвольным.
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _lock, _connect() as conn:
            conn.execute(
                "INSERT INTO feed(url, first_seen, last_seen, count) "
                "VALUES (?, ?, ?, 1) "
                "ON CONFLICT(url) DO UPDATE SET "
                "  last_seen = excluded.last_seen, count = count + 1",
                (url, now, now))
    except sqlite3.Error:
        pass


def selftest() -> None:
    """Проверить, что БД действительно пишется. Ошибки НЕ глушим:
    это единственное место, где сбой хранилища должен быть заметен."""
    with _lock, _connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS _probe (k INTEGER PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO _probe(k) VALUES (1)")
        conn.execute("DELETE FROM _probe")


def migrate_feed(canonicalize) -> int:
    """Привести уже накопленные ссылки к каноническому виду.

    Записи, схлопнувшиеся в одну, объединяются: счётчики складываются,
    первая дата берётся самая ранняя, последняя — самая поздняя.
    Функция канонизации передаётся снаружи, чтобы этот модуль не зависел
    от логики разбора ссылок.
    """
    try:
        with _lock, _connect() as conn:
            rows = conn.execute(
                "SELECT url, first_seen, last_seen, count FROM feed").fetchall()
            merged: dict[str, list] = {}
            changed = False
            for url, first, last, cnt in rows:
                key = canonicalize(url)
                if key != url:
                    changed = True
                if key in merged:
                    m = merged[key]
                    m[0] = min(m[0], first)
                    m[1] = max(m[1], last)
                    m[2] += cnt
                else:
                    merged[key] = [first, last, cnt]
            if not changed:
                return 0
            conn.execute("DELETE FROM feed")
            conn.executemany(
                "INSERT INTO feed(url, first_seen, last_seen, count) "
                "VALUES (?, ?, ?, ?)",
                [(u, v[0], v[1], v[2]) for u, v in merged.items()])
            return len(rows) - len(merged)
    except sqlite3.Error:
        return 0


def feed(order: str = "recent", limit: int = 50, offset: int = 0) -> list[dict]:
    """Лента: последние или самые популярные."""
    if not config.FEED_ENABLED:
        return []
    col = "count DESC, last_seen DESC" if order == "popular" else "last_seen DESC"
    limit = max(1, min(int(limit), 200))
    try:
        with _lock, _connect() as conn:
            rows = conn.execute(
                f"SELECT url, first_seen, last_seen, count FROM feed "
                f"ORDER BY {col} LIMIT ? OFFSET ?", (limit, max(0, int(offset)))
            ).fetchall()
        return [{"url": r[0], "first_seen": r[1], "last_seen": r[2], "count": r[3]}
                for r in rows]
    except sqlite3.Error:
        return []


def feed_totals() -> dict:
    try:
        with _lock, _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(count), 0) FROM feed").fetchone()
        return {"unique": row[0], "total": row[1]}
    except sqlite3.Error:
        return {"unique": 0, "total": 0}


def _sum_since(conn: sqlite3.Connection, since: str | None) -> dict:
    cols = ", ".join(f"COALESCE(SUM({c}), 0)" for c in COUNTERS)
    if since:
        row = conn.execute(f"SELECT {cols} FROM daily WHERE day >= ?", (since,)).fetchone()
    else:
        row = conn.execute(f"SELECT {cols} FROM daily").fetchone()
    return dict(zip(COUNTERS, row))


def summary() -> dict:
    """Ряд по дням за 30 суток + итог за всё время.

    Суммы за сегодня/неделю/месяц сознательно НЕ считаем: их браузер
    получает из того же ряда сложением. Это экономит три агрегирующих
    запроса к БД на каждое обращение, а данные и так уже переданы.
    """
    today = date.today()
    try:
        with _lock, _connect() as conn:
            result = {"all": _sum_since(conn, None)}
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
