"""Селекторы форматов.

Это место ломалось чаще всего и самым незаметным образом: регистр кодека,
потерянный '?' у фильтра высоты, пропавшие кавычки вокруг regex. Поэтому
проверяем двумя способами — дословным сравнением строки и компиляцией
селектора самим yt-dlp (сеть при этом не нужна).
"""
import itertools

import pytest
import yt_dlp

import downloader as dl


def compiles(selector: str) -> bool:
    """Селектор принимается самим yt-dlp?"""
    yt_dlp.YoutubeDL({"quiet": True}).build_format_selector(selector)
    return True


# --- дословные эталоны ------------------------------------------------------

def test_video_selector_exact():
    sel, extra, label = dl.build_format("video", height="1080",
                                        vcodec="h264", vaudio="aac")
    assert sel == (
        "bestvideo[height<=?1080][vcodec~='(?i:^(avc1|h264))']"
        "+bestaudio[acodec~='(?i:^mp4a)']"
        "/bestvideo[height<=?1080][vcodec~='(?i:^(avc1|h264))']+bestaudio"
        # ступень без фильтра кодека: без неё на сайтах с раздельными
        # дорожками выбор проваливался мимо склейки к муксованному 360p
        "/bestvideo[height<=?1080]+bestaudio"
        "/best[height<=?1080][vcodec~='(?i:^(avc1|h264))']"
        "/best[height<=?1080]"
        # замыкающий вариант обязан уважать ограничение высоты
        "/worst"
    )
    assert extra["merge_output_format"] == "mp4"
    assert label == "1080p · H.264 · AAC"


def test_height_filter_tolerates_unknown_height():
    """Без '?' форматы с height=None молча выбрасываются."""
    sel, _, _ = dl.build_format("video", height="720")
    assert "[height<=?720]" in sel
    assert "[height<=720]" not in sel


def test_vp9_matches_both_spellings():
    """VP9 приходит и как 'vp9', и как 'vp09.xx', но не должен цеплять vp8."""
    sel, extra, _ = dl.build_format("video", vcodec="vp9")
    assert "^vp0?9" in sel
    assert extra["merge_output_format"] == "webm"


def test_codec_filters_are_case_insensitive():
    """Сайты отдают и 'avc1', и 'AVC1'; фильтры yt-dlp регистрозависимы."""
    for vcodec in ("h264", "av1", "vp9"):
        sel, _, _ = dl.build_format("video", vcodec=vcodec)
        assert "(?i:" in sel, vcodec


def test_audio_prefers_matching_source_to_avoid_reencode():
    """Если цель — AAC, источник тоже должен быть AAC: тогда поток копируется."""
    sel, extra, _ = dl.build_format("audio", acodec="aac")
    assert sel.startswith("bestaudio[acodec^=mp4a]")
    assert extra["postprocessors"][0]["preferredcodec"] == "aac"

    sel, _, _ = dl.build_format("audio", acodec="opus")
    assert sel.startswith("bestaudio[acodec^=opus]")


def test_audio_original_does_not_set_quality():
    """«Оригинал» копирует дорожку, перекодирования быть не должно."""
    _, extra, label = dl.build_format("audio", acodec="best")
    pp = extra["postprocessors"][0]
    assert pp["preferredcodec"] == "best"
    assert "preferredquality" not in pp
    assert label == "Оригинал"


# --- компиляция всех сочетаний ---------------------------------------------

@pytest.mark.parametrize("height,vcodec,vaudio", list(itertools.product(
    ["auto", "2160", "1080", "480", "360"],
    ["auto", "h264", "av1", "vp9"],
    ["auto", "aac", "opus"],
)))
def test_every_video_combination_compiles(height, vcodec, vaudio):
    sel, _, _ = dl.build_format("video", height=height,
                                vcodec=vcodec, vaudio=vaudio)
    assert compiles(sel)


@pytest.mark.parametrize("acodec", ["best", "mp3", "aac", "opus"])
def test_every_audio_combination_compiles(acodec):
    sel, _, _ = dl.build_format("audio", acodec=acodec)
    assert compiles(sel)


# --- отказы -----------------------------------------------------------------

@pytest.mark.parametrize("kwargs,code", [
    ({"kind": "video", "height": "999"}, "unknown_height"),
    ({"kind": "video", "vcodec": "theora"}, "unknown_vcodec"),
    ({"kind": "video", "vaudio": "flac"}, "unknown_vaudio"),
    ({"kind": "audio", "acodec": "wav"}, "unknown_acodec"),
    ({"kind": "картинка"}, "unknown_kind"),
])
def test_build_format_rejects_unknown(kwargs, code):
    kind = kwargs.pop("kind")
    with pytest.raises(ValueError) as e:
        dl.build_format(kind, **kwargs)
    assert str(e.value) == code


# --- выбор формата по идентификатору ---------------------------------------

@pytest.mark.parametrize("fid", [
    "137", "137+140", "hls-480", "616", "mp4-low",
    # DASH отдаёт Representation@id как есть: '=', ':', '@', '~' в нём
    # встречаются, и прежняя регулярка ломала продвинутый режим целиком
    "video=1500000", "audio=128000", "video=1500000+audio=128000",
])
def test_format_id_accepts_real_ids(fid):
    sel, _, _ = dl.build_from_format_id(fid)
    assert sel == fid


@pytest.mark.parametrize("fid", [
    "bestvideo[height<=1080]",   # синтаксис селектора
    "a/b", "137 140", "$(id)", "../x", "137+140+141", "",
    "a" * 70,                                                 # длиннее предела
    "all", "mergeall", "best", "worst", "bv", "ba", "BEST",   # ключевые слова
    "all+140", "137+all",
])
def test_format_id_rejects_selectors_and_keywords(fid):
    """'all' и 'mergeall' заставляли скачивать все дорожки сразу — это и
    обход выбора пользователя, и усиление нагрузки."""
    with pytest.raises(ValueError):
        dl.build_from_format_id(fid)


def test_format_id_pair_merges_into_safe_container():
    _, extra, _ = dl.build_from_format_id("137+140")
    assert extra["merge_output_format"] == "mkv"
    _, extra, _ = dl.build_from_format_id("137+140", container="mp4")
    assert extra["merge_output_format"] == "mp4"
    _, extra, _ = dl.build_from_format_id("137+140", container="; rm -rf /")
    assert extra["merge_output_format"] == "mkv"
