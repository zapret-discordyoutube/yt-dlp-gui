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
