"""Гарантии приватности, заявленные пользователю на самих страницах.

Каждая из них закрывала реальную утечку, но ни одна не проверялась: любая
из этих поломок прошла бы мимо набора тестов незамеченной.
"""
import logging

import pytest

import config
import downloader as dl
import stats


# --- ссылки не попадают в журнал --------------------------------------------

def test_ytdlp_logger_stays_silent(caplog):
    """yt-dlp печатает ошибки в stderr вместе с адресом, и systemd их
    записывает. Вырезать ссылку из текста мало: yt-dlp подставляет
    идентификатор, выкроенный из пути, а в нём может быть токен."""
    logger = dl._QuietLogger()
    with caplog.at_level(logging.DEBUG):
        logger.debug("https://vk.ru/audio1_2_SECRETTOKEN")
        logger.info("https://vk.ru/audio1_2_SECRETTOKEN")
        logger.warning("https://vk.ru/audio1_2_SECRETTOKEN")
        logger.error("ERROR: [generic] audio1_2_SECRETTOKEN: не удалось")
    assert "SECRETTOKEN" not in caplog.text
    assert caplog.text.strip() == ""


def test_host_logged_without_path():
    """Для диагностики пишется домен, но не путь и не параметры."""
    out = dl._host_of("https://example.com/private/video?token=SECRET")
    assert out == "example.com"
    assert "SECRET" not in out and "private" not in out


@pytest.mark.parametrize("msg,forbidden", [
    ("unable to open /home/codex-pve/ytdlp-gui/downloads/a.mp4", "codex-pve"),
    ("ffmpeg failed: /tmp/xyz/file.mkv", "/tmp/xyz"),
    ("Failed to connect to 127.0.0.1 port 85", "127.0.0.1"),
    ("Failed to connect to 169.254.169.254", "169.254.169.254"),
])
def test_error_text_hides_internals(msg, forbidden):
    """Текст ошибки уходит пользователю, поэтому локальные пути и
    внутренние адреса из него вымарываются."""
    assert forbidden not in dl._clean_err(msg)


def test_error_text_keeps_user_link():
    """А вот ссылку пользователя портить нельзя: слишком жадная регулярка
    когда-то превращала её в «https:/<путь>» и сообщение теряло смысл."""
    out = dl._clean_err("Unsupported URL: https://vk.ru/audio134073007_456240755")
    assert "vk.ru/audio134073007_456240755" in out or "не поддерживается" in out


# --- в публичную ленту идёт приведённая ссылка ------------------------------

def test_feed_receives_canonical_url(monkeypatch):
    """Сырая ссылка с токенами не должна попадать в публичную ленту."""
    import app as app_module
    seen = []
    monkeypatch.setattr(app_module.stats, "record_download", seen.append)
    monkeypatch.setattr(app_module.stats, "bump", lambda *a, **k: None)
    monkeypatch.setattr(app_module.stats, "record_perf", lambda *a, **k: None)

    task = dl.Task(id="p" * 32, fmt="best", extra={}, label="l", title="t",
                   url="https://youtu.be/aqz-KE-bpKQ?si=TRACKING&token=SECRET")
    task.status = "finished"
    app_module._task_finished(task)

    assert seen == ["https://www.youtube.com/watch?v=aqz-KE-bpKQ"]
    assert "SECRET" not in seen[0] and "TRACKING" not in seen[0]


# --- время огрублено до минуты ----------------------------------------------

@pytest.fixture
def clean_feed():
    stats.init()
    with stats._lock, stats._connect() as conn:
        conn.execute("DELETE FROM feed")
        conn.execute("DELETE FROM events")
    yield


def test_event_time_is_rounded_to_minute(clean_feed):
    """При небольшом трафике точная метка однозначно выделяет сеанс: тот,
    кто знает, когда человек заходил, узнал бы, что он скачал."""
    stats.record_download("https://example.com/v1")
    times = stats.events_for("https://example.com/v1")
    assert times and all(t.endswith(":00+00:00") for t in times), times


def test_existing_timestamps_are_coarsened(clean_feed):
    """Округление новых записей не помогает тем, что уже лежат в базе."""
    with stats._lock, stats._connect() as conn:
        conn.execute("INSERT INTO feed(url, first_seen, last_seen, count) "
                     "VALUES (?, ?, ?, 1)",
                     ("https://e.com/x", "2026-09-21T14:10:47.123456+00:00",
                      "2026-09-21T15:46:31.987654+00:00"))
        conn.execute("INSERT INTO events(url, at) VALUES (?, ?)",
                     ("https://e.com/x", "2026-09-21T15:46:31.987654+00:00"))

    assert stats.round_existing_timestamps() >= 2
    item = stats.feed()[0]
    assert item["first_seen"] == "2026-09-21T14:10:00+00:00"
    assert item["last_seen"] == "2026-09-21T15:46:00+00:00"
    assert stats.events_for("https://e.com/x") == ["2026-09-21T15:46:00+00:00"]


def test_feed_disabled_also_disables_event_history(clean_feed, monkeypatch):
    """Рубильник ленты обязан выключать и журнал времён."""
    monkeypatch.setattr(config, "FEED_ENABLED", False)
    stats.record_download("https://e.com/off")
    assert stats.events_for("https://e.com/off") == []


# --- внутренние метрики не отдаются наружу ----------------------------------

def test_readyz_hides_metrics_from_public(client):
    """Сайт публичный: свободное место на разделе гипервизора, глубина
    очереди и версия yt-dlp — не для всех."""
    r = client.get("/readyz", environ_overrides={"REMOTE_ADDR": "203.0.113.7"})
    body = r.get_json()
    assert "checks" in body and "ok" in body
    assert "metrics" not in body and "version" not in body


def test_readyz_shows_metrics_to_localhost(client):
    r = client.get("/readyz", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
    body = r.get_json()
    assert "metrics" in body and "free_disk_mb" in body["metrics"]


# --- сроки хранения ---------------------------------------------------------

def test_events_are_pruned(clean_feed, monkeypatch):
    """Журнал времён без срока хранения превращается в вечный журнал
    активности, а в интерфейсе обещано 90 дней."""
    with stats._lock, stats._connect() as conn:
        conn.execute("INSERT INTO events(url, at) VALUES (?, ?)",
                     ("https://e.com/old", "2020-01-01T00:00:00+00:00"))
        conn.execute("INSERT INTO events(url, at) VALUES (?, ?)",
                     ("https://e.com/new", "2099-01-01T00:00:00+00:00"))
    assert stats.prune_events() == 1
    assert stats.events_for("https://e.com/old") == []
    assert stats.events_for("https://e.com/new") != []


def test_feed_and_daily_are_pruned(clean_feed):
    with stats._lock, stats._connect() as conn:
        conn.execute("DELETE FROM daily")
        conn.execute("INSERT INTO feed(url, first_seen, last_seen, count) "
                     "VALUES ('https://e.com/ancient', '2019-01-01T00:00:00+00:00',"
                     " '2019-01-01T00:00:00+00:00', 1)")
        conn.execute("INSERT INTO daily(day) VALUES ('2019-01-01')")
    gone_feed, gone_daily = stats.prune_feed_and_daily()
    assert gone_feed == 1 and gone_daily == 1


def test_retention_can_be_switched_off(clean_feed, monkeypatch):
    monkeypatch.setattr(config, "FEED_RETENTION_DAYS", 0)
    monkeypatch.setattr(config, "DAILY_RETENTION_DAYS", 0)
    with stats._lock, stats._connect() as conn:
        conn.execute("INSERT INTO feed(url, first_seen, last_seen, count) "
                     "VALUES ('https://e.com/keep', '2019-01-01T00:00:00+00:00',"
                     " '2019-01-01T00:00:00+00:00', 1)")
    assert stats.prune_feed_and_daily() == (0, 0)


# --- разовые миграции не повторяются ----------------------------------------

def test_migrations_run_once(clean_feed):
    """Раньше каждая миграция перечитывала всю ленту при КАЖДОМ старте, и
    время запуска росло вместе с таблицей."""
    stats._meta_set("migration_version", "")
    first = stats.run_migrations(dl.canonical_url)
    second = stats.run_migrations(dl.canonical_url)
    assert first.get("skipped") is False
    assert second.get("skipped") is True


def test_migration_survives_broken_canonicalizer(clean_feed):
    """Исключение из канонизации летело наружу из импорта app, и сервис
    просто не поднимался."""
    stats._meta_set("migration_version", "")
    with stats._lock, stats._connect() as conn:
        conn.execute("INSERT INTO feed(url, first_seen, last_seen, count) "
                     "VALUES ('https://e.com/a', '2026-01-01T00:00:00+00:00',"
                     " '2026-01-01T00:00:00+00:00', 1)")

    def boom(_url):
        raise RuntimeError("внезапно")

    assert stats.migrate_feed(boom) == 0      # не бросает
    assert len(stats.feed()) == 1             # данные на месте
