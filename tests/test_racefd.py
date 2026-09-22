"""Параллельный загрузчик googlevideo: сборка файла и выбор загрузчика."""
import hashlib
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yt_dlp

import racefd

DATA = random.Random(1).randbytes(5 * racefd.CHUNK + 12345)


class _RangeHandler(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *a):          # тишина в выводе тестов
        pass

    def do_GET(self):
        type(self).calls += 1
        # Каждый пятый запрос обрываем посреди ответа — как DPI.
        broken = type(self).calls % 5 == 0
        rng = self.headers.get("Range", "")
        start, end = (int(x) for x in rng.split("=")[1].split("-"))
        body = DATA[start:end + 1]
        self.send_response(206)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
        self.end_headers()
        self.wfile.write(body[: len(body) // 2] if broken else body)
        if broken:
            self.close_connection = True


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/videoplayback"
    srv.shutdown()


def test_assembles_file_exactly_despite_broken_responses(server, tmp_path, monkeypatch):
    monkeypatch.setattr(racefd, "READ_TIMEOUT", 2)
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    fd = racefd.RaceFD(ydl, {"quiet": True, "noprogress": True})
    seen = []
    fd.add_progress_hook(lambda d: seen.append(d["status"]))
    out = tmp_path / "v.mp4"
    ok = fd.real_download(str(out), {"url": server, "filesize": len(DATA),
                                     "http_headers": {}})
    assert ok
    assert hashlib.sha256(out.read_bytes()).digest() == hashlib.sha256(DATA).digest()
    assert seen[-1] == "finished"
    assert not os.path.exists(str(out) + ".part")


@pytest.mark.parametrize("info,params,expected", [
    ({"protocol": "https", "url": "https://rr1---sn-x.googlevideo.com/videoplayback?clen=99999999"}, {}, True),
    ({"protocol": "https", "url": "https://rr1---sn-x.googlevideo.com/videoplayback?clen=300000"}, {}, True),
    ({"protocol": "https", "url": "https://rr1---sn-x.googlevideo.com/videoplayback"}, {}, False),   # размер неизвестен
    ({"protocol": "m3u8_native", "url": "https://rr1---sn-x.googlevideo.com/x?clen=99999999"}, {}, False),
    ({"protocol": "https", "url": "https://example.com/v.mp4", "filesize": 99999999}, {}, False),
    ({"protocol": "https", "url": "https://rr1---sn-x.googlevideo.com/v?clen=99999999"},
     {"proxy": "socks5h://127.0.0.1:1"}, False),                                                     # через прокси — штатно
])
def test_suitable(info, params, expected):
    """Маленькие файлы тоже наши: штатный загрузчик вешал MP3 короткого ролика."""
    assert racefd.suitable(info, params) is expected


def test_single_chunk_file(server, tmp_path, monkeypatch):
    """Файл меньше куска: все соединения гоняются за одним куском."""
    small = DATA[:300_000]
    monkeypatch.setattr(racefd, "READ_TIMEOUT", 2)
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    fd = racefd.RaceFD(ydl, {"quiet": True, "noprogress": True})
    out = tmp_path / "a.m4a"
    assert fd.real_download(str(out), {"url": server, "filesize": len(small),
                                       "http_headers": {}})
    assert out.read_bytes() == small


def test_switches_to_mirror_when_server_is_blocked(server, tmp_path, monkeypatch):
    """Сервер «под DPI» (соединения молчат) — загрузчик сам находит тот же
    файл на другом сервере, как при ручном перезапуске загрузки."""
    import socket
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead.listen(64)                                   # принимает и молчит
    dead_url = f"http://127.0.0.1:{dead.getsockname()[1]}/videoplayback"
    monkeypatch.setattr(racefd, "READ_TIMEOUT", 1)
    monkeypatch.setattr(racefd, "CONNECT_TIMEOUT", 1)
    monkeypatch.setattr(racefd, "RESOLVE_EVERY", 0.5)
    asked = []
    monkeypatch.setattr(racefd, "_fresh_url",
                        lambda info, size: (asked.append(1), server)[1])
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    fd = racefd.RaceFD(ydl, {"quiet": True, "noprogress": True})
    out = tmp_path / "m.mp4"
    try:
        ok = fd.real_download(str(out), {"url": dead_url, "filesize": len(DATA),
                                         "http_headers": {}, "format_id": "401",
                                         "webpage_url": "https://www.youtube.com/watch?v=x"})
    finally:
        dead.close()
    assert ok and asked
    assert out.read_bytes() == DATA


# --- общий список заблокированных серверов (hostban) ------------------------

import socket as _socket
import threading as _threading
import time as _time

import hostban


def _silent_server():
    """Принимает соединения и молчит; считает, сколько их было."""
    srv = _socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    srv.settimeout(0.2)
    seen = {"n": 0}
    stop = _threading.Event()
    held = []

    def loop():
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                seen["n"] += 1
                held.append(c)
            except OSError:
                continue
    _threading.Thread(target=loop, daemon=True).start()

    def close():
        stop.set()
        for c in held:
            c.close()
        srv.close()
    return f"http://127.0.0.1:{srv.getsockname()[1]}/videoplayback", seen, close


@pytest.fixture
def fresh_racefd(monkeypatch):
    """Чистые знания о серверах; «IP» = host:port (в тестах всё на 127.0.0.1)."""
    monkeypatch.setattr(racefd, "_HOSTS", {})
    monkeypatch.setattr(racefd, "_ALT", {})
    monkeypatch.setattr(racefd, "_ip_of", lambda url: racefd.urlparse(url).netloc)
    monkeypatch.setattr(racefd, "READ_TIMEOUT", 1)
    monkeypatch.setattr(racefd, "CONNECT_TIMEOUT", 1)
    for ip, _, _ in hostban.listing():
        hostban.clear(ip)
    yield


def test_hostban_roundtrip_and_ttl():
    hostban.ban("203.0.113.5")
    assert hostban.is_banned("203.0.113.5")
    hostban.clear("203.0.113.5")
    assert not hostban.is_banned("203.0.113.5")
    hostban.ban("203.0.113.6", ttl=0.2)
    assert hostban.is_banned("203.0.113.6")
    _time.sleep(0.3)
    hostban._invalidate()
    assert not hostban.is_banned("203.0.113.6"), "запись не истекла"


def test_banned_server_is_not_touched(server, tmp_path, fresh_racefd, monkeypatch):
    """Сервер уже в списке, а ссылка на другом сервере известна — в
    заблокированный не уходит ни одного соединения."""
    dead_url, seen, close = _silent_server()
    hostban.ban(racefd.urlparse(dead_url).netloc)
    info = {"url": dead_url, "filesize": len(DATA), "http_headers": {},
            "format_id": "401", "webpage_url": "https://www.youtube.com/watch?v=y"}
    racefd._ALT[racefd._alt_key(info, len(DATA))] = {racefd.urlparse(server).netloc: server}
    monkeypatch.setattr(racefd, "_fresh_url", lambda i, s: None)
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    fd = racefd.RaceFD(ydl, {"quiet": True, "noprogress": True})
    out = tmp_path / "b.mp4"
    try:
        assert fd.real_download(str(out), info)
    finally:
        close()
    assert out.read_bytes() == DATA
    assert seen["n"] == 0, f"в заблокированный сервер постучались {seen['n']} раз"


def test_blocked_server_gets_banned(server, tmp_path, fresh_racefd, monkeypatch):
    dead_url, seen, close = _silent_server()
    monkeypatch.setattr(racefd, "RESOLVE_EVERY", 3)
    monkeypatch.setattr(racefd, "_fresh_url", lambda i, s: server)
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    fd = racefd.RaceFD(ydl, {"quiet": True, "noprogress": True})
    out = tmp_path / "c.mp4"
    try:
        assert fd.real_download(str(out), {"url": dead_url, "filesize": len(DATA),
                                           "http_headers": {}, "format_id": "401",
                                           "webpage_url": "https://www.youtube.com/watch?v=z"})
    finally:
        close()
    assert hostban.is_banned(racefd.urlparse(dead_url).netloc)
    assert not hostban.is_banned(racefd.urlparse(server).netloc)


def test_score_prefers_fast_and_demotes_slow(fresh_racefd, monkeypatch):
    """Медленный (<1 МБ/с) сервер ниже, быстрый (>5 МБ/с) выше; забаненный — последний."""
    monkeypatch.setattr(racefd, "_speed", {"slow:1": 0.5, "fast:1": 6.0})
    racefd._HOSTS.update({"slow:1": [5, 0], "fast:1": [5, 0], "new:1": [0, 0]})
    s = racefd._host_score
    assert s("http://fast:1/v") > s("http://new:1/v") > s("http://slow:1/v")
    hostban.ban("fast:1")
    assert s("http://fast:1/v") < s("http://slow:1/v")
    hostban.clear("fast:1")
