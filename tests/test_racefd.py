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
