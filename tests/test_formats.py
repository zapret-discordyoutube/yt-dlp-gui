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
        # bv*, а не bestvideo: лучшим бывает готовый файл со звуком
        "bv*[height<=?1080][vcodec~='(?i:^(avc1|h264))']"
        "+ba[acodec~='(?i:^mp4a)']"
        "/bv*[height<=?1080][vcodec~='(?i:^(avc1|h264))']+ba"
        # ступень без фильтра кодека: без неё на сайтах с раздельными
        # дорожками выбор проваливался мимо склейки к муксованному 360p
        "/bv*[height<=?1080]+ba"
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


def test_postprocessor_order_thumbnail_last():
    """Обложка раньше метаданных роняла ffmpeg на Opus («Conversion failed!»)."""
    fmt, extra, _ = dl.build_format("audio", acodec="best")
    feats = {k: True for k in ("embed_thumbnail", "embed_metadata", "embed_chapters",
                               "sponsorblock")}
    _, extra, _, _ = dl.apply_features(fmt, extra, feats, "audio")
    keys = [pp["key"] for pp in extra["postprocessors"]]
    assert keys[0] == "FFmpegExtractAudio"
    assert keys[-1] == "EmbedThumbnail"
    assert keys.index("FFmpegMetadata") < keys.index("EmbedThumbnail")
    assert keys.index("SponsorBlock") < keys.index("ModifyChapters")


def test_no_merge_on_preset_cascade_asks_runner_to_split():
    """Пресет видео — каскад с '/': запятую не подставляем (сломала бы запасные
    варианты), а просим исполнителя выбрать по каскаду и качать раздельно."""
    fmt, extra, _ = dl.build_format("video", height="720")
    f2, e2, _, bundle = dl.apply_features(fmt, extra, {"no_merge": True}, "video")
    assert f2 == fmt and e2.get(dl.SPLIT_KEY) is True
    assert "merge_output_format" not in e2 and bundle
    # Явная связка без каскада — как раньше, запятая.
    f3, e3, _, _ = dl.apply_features("137+140", {}, {"no_merge": True}, "format")
    assert f3 == "137,140" and dl.SPLIT_KEY not in e3


def test_thumbnail_never_targets_webm():
    """В webm обложку не встроить: склейка VP9 — в mkv, отдельный webm —
    перепаковка в mkv до встраивания."""
    fmt, extra, _ = dl.build_format("video", vcodec="vp9")
    _, e, _, _ = dl.apply_features(fmt, extra, {"embed_thumbnail": True}, "video")
    assert e["merge_output_format"] == "mkv"
    keys = [pp["key"] for pp in e["postprocessors"]]
    assert keys.index("FFmpegVideoRemuxer") < keys.index("EmbedThumbnail")
    # Звук не трогаем: он уходит в opus/m4a/mp3 через FFmpegExtractAudio.
    fmt, extra, _ = dl.build_format("audio", acodec="opus")
    _, e, _, _ = dl.apply_features(fmt, extra, {"embed_thumbnail": True}, "audio")
    assert "FFmpegVideoRemuxer" not in [pp["key"] for pp in e["postprocessors"]]


def test_video_size_is_best_quality_not_users_choice():
    """Размер ролика для ленты: лучшее разрешение (при равном — больший
    битрейт) + лучший звук; без раздельных дорожек — лучший общий файл."""
    import downloader as dl
    info = {"duration": 100, "formats": [
        {"format_id": "18", "vcodec": "avc1", "acodec": "mp4a", "height": 360, "filesize": 10},
        {"format_id": "137", "vcodec": "avc1", "acodec": "none", "height": 1080, "tbr": 3000,
         "filesize": 1000},
        {"format_id": "248", "vcodec": "vp9", "acodec": "none", "height": 1080, "tbr": 2000,
         "filesize": 700},
        {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "abr": 128, "filesize": 50},
        {"format_id": "251", "vcodec": "none", "acodec": "opus", "abr": 160, "filesize": 60},
        {"format_id": "sb0", "vcodec": "none", "acodec": "none"},
    ]}
    assert dl.video_size(info) == 1000 + 60
    assert dl.video_size({"duration": 10, "formats": [
        {"vcodec": "h264", "acodec": "aac", "height": 720, "tbr": 800}]}) == 800 * 125 * 10
    assert dl.video_size({"formats": [{"vcodec": "none", "acodec": "mp3", "filesize": 5}]}) == 5
    assert dl.video_size(None) is None and dl.video_size({"formats": []}) is None


# --- «Авто» = лучшее из лучших ----------------------------------------------
# Форматы — как у YouTube: на одном разрешении AV1 с самым урезанным
# битрейтом, H.264/VP9 щедрее; звук — оригинал, дубляж и DRC.

def _yt_formats(scale: float = 1.0, portrait: bool = False):
    def v(fid, vcodec, h, vbr, fps=30, dr="SDR"):
        w = h * 16 // 9
        if portrait:
            w, h = h, w
        return {"format_id": fid, "url": f"https://r1.googlevideo.com/{fid}",
                "protocol": "https", "ext": "mp4", "vcodec": vcodec, "acodec": "none",
                "width": w, "height": h, "fps": fps, "dynamic_range": dr,
                "vbr": vbr, "tbr": vbr, "filesize": int(vbr * 125 * 600 * scale)}

    def a(fid, acodec, abr, lang="en", pref=10, quality=3, note=""):
        return {"format_id": fid, "url": f"https://r1.googlevideo.com/{fid}",
                "protocol": "https", "ext": "webm", "vcodec": "none", "acodec": acodec,
                "abr": abr, "tbr": abr, "language": lang, "language_preference": pref,
                "quality": quality, "format_note": note,
                "filesize": int(abr * 125 * 600 * scale)}
    return [
        v("401", "av01.0.12M.08", 2160, 10466), v("313", "vp9", 2160, 16241),
        v("701", "av01.0.13M.10", 2160, 24632, dr="HDR10"),
        v("399", "av01.0.08M.08", 1080, 747), v("248", "vp9", 1080, 900),
        v("137", "avc1.640028", 1080, 1443),
        a("251", "opus", 124, note="original"), a("140", "mp4a.40.2", 129),
        a("251-20", "opus", 160, lang="ml", pref=-1),
        a("251-drc", "opus", 140, quality=2.5),
    ]


def _yt_info(**kw):
    return {"id": "x", "duration": 600 * kw.pop("dur_scale", 1),
            "formats": _yt_formats(**kw)}


YT = "https://www.youtube.com/watch?v=x"


def test_auto_takes_highest_video_bitrate_not_av1():
    """AV1 у YouTube урезан по битрейту: «Авто» берёт самую плотную дорожку
    разрешения (4K — VP9, 1080p — H.264), SDR вместо HDR, звук — оригинал
    Opus, не дубляж и не DRC."""
    picks = dl.preview_picks(_yt_info(), YT, ["2160", "1080"])
    assert (picks["auto"]["res"], picks["auto"]["vcodec"], picks["auto"]["hdr"]) == (2160, "vp9", False)
    assert picks["1080"]["vcodec"] == "avc1"
    assert "capped" not in picks["auto"]


def test_auto_audio_stays_original_track():
    info = _yt_info()
    ydl = yt_dlp.YoutubeDL({"quiet": True, "format_sort": list(dl.YT_FORMAT_SORT)})
    ydl.sort_formats(info)
    sel, _, _ = dl.build_format("video")
    chosen = next(iter(ydl.build_format_selector(sel)({
        "formats": info["formats"], "has_merged_format": False,
        "incomplete_formats": False})))
    assert [f["format_id"] for f in chosen["requested_formats"]] == ["313", "251"]


def test_auto_falls_back_to_what_fits_the_size_limit():
    """Лучшее не влезает в MAX_FILESIZE_MB — «Авто» берёт лучшее из того, что
    влезает (а не падает, скачав 2 ГБ), и отмечает это для интерфейса."""
    # 600 с × 8: 4K VP9 ≈ 9,7 ГБ, 4K AV1 ≈ 6,3 ГБ, 1080p H.264 ≈ 0,87 ГБ
    picks = dl.preview_picks(_yt_info(scale=8, dur_scale=8), YT, ["2160", "1080"])
    assert (picks["auto"]["res"], picks["auto"]["vcodec"]) == (1080, "avc1")
    assert picks["auto"]["capped"] is True


def test_size_cap_cascade_ends_without_cap():
    """Если не влезает ничего — каскад продолжается без ограничения размера,
    и понятную ошибку отдаёт сторож размера исполнителя."""
    sel, _, _ = dl.build_format("video", duration=600, size_cap=True)
    first, *_, = sel.split("/")
    assert "[filesize<?" in first and "[tbr<?" in first
    assert "bv*+ba" in sel.split("/") and sel.endswith("/best")
    assert compiles(sel)
    # Без длительности — запас под звук на максимальную длину, без tbr.
    sel, _, _ = dl.build_format("video", size_cap=True)
    assert "[filesize<?" in sel and "[tbr<?" not in sel


def test_portrait_height_means_short_side():
    """Shorts 1080x1920 на «1080p» урезался до 480x854: фильтр смотрел на
    высоту 1920. Для вертикальных роликов ограничение — по ширине."""
    sel, _, _ = dl.build_format("video", height="1080", portrait=True)
    assert "[width<=?1080]" in sel and "[height<=" not in sel
    info = _yt_info(portrait=True)
    assert dl.is_portrait(info)
    picks = dl.preview_picks(info, YT, ["1080"])
    assert picks["1080"]["res"] == 1080


def test_feed_video_size_matches_auto_pick():
    """Лента «что скачивают» показывает размер ровно того, что выберет «Авто»
    (а не «самый большой 4K» по своей оценке)."""
    info = _yt_info()
    assert dl.video_size(info, YT) == dl.preview_picks(info, YT, [])["auto"]["size"]
    # 4K VP9 313 (16241 кбит/с) + оригинальный Opus 251 (124 кбит/с), 600 с
    assert dl.video_size(info, YT) == int(16241 * 125 * 600) + int(124 * 125 * 600)
