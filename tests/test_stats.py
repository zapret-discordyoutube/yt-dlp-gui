"""Счётчики и публичная лента."""
import sqlite3
import threading

import pytest

import stats


@pytest.fixture(autouse=True)
def clean_db():
    stats.init()
    with stats._lock, stats._connect() as conn:
        conn.execute("DELETE FROM daily")
        conn.execute("DELETE FROM feed")
    yield


# --- счётчики ---------------------------------------------------------------

def test_bump_accumulates():
    stats.bump("api_info")
    stats.bump("api_info", 4)
    assert stats.summary()["all"]["api_info"] == 5


def test_bump_ignores_unknown_and_nonpositive():
    stats.bump("нет_такого", 10)
    stats.bump("api_info", 0)
    stats.bump("api_info", -5)
    assert stats.summary()["all"]["api_info"] == 0


def test_series_has_thirty_days_in_order():
    s = stats.summary()["series"]
    assert len(s) == 30
    days = [d["day"] for d in s]
    assert days == sorted(days)
    assert all(set(d) >= set(stats.COUNTERS) for d in s)


def test_bytes_served_is_summed():
    stats.bump("bytes_served", 1000)
    stats.bump("bytes_served", 2345)
    assert stats.summary()["all"]["bytes_served"] == 3345


def test_concurrent_bumps_are_not_lost():
    """Один воркер, но 64 потока — потери инкрементов были бы незаметны."""
    def worker():
        for _ in range(50):
            stats.bump("downloads_done")

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert stats.summary()["all"]["downloads_done"] == 16 * 50


# --- лента ------------------------------------------------------------------

def test_record_download_is_upsert():
    url = "https://www.youtube.com/watch?v=abc"
    stats.record_download(url)
    stats.record_download(url)
    stats.record_download(url)
    items = stats.feed()
    assert len(items) == 1
    assert items[0]["count"] == 3
    assert items[0]["first_seen"] <= items[0]["last_seen"]


def test_feed_orders():
    stats.record_download("https://a.example/1")
    for _ in range(5):
        stats.record_download("https://b.example/2")

    recent = stats.feed(order="recent")
    popular = stats.feed(order="popular")
    assert popular[0]["url"] == "https://b.example/2"
    assert recent[0]["url"] == "https://b.example/2"   # он же и последний
    assert {i["url"] for i in recent} == {"https://a.example/1",
                                          "https://b.example/2"}


def test_feed_totals():
    stats.record_download("https://a.example/1")
    stats.record_download("https://a.example/1")
    stats.record_download("https://b.example/2")
    assert stats.feed_totals() == {"unique": 2, "total": 3}


def test_feed_limit_is_clamped():
    for i in range(5):
        stats.record_download(f"https://x.example/{i}")
    assert len(stats.feed(limit=2)) == 2
    assert len(stats.feed(limit=10_000)) == 5      # не падает на огромном лимите
    assert len(stats.feed(limit=0)) == 1           # приводится к минимуму


def test_feed_pagination():
    for i in range(5):
        stats.record_download(f"https://x.example/{i}")
    page1 = stats.feed(limit=2, offset=0)
    page2 = stats.feed(limit=2, offset=2)
    assert {i["url"] for i in page1} & {i["url"] for i in page2} == set()


def test_record_download_ignores_empty():
    stats.record_download("")
    assert stats.feed() == []


def test_feed_disabled(monkeypatch):
    import config
    monkeypatch.setattr(config, "FEED_ENABLED", False)
    stats.record_download("https://a.example/1")
    assert stats.feed() == []


# --- миграция ---------------------------------------------------------------

def test_migrate_feed_merges_duplicates():
    """Одна и та же запись под разными адресами должна схлопнуться,
    а счётчики — сложиться."""
    with stats._lock, stats._connect() as conn:
        conn.executemany(
            "INSERT INTO feed(url, first_seen, last_seen, count) VALUES (?,?,?,?)",
            [("https://youtu.be/aqz-KE-bpKQ", "2026-01-01T00:00:00+00:00",
              "2026-01-02T00:00:00+00:00", 2),
             ("https://www.youtube.com/watch?v=aqz-KE-bpKQ",
              "2026-01-03T00:00:00+00:00", "2026-01-04T00:00:00+00:00", 3)])

    import downloader as dl
    merged = stats.migrate_feed(dl.canonical_url)

    items = stats.feed()
    assert merged == 1
    assert len(items) == 1
    assert items[0]["count"] == 5
    assert items[0]["first_seen"] == "2026-01-01T00:00:00+00:00"
    assert items[0]["last_seen"] == "2026-01-04T00:00:00+00:00"


def test_migrate_feed_is_idempotent():
    stats.record_download("https://www.youtube.com/watch?v=ID2")
    import downloader as dl
    assert stats.migrate_feed(dl.canonical_url) == 0


def test_migrate_feed_leaves_short_ids_alone():
    """Слишком короткий идентификатор — не ролик, трогать такую ссылку
    нельзя: канонизация должна оставить её как есть."""
    import downloader as dl
    assert dl.canonical_url("https://youtu.be/ab") == "https://youtu.be/ab"


# --- устойчивость -----------------------------------------------------------

def test_selftest_raises_when_db_broken(monkeypatch):
    """selftest — единственное место, где сбой БД обязан быть заметен."""
    def boom(*a, **k):
        raise sqlite3.OperationalError("диск только на чтение")
    monkeypatch.setattr(stats, "_connect", boom)
    with pytest.raises(sqlite3.Error):
        stats.selftest()


def test_bump_never_raises(monkeypatch):
    """А вот запись счётчика ронять запрос не должна."""
    def boom(*a, **k):
        raise sqlite3.OperationalError("нет места")
    monkeypatch.setattr(stats, "_connect", boom)
    stats.bump("api_info")
    stats.record_download("https://a.example/1")
