"""Обезличенная статистика использования.

Храним три вида записей и ничего больше:
 * суточные счётчики (запросы, загрузки, отданные байты);
 * ленту скачанного: адрес ролика, первая и последняя даты, счётчик;
 * времена отдельных скачиваний, ОКРУГЛЁННЫЕ ДО МИНУТЫ.

Ни IP, ни User-Agent, ни заголовков, ни сессий. Округление времени —
не косметика: при небольшом трафике точная метка однозначно выделяет
сеанс, и тот, кто знает, когда человек заходил, узнал бы, что он скачал.
Округление до минуты сохраняет смысл «когда это было» и убирает
возможность сопоставления по совпадению момента.
"""
from __future__ import annotations

import base64
import contextlib
import logging
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
-- Составной: одиночный индекс по count не покрывал вторую часть
-- сортировки, и SQLite достраивал временное B-дерево.
CREATE INDEX IF NOT EXISTS feed_count ON feed(count DESC, last_seen DESC, url DESC);

-- Отдельные события: время КАЖДОГО скачивания, а не только первого и
-- последнего. Здесь по-прежнему нет ничего о том, КТО скачивал — только
-- адрес ролика и момент времени.
CREATE TABLE IF NOT EXISTS events (
    id  INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    at  TEXT NOT NULL
);
-- Отметки о выполненных разовых миграциях: без них каждая из них
-- перечитывала всю ленту при каждом старте сервиса.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS events_url ON events(url, at DESC);
CREATE INDEX IF NOT EXISTS events_at  ON events(at);
"""


_conn: sqlite3.Connection | None = None


@contextlib.contextmanager
def _connect():
    """Одно долгоживущее соединение на весь процесс.

    Соединение на каждый вызов давало две беды сразу. Без закрытия
    дескрипторы копились до сборки мусора и упирались в лимит открытых
    файлов. А с закрытием становилось ещё хуже: в режиме WAL SQLite
    выполняет контрольную точку при закрытии ПОСЛЕДНЕГО соединения, то
    есть на каждой операции — замер показал 12 операций в секунду против
    тысяч.

    Доступ и так полностью сериализован глобальной блокировкой (единственный
    писатель), поэтому одно переиспользуемое соединение и решает обе задачи.
    check_same_thread выключен осознанно: за очерёдность отвечает _lock.
    """
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_DB_PATH, timeout=10, check_same_thread=False)
        _conn.execute("PRAGMA busy_timeout=5000")
        # С WAL это безопасно и снимает fsync на каждой фиксации.
        _conn.execute("PRAGMA synchronous=NORMAL")
    try:
        with _conn:
            yield _conn
    except sqlite3.Error:
        # Соединение могло стать непригодным — пересоздадим на следующем вызове
        try:
            _conn.close()
        except sqlite3.Error:
            pass
        _conn = None
        raise


def init() -> bool:
    """Подготовить БД. Возвращает False, если хранилище недоступно.

    Ошибку НЕ поднимаем: раньше она летела наружу из импорта app и
    воркер gunicorn вообще не поднимался — то есть недоступная или битая
    база роняла весь сервис, хотя все остальные пути умеют деградировать.
    """
    try:
        return _init_unsafe()
    except Exception:
        logging.exception("хранилище статистики недоступно")
        return False


def _init_unsafe() -> bool:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock, _connect() as conn:
        # WAL — персистентное свойство файла, задавать его на каждом
        # соединении не нужно.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
    return True


def _meta_get(key: str) -> str | None:
    try:
        with _lock, _connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _meta_set(key: str, value: str) -> None:
    try:
        with _lock, _connect() as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (key, value))
    except sqlite3.Error:
        pass


def prune_feed_and_daily() -> tuple[int, int]:
    """Срок хранения для ленты и суточных счётчиков.

    Обе таблицы росли бессрочно. Лента вдобавок целиком читалась
    миграцией при каждом старте, поэтому её размер напрямую бил по
    времени запуска и по памяти.
    """
    removed_feed = removed_daily = 0
    try:
        if config.FEED_RETENTION_DAYS > 0:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=config.FEED_RETENTION_DAYS)).isoformat()
            with _lock, _connect() as conn:
                cur = conn.execute("DELETE FROM feed WHERE last_seen < ?", (cutoff,))
                removed_feed = cur.rowcount or 0
        if config.DAILY_RETENTION_DAYS > 0:
            day = (date.today() - timedelta(days=config.DAILY_RETENTION_DAYS)).isoformat()
            with _lock, _connect() as conn:
                cur = conn.execute("DELETE FROM daily WHERE day < ?", (day,))
                removed_daily = cur.rowcount or 0
    except sqlite3.Error:
        logging.exception("не удалось применить срок хранения")
    return removed_feed, removed_daily


def round_existing_timestamps() -> int:
    """Одноразово огрубить уже накопленные метки до минуты.

    Округление новых записей не помогает тем, что уже лежат в базе, — а
    именно они и раскрывали точный момент: при небольшом трафике запись с
    count=1 публикует секунду единственного скачивания конкретным
    человеком. Формат `...T00:00:00+00:00` остаётся лексикографически
    сортируемым, поэтому порядок в ленте не страдает.
    """
    try:
        with _lock, _connect() as conn:
            n = 0
            for table, cols in (("events", ("at",)),
                                ("feed", ("first_seen", "last_seen"))):
                for col in cols:
                    cur = conn.execute(
                        f"UPDATE {table} SET {col} = substr({col}, 1, 16) || ':00+00:00' "
                        f"WHERE length({col}) > 22")
                    n += cur.rowcount or 0
            return n
    except Exception:
        logging.exception("не удалось огрубить метки времени")
        return 0


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
            # До минуты: см. пояснение в заголовке модуля.
            conn.execute("INSERT INTO events(url, at) VALUES (?, ?)",
                         (url, now[:16] + ":00+00:00"))
    except sqlite3.Error:
        pass


def events_for(url: str, limit: int = 100) -> list[str]:
    """Времена скачиваний одного ролика, от свежих к старым.

    Возвращается не больше `limit` записей, поэтому вызывающая сторона не
    должна выдавать недобор за «остальные не записаны».
    """
    if not config.FEED_ENABLED or not url:
        return []
    limit = max(1, min(int(limit), 500))
    try:
        with _lock, _connect() as conn:
            rows = conn.execute(
                "SELECT at FROM events WHERE url = ? ORDER BY at DESC LIMIT ?",
                (url, limit)).fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error:
        return []


def prune_events() -> int:
    """Убрать события старше срока хранения.

    Таблица растёт на строку с каждой загрузкой, поэтому ей нужен предел —
    иначе она становится вечным журналом активности.
    """
    if config.EVENT_RETENTION_DAYS <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=config.EVENT_RETENTION_DAYS)).isoformat()
    try:
        with _lock, _connect() as conn:
            cur = conn.execute("DELETE FROM events WHERE at < ?", (cutoff,))
            return cur.rowcount or 0
    except sqlite3.Error:
        return 0


def selftest() -> None:
    """Проверить, что БД действительно пишется. Ошибки НЕ глушим:
    это единственное место, где сбой хранилища должен быть заметен."""
    with _lock, _connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS _probe (k INTEGER PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO _probe(k) VALUES (1)")
        conn.execute("DELETE FROM _probe")


# Версия разовых преобразований. Пока она совпадает с записанной в БД,
# миграции не запускаются: раньше каждая из них перечитывала всю ленту
# при каждом старте сервиса, и время запуска росло вместе с таблицей.
MIGRATION_VERSION = "2"


def run_migrations(canonicalize) -> dict:
    """Выполнить разовые преобразования, если они ещё не применялись."""
    if _meta_get("migration_version") == MIGRATION_VERSION:
        return {"skipped": True}
    result = {
        "rounded": round_existing_timestamps(),
        "merged": migrate_feed(canonicalize),
        "skipped": False,
    }
    _meta_set("migration_version", MIGRATION_VERSION)
    return result


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
            # Любое исключение из canonicalize (а не только sqlite3.Error)
            # раньше летело наружу из импорта app и не давало сервису
            # стартовать: canonical_url ловит лишь ValueError, а на
            # одиночном суррогате бросает UnicodeEncodeError.
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
            # События хранятся по тому же адресу: без переноса вся
            # собранная история оставалась осиротевшей и невидимой.
            for old_url in {r[0] for r in rows}:
                new_url = canonicalize(old_url)
                if new_url != old_url:
                    conn.execute("UPDATE events SET url = ? WHERE url = ?",
                                 (new_url, old_url))
            return len(rows) - len(merged)
    except Exception:
        logging.exception("миграция ленты не удалась")
        return 0


def encode_cursor(row: dict, order: str) -> str:
    """Непрозрачная метка позиции для следующей страницы."""
    key = ([str(row["count"]), row["last_seen"], row["url"]] if order == "popular"
           else [row["last_seen"], row["url"]])
    raw = "\x1f".join(key).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str, order: str):
    try:
        pad = "=" * (-len(cursor) % 4)
        parts = base64.urlsafe_b64decode(cursor + pad).decode().split("\x1f")
    except Exception:
        return None
    want = 3 if order == "popular" else 2
    return parts if len(parts) == want else None


def feed(order: str = "recent", limit: int = 50,
         cursor: str | None = None) -> list[dict]:
    """Лента: последние или самые популярные.

    Листание идёт по ключу сортировки, а не по OFFSET. При OFFSET записи
    сдвигались между запросами страниц, потому что last_seen меняется при
    каждом новом скачивании: одни записи попадали на две страницы, другие
    не попадали ни на одну.
    """
    if not config.FEED_ENABLED:
        return []
    limit = max(1, min(int(limit), 200))
    popular = order == "popular"
    col = "count DESC, last_seen DESC, url DESC" if popular else "last_seen DESC, url DESC"

    where, args = "", []
    key = _decode_cursor(cursor, order) if cursor else None
    if key:
        if popular:
            # строгое «меньше» по составному ключу (count, last_seen, url)
            where = ("WHERE (count < ?) OR (count = ? AND last_seen < ?) "
                     "OR (count = ? AND last_seen = ? AND url < ?) ")
            c, t, u = key
            args = [c, c, t, c, t, u]
        else:
            where = "WHERE (last_seen < ?) OR (last_seen = ? AND url < ?) "
            t, u = key
            args = [t, t, u]
    try:
        with _lock, _connect() as conn:
            rows = conn.execute(
                f"SELECT url, first_seen, last_seen, count FROM feed "
                f"{where}ORDER BY {col} LIMIT ?", (*args, limit)).fetchall()
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
