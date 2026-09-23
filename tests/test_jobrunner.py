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
    with manager.lock:
        manager.tasks[t.id] = t
    t0 = time.time()
    assert manager.cancel(t.id)
    # Отмена видна сразу, ещё до остановки процесса.
    assert t.status == "cancelled"
    th.join(timeout=15)
    assert not th.is_alive(), "задача не остановилась после отмены"
    assert time.time() - t0 < 3, "отмена зависшей загрузки заняла слишком долго"
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


def test_metrics_are_filled(manager):
    t = _task(_closed_port_url(), "5" * 32)
    manager._run(t)
    m = t.metrics
    assert m["engine"] == "ytdlp"
    assert m["total_ms"] is not None and m["queue_ms"] is not None
    assert not any(k.startswith("_") for k in m), "служебные отметки утекли"


def test_youtube_prefers_https_formats():
    """HLS-форматы YouTube висли на заблокированном сервере: при равном
    разрешении берём https (его качает racefd со сменой сервера)."""
    import io
    import jobrunner
    yt = jobrunner.Job({"id": "6" * 32, "url": "https://www.youtube.com/watch?v=x",
                        "fmt": "bv*+ba/b"}, io.StringIO())
    other = jobrunner.Job({"id": "7" * 32, "url": "https://vimeo.com/1",
                           "fmt": "bv*+ba/b"}, io.StringIO())
    fs = yt.ydl_opts()["format_sort"]
    assert fs.index("res") < fs.index("proto:https")
    assert "format_sort" not in other.ydl_opts()


def test_youtube_uses_own_source_ip(monkeypatch):
    """YouTube напрямую — с отдельного IP; прочие сайты и egress — нет."""
    import io
    import jobrunner
    monkeypatch.setattr(config, "YT_SOURCE_IP", "203.0.113.9")
    yt = jobrunner.Job({"id": "8" * 32, "url": "https://www.youtube.com/watch?v=x"}, io.StringIO())
    other = jobrunner.Job({"id": "9" * 32, "url": "https://vimeo.com/1"}, io.StringIO())
    via_egress = jobrunner.Job({"id": "a" * 32, "url": "https://www.youtube.com/watch?v=x",
                                "proxy": "socks5://127.0.0.1:18110"}, io.StringIO())
    assert yt.ydl_opts()["source_address"] == "203.0.113.9"
    assert "source_address" not in other.ydl_opts()
    assert "source_address" not in via_egress.ydl_opts()


def test_racefd_direct_session_binds_source_ip(monkeypatch):
    import racefd
    monkeypatch.setattr(config, "YT_SOURCE_IP", "203.0.113.9")
    s = racefd._direct_session()
    ad = s.get_adapter("https://rr1---sn-x.googlevideo.com/videoplayback")
    assert isinstance(ad, racefd._SourceAdapter) and ad._source_ip == "203.0.113.9"
    monkeypatch.setattr(config, "YT_SOURCE_IP", "")
    assert not isinstance(racefd._direct_session().get_adapter("https://x/"), racefd._SourceAdapter)
