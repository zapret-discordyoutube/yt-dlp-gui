"""Регрессии на гонки и живучесть, найденные аудитом.

Каждый тест здесь закрывает конкретный воспроизведённый баг, а не
гипотетический сценарий.
"""
import threading
import time

import pytest

import config
import downloader as dl


@pytest.fixture
def manager():
    m = dl.DownloadManager()
    yield m


def _task(m, status="finished", tid=None):
    t = dl.Task(id=tid or ("a" * 32), url="https://example.com/v", title="Ролик",
                fmt="best", extra={}, label="тест")
    t.status = status
    with m.lock:
        m.tasks[t.id] = t
    return t


# --- выдача файла ----------------------------------------------------------

def test_mark_served_is_atomic(manager):
    """Параллельные Range-запросы кратно завышали публичную статистику:
    проверка served_at и присваивание шли по отдельности."""
    t = _task(manager)
    results = []

    def grab():
        results.append(manager.mark_served(t))

    threads = [threading.Thread(target=grab) for _ in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert results.count(True) == 1, "первой выдачей может быть только одна"
    assert results.count(False) == 15


# --- уборщик ---------------------------------------------------------------

def test_janitor_keeps_files_of_live_tasks(manager, monkeypatch):
    """Загрузка длиннее FILE_TTL теряла из-под yt-dlp уже скачанную
    дорожку: страховочное удаление по mtime не смотрело на живые задачи."""
    monkeypatch.setattr(config, "FILE_TTL_MINUTES", 0)
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    live = _task(manager, status="downloading", tid="b" * 32)
    part = config.DOWNLOAD_DIR / f"{live.id}.f137.mp4.part"
    part.write_bytes(b"data")
    old = config.DOWNLOAD_DIR / "cccccccc.mp4"
    old.write_bytes(b"junk")
    # состарим оба файла
    past = time.time() - 3600
    import os
    os.utime(part, (past, past))
    os.utime(old, (past, past))

    try:
        manager._janitor_pass()
        assert part.exists(), "файл живой задачи удалён"
        assert not old.exists(), "чужой протухший файл должен был уйти"
    finally:
        part.unlink(missing_ok=True)
        old.unlink(missing_ok=True)


def test_janitor_evicts_terminal_tasks(manager, monkeypatch):
    monkeypatch.setattr(config, "TASK_TTL_MINUTES", 0)
    t = _task(manager, status="finished", tid="d" * 32)
    t.created_at = time.time() - 10
    manager._janitor_pass()
    assert manager.get(t.id) is None


def test_janitor_removes_stuck_task(manager, monkeypatch):
    """Задача в нетерминальном статусе не выселялась НИКОГДА: карточки
    копились до TASKS_MAX, и сервис отвечал вечным «перегружен»."""
    monkeypatch.setattr(config, "TASK_TTL_MINUTES", 1)
    t = _task(manager, status="downloading", tid="e" * 32)
    t.created_at = time.time() - 10_000      # заведомо дольше любого порога
    manager._janitor_pass()
    assert manager.get(t.id).status == "error"


def test_janitor_keeps_gitkeep(manager, monkeypatch):
    monkeypatch.setattr(config, "FILE_TTL_MINUTES", 0)
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    keep = config.DOWNLOAD_DIR / ".gitkeep"
    keep.write_bytes(b"")
    import os
    past = time.time() - 3600
    os.utime(keep, (past, past))
    manager._janitor_pass()
    assert keep.exists(), "маркер каталога удалён уборщиком"


# --- очередь и пул ---------------------------------------------------------

def test_queue_overflow_is_rejected(manager, monkeypatch):
    """Переполнение должно давать честный отказ сразу, а не молчаливое
    ожидание в очереди.

    Очередь набиваем не вручную: постоянные воркеры её тут же разбирают
    (что само по себе подтверждает их живучесть). Поэтому имитируем
    переполнение на самой постановке.
    """
    import queue as _q

    def full(_item):
        raise _q.Full

    monkeypatch.setattr(manager.queue, "put_nowait", full)
    with pytest.raises(dl.Overloaded):
        manager.create(url="https://example.com/v", fmt="best", extra={},
                       label="l", title="t", thumbnail=None)
    # карточка не должна остаться висеть после отказа
    assert manager.health()["tasks_total"] == 0


def test_tasks_cap_is_enforced(manager, monkeypatch):
    monkeypatch.setattr(config, "TASKS_MAX", 1)
    _task(manager, tid="f" * 32)
    with pytest.raises(dl.Overloaded):
        manager.create(url="https://example.com/v", fmt="best", extra={},
                       label="l", title="t", thumbnail=None)


def test_worker_survives_failing_task(manager):
    """Воркер умирал молча, и пул усыхал до нуля."""
    m = dl.DownloadManager()
    done = threading.Event()
    seen = []
    m.on_complete = lambda t: (seen.append(t.status), done.set())

    def boom(task):
        raise BaseException("внезапно")      # noqa: TRY002

    m._run = boom
    t = dl.Task(id="g" * 32, url="https://example.com/v", title="t",
                fmt="best", extra={}, label="l")
    with m.lock:
        m.tasks[t.id] = t
    m.queue.put_nowait(t)

    assert done.wait(timeout=5), "on_complete не вызван — воркер умер"
    assert seen == ["error"]
    # пул всё ещё разбирает очередь
    done2 = threading.Event()
    m.on_complete = lambda t: done2.set()
    t2 = dl.Task(id="h" * 32, url="https://example.com/v", title="t",
                 fmt="best", extra={}, label="l")
    m.queue.put_nowait(t2)
    assert done2.wait(timeout=5), "после сбоя пул перестал работать"


# --- здоровье --------------------------------------------------------------

def test_health_reports_numbers(manager):
    h = manager.health()
    for key in ("free_disk_mb", "downloads_mb", "tasks_total",
                "tasks_active", "queue_pending", "janitor_age_sec"):
        assert key in h
