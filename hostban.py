"""Общий для всего сервиса список заблокированных видеосерверов.

Провайдер режет соединения к отдельным IP видеосерверов YouTube (подробно —
в racefd.py и AGENTS.md). Загрузка, которая упёрлась в такой IP, заносит его
сюда, и все остальные загрузки — любых роликов — обходят его сразу, а не
тратят десятки секунд на те же повисшие рукопожатия.

Запись временная (BAN_TTL_SEC): блокировки со временем меняются, и сервер,
заблокированный час назад, сейчас может работать. Удачное соединение снимает
запись сразу.

Хранилище — SQLite в DATA_DIR: загрузки идут в отдельных процессах, и им
нужен общий, безопасный для одновременной записи файл. Хранятся только IP
серверов Google и время — никаких данных пользователей.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import config

_DB = config.DATA_DIR / "hostban.sqlite3"
_local = threading.local()
_cache: dict = {"at": 0.0, "ips": frozenset()}
_cache_lock = threading.Lock()
CACHE_SEC = 2.0          # как часто перечитывать список


def _conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(_DB, timeout=5, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("CREATE TABLE IF NOT EXISTS banned ("
                  " ip TEXT PRIMARY KEY, until REAL NOT NULL, since REAL NOT NULL)")
        # Средняя скорость одного соединения с сервером (МБ/с): по ней загрузки
        # выбирают, с какого сервера качать. Устаревает за RATE_TTL_SEC.
        c.execute("CREATE TABLE IF NOT EXISTS rate ("
                  " ip TEXT PRIMARY KEY, mbps REAL NOT NULL, at REAL NOT NULL)")
        _local.conn = c
    return c


def ban(ip: str, ttl: float | None = None) -> None:
    """Занести IP в список (или продлить запись)."""
    if not ip:
        return
    now = time.time()
    ttl = config.BAN_TTL_SEC if ttl is None else ttl
    try:
        _conn().execute(
            "INSERT INTO banned(ip, until, since) VALUES (?, ?, ?) "
            "ON CONFLICT(ip) DO UPDATE SET until = excluded.until",
            (ip, now + ttl, now))
    except sqlite3.Error:
        return
    _invalidate()


def clear(ip: str) -> None:
    """Сервер снова отвечает — убрать из списка."""
    if not ip:
        return
    try:
        _conn().execute("DELETE FROM banned WHERE ip = ?", (ip,))
    except sqlite3.Error:
        return
    _invalidate()


def banned() -> frozenset:
    """Текущий список (с коротким кэшем: вызывается на каждое соединение)."""
    now = time.time()
    with _cache_lock:
        if now - _cache["at"] < CACHE_SEC:
            return _cache["ips"]
    try:
        rows = _conn().execute("SELECT ip FROM banned WHERE until > ?", (now,)).fetchall()
        _conn().execute("DELETE FROM banned WHERE until <= ?", (now,))
        ips = frozenset(r[0] for r in rows)
    except sqlite3.Error:
        ips = frozenset()
    with _cache_lock:
        _cache.update(at=now, ips=ips)
    return ips


def is_banned(ip: str | None) -> bool:
    return bool(ip) and ip in banned()


RATE_TTL_SEC = 900
_rates: dict = {"at": 0.0, "map": {}}


def rate_put(ip: str, mbps: float) -> None:
    """Записать скорость соединения с сервером (сглаженно с прошлой)."""
    if not ip or mbps <= 0:
        return
    now = time.time()
    try:
        _conn().execute(
            "INSERT INTO rate(ip, mbps, at) VALUES (?, ?, ?) ON CONFLICT(ip) DO UPDATE SET "
            "mbps = CASE WHEN ? - at > ? THEN excluded.mbps "
            "            ELSE mbps * 0.6 + excluded.mbps * 0.4 END, at = excluded.at",
            (ip, mbps, now, now, RATE_TTL_SEC))
    except sqlite3.Error:
        pass


def rate_get(ip: str | None) -> float | None:
    """Свежая средняя скорость сервера, МБ/с, или None, если не знаем."""
    if not ip:
        return None
    now = time.time()
    with _cache_lock:
        if now - _rates["at"] >= CACHE_SEC:
            try:
                rows = _conn().execute("SELECT ip, mbps FROM rate WHERE at > ?",
                                       (now - RATE_TTL_SEC,)).fetchall()
                _rates.update(at=now, map=dict(rows))
            except sqlite3.Error:
                _rates.update(at=now, map={})
        return _rates["map"].get(ip)


def listing() -> list[tuple[str, float, float]]:
    """(ip, с какого момента, до какого) — для отчёта."""
    try:
        return _conn().execute(
            "SELECT ip, since, until FROM banned WHERE until > ? ORDER BY since",
            (time.time(),)).fetchall()
    except sqlite3.Error:
        return []


def _invalidate() -> None:
    with _cache_lock:
        _cache["at"] = 0.0
