"""Исполнитель загрузок в отдельном процессе: итог, отмена, сторож зависаний.

Сеть наружу не нужна: источник — локальный сокет, который либо закрыт
(ошибка сразу), либо принимает соединение и молчит (зависший сервер).
"""
import socket
import threading
import time

import pytest

import config
import downloader as dl


@pytest.fixture
def manager():
    return dl.DownloadManager()


@pytest.fixture
def silent_server():
    """Принимает соединения и ничего не отвечает — как зависший CDN."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    held = []
    stop = threading.Event()

    def loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                held.append(c)
            except OSError:
                continue
    threading.Thread(target=loop, daemon=True).start()
    yield f"http://127.0.0.1:{srv.getsockname()[1]}/video.mp4"
    stop.set()
    for c in held:
        c.close()
    srv.close()


def _closed_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/video.mp4"


def _task(url, tid):
    return dl.Task(id=tid, url=url, title="Ролик", fmt="best", extra={}, label="l")


def _run_async(m, t):
    th = threading.Thread(target=m._run, args=(t,), daemon=True)
    th.start()
    return th


def test_error_is_reported_not_crashed(manager):
    t = _task(_closed_port_url(), "1" * 32)
    manager._run(t)
    assert t.status == "error"
    assert t.error and "Внутренняя" not in t.error
    assert t.completed


def test_cancel_stops_hung_job(manager, silent_server, monkeypatch):
    """Зависший источник не вызывает хук прогресса — мягкая отмена не
    срабатывает, и процесс обязан быть остановлен принудительно."""
    monkeypatch.setattr(config, "CANCEL_GRACE_SEC", 1)
    t = _task(silent_server, "2" * 32)
    th = _run_async(manager, t)
    deadline = time.time() + 20
    while t.status != "preparing" and time.time() < deadline:
        time.sleep(0.05)
    assert t.status == "preparing", "исполнитель не сообщил о подготовке"
    t.cancel.set()
    th.join(timeout=15)
    assert not th.is_alive(), "задача не остановилась после отмены"
    assert t.status == "cancelled"


def test_stall_watchdog_fails_task(manager, silent_server, monkeypatch):
    monkeypatch.setattr(config, "STALL_SEC", 2)
    monkeypatch.setattr(config, "CANCEL_GRACE_SEC", 1)
    t = _task(silent_server, "3" * 32)
    th = _run_async(manager, t)
    th.join(timeout=20)
    assert not th.is_alive(), "сторож не снял зависшую задачу"
    assert t.status == "error"
    assert "перестал отдавать" in t.error
    assert not dl.task_files(t.id), "после сбоя остался мусор"


def test_finished_filename_must_belong_to_task(manager, monkeypatch, tmp_path):
    """Имя файла от исполнителя проверяется: чужой файл не выдаём."""
    other = config.DOWNLOAD_DIR / ("f" * 32 + ".mp4")
    other.write_bytes(b"x")
    monkeypatch.setattr(manager, "_execute",
                        lambda task: {"status": "finished", "filename": other.name})
    t = _task("https://example.com/v", "4" * 32)
    manager._run(t)
    assert t.status == "error"
    other.unlink()
