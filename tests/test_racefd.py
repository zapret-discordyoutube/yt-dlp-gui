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
def env_proxy(monkeypatch):
    """Чужой прокси в окружении (как в оболочке с HTTP_PROXY): requests
    ставит его выше Session.proxies. Сессиям racefd он не должен мешать."""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")
    for k in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)


def test_direct_session_ignores_env_proxy(server, env_proxy):
    """Прямой путь — напрямую со своего IP, мимо прокси окружения."""
    r = racefd._direct_session().get(server, headers={"Range": "bytes=0-99"}, timeout=5)
    assert r.status_code == 206 and r.content == DATA[:100]


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


def test_falls_back_to_egress_pool(server, tmp_path, fresh_racefd, env_proxy, monkeypatch):
    """Напрямую ничего не проходит — загрузка сама разбирает ролик через
    egress и качает через пул туннелей. «Туннель» здесь — HTTP-прокси
    (тестовый сервер принимает запрос с абсолютным адресом), а ссылка ведёт
    на хост, достижимый только через него."""
    dead_url, seen, close = _silent_server()
    monkeypatch.setattr(racefd.config, "EGRESS_POOL", [server.rsplit("/", 1)[0]])
    monkeypatch.setattr(racefd, "EGRESS_AFTER", 1)
    monkeypatch.setattr(racefd, "_egress", {"ok": 0, "fail": 0, "speed": None})
    racefd.STATS["egress_bytes"] = 0
    via = []

    def fake_fresh(info, size, proxy=None):
        via.append(proxy)
        return "http://egress-only.invalid/videoplayback" if proxy else None
    monkeypatch.setattr(racefd, "_fresh_url", fake_fresh)
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    fd = racefd.RaceFD(ydl, {"quiet": True, "noprogress": True})
    out = tmp_path / "e.mp4"
    try:
        assert fd.real_download(str(out), {"url": dead_url, "filesize": len(DATA),
                                           "http_headers": {}, "format_id": "401",
                                           "webpage_url": "https://www.youtube.com/watch?v=e"})
    finally:
        close()
    assert out.read_bytes() == DATA
    assert any(via), "разбор через egress не запускался"
    assert racefd.STATS["egress_bytes"] > 0


def test_egress_rotates_away_from_stuck_tunnel(server, tmp_path, fresh_racefd, env_proxy,
                                                monkeypatch):
    """Задача целиком через egress, один туннель пула подвис — потоки на нём
    переходят на следующий туннель, а не переподключаются через тот же."""
    dead_url, seen, close = _silent_server()
    dead_proxy = dead_url.rsplit("/", 1)[0]
    good_proxy = server.rsplit("/", 1)[0]
    monkeypatch.setattr(racefd.config, "EGRESS_POOL", [dead_proxy, good_proxy])
    monkeypatch.setattr(racefd, "EGRESS_READ_TIMEOUT", 1)
    monkeypatch.setattr(racefd, "_egress", {"ok": 0, "fail": 0, "speed": None})
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    fd = racefd.RaceFD(ydl, {"quiet": True, "noprogress": True, "proxy": dead_proxy})
    out = tmp_path / "r.mp4"
    try:
        assert fd.real_download(str(out), {"url": "http://egress-only.invalid/videoplayback",
                                           "filesize": len(DATA), "http_headers": {},
                                           "format_id": "401",
                                           "webpage_url": "https://www.youtube.com/watch?v=r"})
    finally:
        close()
    assert out.read_bytes() == DATA
    assert seen["n"] > 0, "подвисший туннель вообще не использовался — тест ничего не проверил"


class _ForbiddenHandler(BaseHTTPRequestHandler):
    """Ссылка протухла: сервер отвечает, но только 403."""
    calls = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).calls += 1
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def forbidden_server():
    _ForbiddenHandler.calls = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ForbiddenHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/videoplayback"
    srv.shutdown()


def test_dead_link_is_replaced_and_server_not_banned(server, forbidden_server, tmp_path,
                                                     fresh_racefd, monkeypatch):
    """403 — мёртвая ссылка, а не DPI: берём свежую, сервер в бан не идёт."""
    monkeypatch.setattr(racefd, "RESOLVE_EVERY", 0.5)
    monkeypatch.setattr(racefd, "_fresh_url", lambda i, s: server)
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    fd = racefd.RaceFD(ydl, {"quiet": True, "noprogress": True})
    out = tmp_path / "f.mp4"
    assert fd.real_download(str(out), {"url": forbidden_server, "filesize": len(DATA),
                                       "http_headers": {}, "format_id": "251",
                                       "webpage_url": "https://www.youtube.com/watch?v=f"})
    assert out.read_bytes() == DATA
    assert not hostban.is_banned(racefd.urlparse(forbidden_server).netloc)


def test_all_links_dead_fails_fast(forbidden_server, tmp_path, fresh_racefd, monkeypatch):
    """Все ссылки отвечают 403, новых нет — сдаёмся сразу (исполнитель
    разберёт ролик заново), а не долбим 403 до MAX_FAILS."""
    monkeypatch.setattr(racefd, "RESOLVE_EVERY", 0.2)
    monkeypatch.setattr(racefd, "_fresh_url", lambda i, s: None)
    monkeypatch.setattr(racefd.config, "EGRESS_POOL", [])
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    fd = racefd.RaceFD(ydl, {"quiet": True, "noprogress": True})
    t0 = _time.monotonic()
    with pytest.raises(yt_dlp.utils.DownloadError, match="403"):
        fd.real_download(str(tmp_path / "g.mp4"),
                         {"url": forbidden_server, "filesize": len(DATA), "http_headers": {},
                          "format_id": "251", "webpage_url": "https://www.youtube.com/watch?v=g"})
    assert _time.monotonic() - t0 < 10
    assert _ForbiddenHandler.calls < 100


# ---- Отрезок «по запросу»: SectionSession / SectionPP ----

def _get_range(url: str, start: int, nbytes: int, timeout: float = 10) -> bytes:
    """Прочитать nbytes с позиции start и закрыть соединение — как ffmpeg,
    который дочитал своё и ушёл."""
    import http.client
    u = racefd.urlparse(url)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
    c.request("GET", u.path, headers={"Range": f"bytes={start}-"})
    r = c.getresponse()
    assert r.status == 206
    assert r.getheader("Content-Range") == f"bytes {start}-{len(DATA) - 1}/{len(DATA)}"
    buf = b""
    while len(buf) < nbytes:
        part = r.read(min(65536, nbytes - len(buf)))
        if not part:
            break
        buf += part
    c.close()
    return buf


@pytest.fixture
def no_resolve(monkeypatch):
    """Без сети: повторные разборы и egress выключены."""
    monkeypatch.setattr(racefd, "_fresh_url", lambda *a, **k: None)
    monkeypatch.setattr(racefd.config, "EGRESS_POOL", [])


def test_section_session_downloads_only_what_is_read(server, fresh_racefd, no_resolve):
    """ffmpeg читает голову и отрезок — только эти куски и качаются.
    Читатель — объект, рвущий запись после n байт (как ffmpeg, который
    дочитал своё): у сокета на localhost буферы в мегабайты, и тест по
    настоящему соединению зависел бы от них."""
    class Reader:
        def __init__(self, n):
            self.n, self.buf = n, b""

        def write(self, data):
            if len(self.buf) >= self.n:
                raise BrokenPipeError
            self.buf += data

    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    fmt = {"url": server, "filesize": len(DATA), "http_headers": {}, "format_id": "137",
           "webpage_url": "https://www.youtube.com/watch?v=s"}
    sess = racefd.SectionSession(ydl, [fmt], est_total=None)
    try:
        d = sess.demands[0]
        head = Reader(1000)
        sess._stream(0, d, 0, len(DATA) - 1, head)
        assert head.buf[:1000] == DATA[:1000]
        pos = 3 * racefd.CHUNK + 777
        clip = Reader(5000)
        sess._stream(0, d, pos, len(DATA) - 1, clip)
        assert clip.buf[:5000] == DATA[pos:pos + 5000]
        _time.sleep(0.3)
        # Голова (кусок 0) и отрезок (3, с упреждением 4), но не весь файл:
        # кусок 2 никому не был нужен.
        assert {0, 3} <= d.finished and 2 not in d.finished
        assert d.served >= 6000
        # Отдано через HTTP — то же самое.
        assert _get_range(sess.url(0), pos, 5000) == DATA[pos:pos + 5000]
    finally:
        sess.close()


def test_section_session_rejects_foreign_paths(server, fresh_racefd, no_resolve):
    import urllib.error
    import urllib.request
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    sess = racefd.SectionSession(ydl, [{"url": server, "filesize": len(DATA),
                                        "http_headers": {}}], est_total=None)
    try:
        bad = sess.url(0).replace(sess.token, "0" * 24)
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad, timeout=5)
    finally:
        sess.close()


def test_section_session_breaks_response_when_source_dies(forbidden_server, fresh_racefd,
                                                          no_resolve, monkeypatch):
    """Источник мёртв (403, новых ссылок нет) — ответ обрывается сразу:
    ffmpeg получает ошибку, а не висит до сторожа простоя."""
    monkeypatch.setattr(racefd, "RESOLVE_EVERY", 0.2)
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    sess = racefd.SectionSession(ydl, [{"url": forbidden_server, "filesize": len(DATA),
                                        "http_headers": {}, "format_id": "251",
                                        "webpage_url": "https://www.youtube.com/watch?v=d"}],
                                 est_total=None)
    try:
        t0 = _time.monotonic()
        assert _get_range(sess.url(0), 0, 1000, timeout=30) == b""
        assert _time.monotonic() - t0 < 15
    finally:
        sess.close()


def test_section_session_reports_progress_and_stops_on_hook_error(server, fresh_racefd,
                                                                   no_resolve):
    """Прогресс — то, что отдано ffmpeg; хук оборвал задачу (размер, отмена)
    — сессия рвёт ответы."""
    seen = []

    def hook(d):
        seen.append(d)
        if d.get("downloaded_bytes"):
            raise yt_dlp.utils.DownloadCancelled()
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "progress_hooks": [hook]})
    sess = racefd.SectionSession(ydl, [{"url": server, "filesize": len(DATA),
                                        "http_headers": {}}], est_total=len(DATA))
    try:
        assert _get_range(sess.url(0), 0, 1000) == DATA[:1000]
        deadline = _time.monotonic() + 5
        while not sess.demands[0].dead and _time.monotonic() < deadline:
            _time.sleep(0.1)
        assert sess.demands[0].dead
        assert any(d.get("downloaded_bytes") for d in seen)
        with pytest.raises(ConnectionRefusedError):     # новое соединение — отказ сразу
            _get_range(sess.url(0), 0, 1000, timeout=5)
    finally:
        sess.close()


def test_section_pp_rewrites_googlevideo_and_falls_back_on_hls(monkeypatch):
    """Дорожки для RaceFD — ссылки на локальный сервер; HLS — отрезок снят,
    ролик качается целиком (on_fallback)."""
    monkeypatch.setattr(racefd, "SectionSession", lambda ydl, fmts, est, stop: _FakeSess(fmts, est))
    ydl = yt_dlp.YoutubeDL({"quiet": True})
    fell = []
    pp = racefd.SectionPP(ydl, 3600, on_fallback=lambda: fell.append(1))
    gv = "https://rr1---sn-x.googlevideo.com/videoplayback?clen="
    info = {"id": "x", "section_start": 600, "section_end": 840, "webpage_url": "w",
            "requested_formats": [
                {"format_id": "137", "protocol": "https", "url": gv + "3600000000"},
                {"format_id": "140", "protocol": "https", "url": gv + "360000000"}]}
    _, out = pp.run(info)
    assert [f["url"] for f in out["requested_formats"]] == ["local/0", "local/1"]
    assert pp.session.est == (3600000000 + 360000000) * 240 // 3600
    assert not fell
    hls = {"id": "x", "section_start": 600, "section_end": 840, "protocol": "m3u8_native",
           "url": "https://manifest.googlevideo.com/x.m3u8"}
    _, out = pp.run(hls)
    assert fell and "section_start" not in out and out["url"].endswith(".m3u8")
    no_clip = {"id": "x", "protocol": "https", "url": gv + "1"}
    assert pp.run(dict(no_clip))[1] == no_clip


class _FakeSess:
    def __init__(self, fmts, est):
        self.est = est

    def url(self, n):
        return f"local/{n}"

    def close(self):
        pass
