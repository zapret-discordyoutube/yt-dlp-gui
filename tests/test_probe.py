"""Разбор метаданных: какие форматы попадают в выдачу, а какие отсеиваются.

Здесь закреплена дорогая ошибка: у yt-dlp строка "none" означает, что
дорожки точно нет, а None — что кодек просто неизвестен. Код писал
`f.get("vcodec") or "none"` и превращал неизвестный в отсутствующий, из-за
чего у всех сайтов, не сообщающих кодеки (то есть почти у всех, кроме
YouTube), выбрасывались ВСЕ форматы.
"""
import pytest

import downloader as dl


class FakeYDL:
    """Подменяет YoutubeDL: отдаёт заранее заданные метаданные без сети."""

    def __init__(self, info):
        self._info = info

    def __call__(self, opts):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return self._info

    def sanitize_info(self, info):
        return info


@pytest.fixture
def probe_with(monkeypatch):
    def run(info):
        monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", FakeYDL(info))
        return dl.probe("https://example.com/v")
    return run


def test_formats_with_unknown_codecs_are_kept(probe_with, fake_info):
    out = probe_with(fake_info)
    ids = [f["format_id"] for f in out["formats"]]
    assert "hls-480" in ids, "формат с vcodec=None выброшен — это регрессия"
    assert "mp4-low" in ids


def test_format_with_explicit_none_both_is_dropped(probe_with, fake_info):
    """Раскадровка не содержит ни видео, ни звука — её показывать незачем."""
    out = probe_with(fake_info)
    assert "sb0" not in [f["format_id"] for f in out["formats"]]


def test_kind_classification(probe_with, fake_info):
    out = probe_with(fake_info)
    kinds = {f["format_id"]: f["kind"] for f in out["formats"]}
    assert kinds["137"] == "video"        # есть видео, звука нет
    assert kinds["140"] == "audio"        # есть звук, видео нет
    assert kinds["hls-480"] == "both"     # неизвестно -> считаем полноценным


def test_unknown_codec_reported_as_none_not_crash(probe_with, fake_info):
    """Раньше здесь падал v.split('.') у None, и запрос отвечал общей
    ошибкой вместо выдачи форматов."""
    out = probe_with(fake_info)
    f = next(f for f in out["formats"] if f["format_id"] == "hls-480")
    assert f["vcodec"] is None and f["acodec"] is None


def test_codec_is_shortened(probe_with, fake_info):
    out = probe_with(fake_info)
    f = next(f for f in out["formats"] if f["format_id"] == "137")
    assert f["vcodec"] == "avc1"          # из 'avc1.640028'


def test_heights_include_unknown_codec_formats(probe_with, fake_info):
    """Список качеств строился по тому же принципу и терял те же форматы."""
    out = probe_with(fake_info)
    assert "480" in out["heights"]
    assert "1080" in out["heights"]


def test_duration_limit(probe_with, fake_info, monkeypatch):
    import config
    monkeypatch.setattr(config, "MAX_DURATION_SEC", 60)
    with pytest.raises(ValueError) as e:
        probe_with(fake_info)             # ролик на 120 секунд
    assert str(e.value) == "too_long"


def test_empty_formats_does_not_crash(probe_with):
    out = probe_with({"id": "x", "title": "Пусто", "formats": []})
    assert out["formats"] == []
    assert out["heights"] == []


def test_missing_title_gets_placeholder(probe_with):
    out = probe_with({"id": "x", "formats": []})
    assert out["title"] == "Без названия"


def test_video_without_formats_is_not_a_gallery(probe_with):
    """Ролик 18+ без форматов (YouTube требует вход) выдавался за «пост с
    фото» — предлагалось скачать его обложку."""
    info = {"id": "x", "title": "Ролик 18+", "duration": 518, "age_limit": 18,
            "thumbnail": "https://i.ytimg.com/vi/x/hq.jpg",
            "thumbnails": [{"url": "https://i.ytimg.com/vi/x/hq.jpg"}], "formats": []}
    with pytest.raises(ValueError, match="age_restricted"):
        probe_with(info)
    with pytest.raises(ValueError, match="no_formats"):
        probe_with({**info, "age_limit": 0})


def test_image_post_is_still_a_gallery(probe_with):
    info = {"id": "p", "title": "Пост", "formats": [],
            "thumbnails": [{"url": "https://pbs.twimg.com/media/a.jpg"}],
            "thumbnail": "https://pbs.twimg.com/media/a.jpg"}
    assert probe_with(info)["is_gallery"] is True


def test_silent_bot_check_retries_via_egress(monkeypatch):
    """Антибот YouTube при ignore_no_formats_error приходит «молча» — роликом
    без форматов. Такой ролик повторяем через egress."""
    import config
    monkeypatch.setattr(config, "EGRESS_PROXY", "socks5h://127.0.0.1:1")
    ok = {"id": "x", "title": "Ролик", "duration": 60,
          "formats": [{"format_id": "18", "ext": "mp4", "vcodec": "avc1",
                       "acodec": "mp4a", "height": 360}]}
    calls = []

    def fake(url, proxy):
        calls.append(proxy)
        return ok if proxy else {**ok, "formats": []}
    monkeypatch.setattr(dl, "_probe_extract", fake)
    out = dl.probe("https://www.youtube.com/watch?v=x")
    assert out["via_egress"] is True and out["formats"]
    assert calls == [None, "socks5h://127.0.0.1:1"]


def test_info_cache_reuse_rules(monkeypatch):
    """Готовый разбор отдаётся загрузке только свежим, тем же путём и под любым
    из адресов ролика; тяжёлые автосубтитры не хранятся."""
    monkeypatch.setattr(dl, "_info_cache", dl.OrderedDict())
    info = {"id": "v", "formats": [{"format_id": "18"}], "automatic_captions": {"en": ["x"] * 100}}
    dl.cache_info(("https://youtu.be/v?si=abc", "https://www.youtube.com/watch?v=v"), True, info)
    got = dl.cached_info("https://www.youtube.com/watch?v=v", True)
    assert got and "automatic_captions" not in got
    assert dl.cached_info("https://youtu.be/v?si=abc", True)
    assert dl.cached_info("https://www.youtube.com/watch?v=v", False) is None, \
        "ссылки из egress нельзя качать напрямую"
    monkeypatch.setattr(dl, "INFO_CACHE_SEC", -1)
    assert dl.cached_info("https://www.youtube.com/watch?v=v", True) is None


def test_info_cache_drops_format_selection(monkeypatch):
    """Разбор кэшируется без выбранного по умолчанию формата: иначе загрузка
    аудио наследовала requested_formats=[видео, звук] и падала на склейке
    с нескачанным видео (FileNotFoundError .f401.mkv)."""
    import yt_dlp
    monkeypatch.setattr(dl, "_info_cache", dl.OrderedDict())
    video = {"format_id": "401", "url": "https://x/v", "ext": "mp4", "protocol": "https",
             "vcodec": "av01", "acodec": "none", "height": 2160, "width": 3840}
    audio = {"format_id": "251", "url": "https://x/a", "ext": "webm", "protocol": "https",
             "vcodec": "none", "acodec": "opus", "abr": 130}
    info = {"id": "v", "title": "T", "extractor": "youtube", "extractor_key": "Youtube",
            "webpage_url": "https://www.youtube.com/watch?v=v",
            "formats": [audio, video], "requested_formats": [video, audio],
            "format_id": "401+251", "format": "401+251", "ext": "webm",
            "height": 2160, "vcodec": "av01", "acodec": "opus"}
    dl.cache_info(("https://www.youtube.com/watch?v=v",), False, info)
    got = dl.cached_info("https://www.youtube.com/watch?v=v", False)
    assert got["title"] == "T" and len(got["formats"]) == 2
    for k in ("requested_formats", "format_id", "format", "height", "vcodec", "ext"):
        assert k not in got, k
    with yt_dlp.YoutubeDL({"format": "bestaudio", "simulate": True, "quiet": True}) as ydl:
        res = ydl.process_ie_result(dict(got), download=True)
    assert res["format_id"] == "251" and not res.get("requested_formats")


def test_info_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(dl, "_info_cache", dl.OrderedDict())
    monkeypatch.setattr(dl, "INFO_CACHE_MAX", 3)
    for i in range(5):
        dl.cache_info((f"https://example.com/v{i}",), False, {"formats": [1]})
    assert len(dl._info_cache) == 3


@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/playlist?list=PLabc", True),
    ("https://www.youtube.com/@channel/videos", True),
    ("https://www.youtube.com/watch?v=x&list=PLabc", False),   # конкретный ролик
    ("https://youtu.be/x?list=PLabc", False),
    ("https://www.youtube.com/shorts/x", False),
    ("https://vimeo.com/showcase/1", False),
])
def test_is_youtube_list(url, expected):
    assert dl.is_youtube_list(url) is expected


def test_playlist_probe_lists_entries_fast(monkeypatch):
    """Плейлист — только список роликов (без разбора каждого), со ссылками."""
    flat = {"_type": "playlist", "title": "Курс", "uploader": "Автор", "playlist_count": 3,
            "entries": [{"id": "a1", "url": "https://www.youtube.com/watch?v=a1",
                         "title": "Первый", "duration": 60},
                        {"id": "b2", "title": "Второй"},                 # без url — соберём по id
                        {"id": None, "url": "javascript:alert(1)"}]}    # мусор — отбросить
    seen = {}

    class Y(FakeYDL):
        def __init__(self, opts):
            seen.update(opts)
            super().__init__(flat)

        def __call__(self, opts):
            return Y(opts)
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", lambda opts: Y(opts))
    out = dl.probe("https://www.youtube.com/playlist?list=PLx")
    assert out["is_playlist"] and out["count"] == 3
    assert [e["url"] for e in out["entries"]] == ["https://www.youtube.com/watch?v=a1",
                                                  "https://www.youtube.com/watch?v=b2"]
    assert seen.get("extract_flat") == "in_playlist"


@pytest.mark.parametrize("formats,expected", [
    ([{"format_id": "0", "vcodec": None, "acodec": "none", "ext": "mp4"},
      {"format_id": "d", "vcodec": "avc1", "acodec": "none", "ext": "mp4"}], False),  # немой рил
    ([{"format_id": "0", "vcodec": None, "acodec": None, "ext": "mp4"}], True),       # кодек неизвестен
    ([{"format_id": "v", "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
      {"format_id": "a", "vcodec": "none", "acodec": "mp4a", "ext": "m4a"}], True),
])
def test_probe_reports_silent_video(monkeypatch, formats, expected):
    """Немой ролик (Instagram/Pinterest): вкладку «Аудио» не предлагаем —
    извлечение звука падало на ffprobe с непонятной ошибкой."""
    monkeypatch.setattr(dl, "_info_cache", dl.OrderedDict())
    monkeypatch.setattr(dl, "_probe_extract", lambda url, proxy: {
        "id": "r", "title": "t", "duration": 10, "formats": formats,
        "webpage_url": "https://www.instagram.com/reel/r/"})
    assert dl.probe("https://www.instagram.com/reel/r/")["has_audio"] is expected


def test_silent_audio_error_is_explained():
    raw = ("ERROR: Postprocessing: WARNING: unable to obtain file audio codec with ffprobe")
    assert "нет звука" in dl._clean_err(raw)


def test_probe_multi_video_post(monkeypatch):
    """Твит/карусель с несколькими видео: форматы у записей, а не у поста.
    Высоты и звук берём из записей, число видео запоминаем для сервера."""
    monkeypatch.setattr(dl, "_info_cache", dl.OrderedDict())
    monkeypatch.setattr(dl, "_multi_posts", dl.OrderedDict())
    entry = lambda i: {"id": str(i), "formats": [
        {"format_id": "v", "vcodec": "avc1", "acodec": "mp4a", "height": 720, "ext": "mp4"}]}
    monkeypatch.setattr(dl, "_probe_extract", lambda url, proxy: {
        "_type": "playlist", "id": "p", "title": "t", "entries": [entry(1), entry(2)],
        "webpage_url": "https://x.com/u/status/1"})
    out = dl.probe("https://x.com/u/status/1")
    assert out["multi_count"] == 2 and out["heights"] == ["720"] and out["has_audio"]
    assert not out["is_gallery"]
    assert dl.multi_post("https://x.com/u/status/1") == {"videos": 2, "images": 0, "entries": 2}


def test_probe_mixed_carousel_is_not_a_gallery(monkeypatch):
    """Карусель из фото и видео: раньше — галерея (видео терялись), теперь —
    пост: видео и фото, номера фото — их места в карусели."""
    monkeypatch.setattr(dl, "_info_cache", dl.OrderedDict())
    monkeypatch.setattr(dl, "_multi_posts", dl.OrderedDict())
    photo = lambda i: {"id": f"p{i}", "thumbnails": [{"url": f"https://cdn/p{i}.jpg"}]}
    video = {"id": "v", "thumbnail": "https://cdn/v.jpg", "formats": [
        {"format_id": "v", "vcodec": "avc1", "acodec": "mp4a", "height": 1080, "ext": "mp4"}]}
    info = {"_type": "playlist", "id": "c", "title": "t",
            "entries": [photo(1), video, photo(3)],
            "webpage_url": "https://www.instagram.com/p/c/"}
    monkeypatch.setattr(dl, "_probe_extract", lambda url, proxy: info)
    out = dl.probe("https://www.instagram.com/p/c/")
    assert not out["is_gallery"] and out["multi_count"] == 1 and out["multi_images"] == 2
    assert dl.multi_post("https://www.instagram.com/p/c/") == {"videos": 1, "images": 2, "entries": 3}
    assert dl.collect_images_indexed(info) == [(1, "https://cdn/p1.jpg"), (3, "https://cdn/p3.jpg")]


# --- YouTube напрямую молчит (блокировка по SNI) -> сразу через egress --------
# 13.10.2026 провайдер начал глотать TLS с именем youtube.com: ошибки нет,
# yt-dlp ждёт по 30 с на запрос, «Проверяем…» висело минутами 16 часов подряд.

_YT_OK = {"id": "x", "title": "Ролик", "duration": 60,
          "formats": [{"format_id": "18", "ext": "mp4", "vcodec": "avc1",
                       "acodec": "mp4a", "height": 360}]}


@pytest.fixture
def egress(monkeypatch):
    import config
    monkeypatch.setattr(config, "EGRESS_PROXY", "socks5h://127.0.0.1:1")
    monkeypatch.setattr(config, "PROXY", "")
    monkeypatch.setattr(dl, "_info_cache", dl.OrderedDict())
    calls = []
    monkeypatch.setattr(dl, "_probe_extract",
                        lambda url, proxy: (calls.append(proxy), dict(_YT_OK))[1])
    return calls


def test_youtube_silent_directly_goes_via_egress_at_once(monkeypatch, egress):
    """Прямой путь молчит — прямой разбор не запускается вовсе: он не упал
    бы с ошибкой, а провисел бы минуты."""
    monkeypatch.setattr(dl, "_yt_direct_check", lambda: False)
    out = dl.probe("https://www.youtube.com/watch?v=x")
    assert out["via_egress"] is True
    assert egress == ["socks5h://127.0.0.1:1"]
    assert dl.cached_info("https://www.youtube.com/watch?v=x", True), \
        "разбор через egress — загрузке, тем же путём"


def test_empty_answer_via_egress_is_retried_once(monkeypatch, egress):
    """Туннель бывает медленным, и yt-dlp молча возвращает ролик без форматов.
    Прямого пути нет — пробуем egress ещё раз, а не отдаём ошибку."""
    monkeypatch.setattr(dl, "_yt_direct_check", lambda: False)
    answers = iter([{**_YT_OK, "formats": []}, dict(_YT_OK)])
    monkeypatch.setattr(dl, "_probe_extract",
                        lambda url, proxy: (egress.append(proxy), next(answers))[1])
    out = dl.probe("https://www.youtube.com/watch?v=x")
    assert out["via_egress"] is True and out["formats"]
    assert egress == ["socks5h://127.0.0.1:1"] * 2


def test_reach_check_concerns_only_youtube(monkeypatch, egress):
    asked = []
    monkeypatch.setattr(dl, "_yt_direct_check", lambda: (asked.append(1), False)[1])
    out = dl.probe("https://vimeo.com/1")
    assert out["via_egress"] is False and egress == [None] and not asked


def test_reach_check_can_be_switched_off(monkeypatch, egress):
    import config
    monkeypatch.setattr(config, "YT_REACH_CHECK", False)
    monkeypatch.setattr(dl, "_yt_direct_check", lambda: False)
    assert dl.probe("https://www.youtube.com/watch?v=x")["via_egress"] is False
    assert egress == [None]


def test_reach_check_is_cached_and_one_failure_is_not_an_outage(monkeypatch):
    """Проверка — раз в TTL, а не на каждый разбор; одна неудача из двух
    попыток — случайность, а не блокировка."""
    answers = iter([False, True])
    asked = []
    monkeypatch.setattr(dl, "_yt_direct_check", lambda: (asked.append(1), next(answers))[1])
    assert dl.yt_direct_ok() is True
    assert dl.yt_direct_ok() is True
    assert len(asked) == 2, "вторая попытка — только после неудачной первой, дальше кэш"


def test_reach_outage_and_recovery_are_logged_once(monkeypatch, capsys):
    """Поломку должно быть видно в журнале: 13.10.2026 он был пуст. Строка —
    на смену состояния, а не на каждую проверку."""
    import config
    monkeypatch.setattr(config, "EGRESS_PROXY", "socks5h://127.0.0.1:1")
    state = {"ok": False}
    monkeypatch.setattr(dl, "_yt_direct_check", lambda: state["ok"])
    monkeypatch.setattr(dl, "YT_REACH_FAIL_TTL_SEC", -1)     # без кэша: каждая проверка настоящая
    assert dl.yt_direct_ok() is False
    assert dl.yt_direct_ok() is False
    assert dl.yt_direct_state()["ok"] is False
    out = capsys.readouterr().out
    assert out.count("[youtube] напрямую не отвечает") == 1 and "через egress" in out
    state["ok"] = True
    assert dl.yt_direct_ok() is True
    assert "[youtube] напрямую снова отвечает" in capsys.readouterr().out
    assert dl.yt_direct_state()["ok"] is True


def test_https_alive_gives_up_on_silent_server():
    """Так выглядит блокировка по SNI: TCP принят, в ответ — тишина. Проверка
    обязана сдаться по таймауту, а не ждать вечно."""
    import socket
    import time
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    addr = srv.getsockname()
    try:
        t0 = time.time()
        assert dl._https_alive("www.youtube.com", timeout=0.3, connect_to=addr) is False
        assert time.time() - t0 < 2
    finally:
        srv.close()
    assert dl._https_alive("www.youtube.com", timeout=0.3, connect_to=addr) is False, \
        "порт закрыт — тоже «нет»"


def test_playlist_probe_via_egress_when_youtube_is_silent(monkeypatch):
    import config
    monkeypatch.setattr(config, "EGRESS_PROXY", "socks5h://127.0.0.1:1")
    monkeypatch.setattr(config, "PROXY", "")
    monkeypatch.setattr(config, "YT_SOURCE_IP", "192.0.2.1")
    monkeypatch.setattr(dl, "_yt_direct_check", lambda: False)
    seen = {}

    class Y(FakeYDL):
        def __init__(self, opts):
            seen.update(opts)
            super().__init__({"title": "Курс", "entries": []})
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", lambda opts: Y(opts))
    out = dl.probe("https://www.youtube.com/playlist?list=PLx")
    assert out["via_egress"] is True
    assert seen.get("proxy") == "socks5h://127.0.0.1:1" and "source_address" not in seen, \
        "через прокси свой адрес не привязываем"


# --- Туннель пула для разбора через egress ---------------------------------

def test_egress_pool_skips_dead_tunnel(monkeypatch):
    """Разбор через egress брал первый туннель пула; 13.10.2026 он был «живым
    трупом» (ssh держит связь, данные не идут), и загрузка висела на подготовке
    при 31 рабочем туннеле."""
    import config
    pool = ["socks5://127.0.0.1:1", "socks5://127.0.0.1:2", "socks5://127.0.0.1:3"]
    monkeypatch.setattr(config, "EGRESS_POOL", pool)
    monkeypatch.setattr(dl.random, "randrange", lambda n: 0)
    monkeypatch.setattr(dl, "_tunnel_alive", lambda proxy: proxy != pool[0])
    assert dl.egress_pool_proxy() == pool[1]
    monkeypatch.setattr(dl, "_tunnel_alive", lambda proxy: False)
    assert dl.egress_pool_proxy() == pool[0], "все молчат — отдаём хоть какой-то, а не падаем"


def test_tunnel_alive_checks_the_far_end():
    """Порт туннеля отвечает всегда; «жив» — когда соединение дошло дальше."""
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert dl._tunnel_alive(f"socks5://127.0.0.1:{port}", timeout=0.3) is False
    finally:
        srv.close()
    assert dl._tunnel_alive(f"socks5://127.0.0.1:{port}", timeout=0.3) is False

