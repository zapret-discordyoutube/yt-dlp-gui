"""Ядро: работа с yt-dlp, менеджер фоновых задач, пресеты форматов."""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import queue
import re
import selectors
import signal
import socket
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

import yt_dlp

import config

# --- Пресеты формата (клиент НЕ может передать произвольные опции yt-dlp) ---
# Всё строится из фиксированного перечня: kind + height + codec.
_HEIGHTS = {"2160", "1440", "1080", "720", "480", "360"}

# Код языка аудиодорожки (дубляж YouTube): en, ru, pt-BR, zh-Hans и т.п.
# Строгая проверка: код уходит внутрь селектора формата yt-dlp, и без неё
# это была бы инъекция произвольного селектора.
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z]{2,8})?$")

# Аудио-кодеки для режима «только аудио».
# 'best' = оставить оригинальную дорожку без перекодирования (без потерь и быстро).
_ACODECS = {
    "best": ("best", "Оригинал"),
    "mp3":  ("mp3",  "MP3"),
    "aac":  ("aac",  "AAC"),
    "opus": ("opus", "Opus"),
}

# Видеокодеки: ключ -> (фильтр формата, контейнер для склейки, подпись).
#
# Тонкости синтаксиса фильтров yt-dlp:
#  * фильтры РЕГИСТРОЗАВИСИМЫ, а сайты отдают и 'avc1.640028', и 'AVC1.640028',
#    поэтому сравниваем регистронезависимым regex;
#  * '(?i)' в начале выражения падает с PatternError — допустима только
#    локальная флаг-группа '(?i:...)';
#  * VP9 приходит и как 'vp9' (HLS), и как 'vp09.xx' (DASH), при этом '^=vp'
#    зацепил бы ещё и vp8 — отсюда '^vp0?9';
#  * значение с символами вне [\w.-] обязано быть в кавычках.
_VCODECS = {
    "auto": ("",                                      "mp4",  "Все кодеки"),
    "h264": (r"[vcodec~='(?i:^(avc1|h264))']",        "mp4",  "H.264"),
    "av1":  (r"[vcodec~='(?i:^av01)']",               "mp4",  "AV1"),
    "vp9":  (r"[vcodec~='(?i:^vp0?9)']",              "webm", "VP9"),
}

# Предпочтения по аудиодорожке внутри видео.
# AAC — это 'mp4a.40.2' (LC) и 'mp4a.40.5' (HE-AAC).
_VIDEO_AUDIO = {
    "auto": ("",                              "Все кодеки"),
    "aac":  (r"[acodec~='(?i:^mp4a)']",        "AAC"),
    "opus": (r"[acodec~='(?i:^opus)']",        "Opus"),
}


def build_format(kind: str, height: str = "auto", acodec: str = "best",
                 vcodec: str = "auto", vaudio: str = "auto",
                 alang: str = "") -> tuple[str, dict, str]:
    """Возвращает (format_selector, extra_opts, label) по безопасному выбору.

    kind='audio': acodec in {best,mp3,aac,opus}
    kind='video': height in {auto,2160..360}, vcodec in {auto,h264,av1,vp9},
                  vaudio in {auto,aac,opus}
    alang: код языка аудиодорожки (дубляж), напр. 'ru'; '' = как в источнике.
    """
    alang = (alang or "").strip()
    if alang and not _LANG_RE.fullmatch(alang):
        raise ValueError("unknown_lang")
    # Фильтр языка для селектора аудио. Без языка — пусто.
    lang_f = f"[language={alang}]" if alang else ""

    if kind == "audio":
        if acodec not in _ACODECS:
            raise ValueError("unknown_acodec")
        pref, label = _ACODECS[acodec]
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": pref}
        if pref != "best":
            pp["preferredquality"] = "192"

        # Берём исходную дорожку в том же кодеке, что и цель: тогда
        # FFmpegExtractAudio просто скопирует поток вместо перекодирования.
        # Без этого выбор AAC приводил к пережатию Opus-дорожки впустую —
        # это и потеря качества, и минуты ожидания на длинном ролике.
        # Для MP3 копирование невозможно: источники его не отдают.
        prefer = {
            "aac":  f"bestaudio{lang_f}[acodec^=mp4a]/bestaudio{lang_f}/bestaudio/best",
            "opus": f"bestaudio{lang_f}[acodec^=opus]/bestaudio{lang_f}/bestaudio/best",
        }.get(acodec, f"bestaudio{lang_f}/bestaudio/best")
        extra: dict = {"postprocessors": [pp]}
        if acodec == "mp3":
            # ffmpeg по умолчанию пишет ID3v2.4, а Windows Explorer показывает
            # встроенную обложку MP3 только у ID3v2.3. Форсируем 2.3.
            extra["postprocessor_args"] = {"default": ["-id3v2_version", "3"]}
        if alang:
            label = f"{label} · {alang}"
        return prefer, extra, label

    if kind == "video":
        if vcodec not in _VCODECS:
            raise ValueError("unknown_vcodec")
        if vaudio not in _VIDEO_AUDIO:
            raise ValueError("unknown_vaudio")
        vfilter, container, vlabel = _VCODECS[vcodec]
        afilter, alabel = _VIDEO_AUDIO[vaudio]

        if height == "auto":
            hfilter, hlabel = "", "Авто"
        elif height in _HEIGHTS:
            # '?' после оператора обязателен: иначе форматы, у которых height
            # неизвестен (None), молча выбрасываются из выборки.
            hfilter, hlabel = f"[height<=?{height}]", f"{height}p"
        else:
            raise ValueError("unknown_height")

        v = f"bestvideo{hfilter}{vfilter}"
        # Каскад запасных вариантов: от точного совпадения к послаблениям.
        # Важны две ступени, которых не хватало:
        #  * «снять фильтр кодека, но оставить склейку» — без неё на сайтах
        #    с раздельными дорожками (DASH/HLS, YouTube выше 1080p) выбор
        #    проваливался мимо склейки к муксованному 360p, и пользователь
        #    молча получал не то, что заказывал, с верной подписью;
        #  * замыкающий вариант обязан уважать ограничение высоты: прежний
        #    голый `best` отдавал 1080p на запрос 360p, то есть кратно
        #    больше трафика и диска, чем просили.
        sel = "/".join(filter(None, [
            # Сначала выбранный язык (+ кодек), затем язык без кодека, затем
            # дорожка как в источнике, затем общие послабления.
            f"{v}+bestaudio{lang_f}{afilter}" if (lang_f or afilter) else None,
            f"{v}+bestaudio{lang_f}" if lang_f else None,
            f"{v}+bestaudio",
            f"bestvideo{hfilter}+bestaudio" if vfilter else None,
            f"best{hfilter}{vfilter}" if vfilter else None,
            f"best{hfilter}",
            "worst" if hfilter else "best",
        ]))
        parts = [hlabel, vlabel] + ([alabel] if vaudio != "auto" else [])
        if alang:
            parts.append(alang)
        return sel, {"merge_output_format": container}, " · ".join(parts)

    raise ValueError("unknown_kind")


_MERGE_CONTAINERS = {"mp4", "webm", "mkv", "mov"}

# Разрешаем произвольный выбор формата, но только как ID (и связку через '+').
# Запрещены скобки, фильтры, '/', пробелы — то есть синтаксис селекторов yt-dlp.
_FORMAT_ID_RE = re.compile(
    r"^[A-Za-z0-9_\-.=:@~]{1,64}(\+[A-Za-z0-9_\-.=:@~]{1,64})?$")

# Регулярка выше пропускала не только идентификаторы, но и КЛЮЧЕВЫЕ СЛОВА
# селектора: 'all' и 'mergeall' заставляли yt-dlp скачать все дорожки сразу,
# а 'best'/'worst' подменяли выбор пользователя. Это и обход белого списка,
# и усиление нагрузки, поэтому такие слова отвергаем явно.
_FORMAT_KEYWORDS = {
    "all", "mergeall", "best", "worst", "b", "w",
    "bestvideo", "worstvideo", "bv", "wv",
    "bestaudio", "worstaudio", "ba", "wa",
    "bestvideo*", "bv*", "b*",
}


def is_youtube(url: str) -> bool:
    """YouTube отдаёт HLS, а download_ranges с HLS зависает — обрезку там
    отключаем. Проверяем хост, а не подстроку, чтобы не ловить чужие домены."""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host in _YT_HOSTS or host.endswith(".youtube.com") or host == "youtu.be"


def parse_timecode(s: str) -> float | None:
    """'90' | '1:30' | '1:02:03' | '1:30.5' -> секунды. Пусто/мусор -> None."""
    s = (s or "").strip()
    if not s:
        return None
    if not re.fullmatch(r"\d{1,3}(:[0-5]?\d){0,2}(\.\d{1,3})?", s):
        return None
    sec = 0.0
    for part in s.split(":"):
        sec = sec * 60 + float(part)
    return sec


def parse_clip(from_s: str, to_s: str) -> tuple[float, float | None] | None:
    """Разобрать отрезок в (start, end) секунд. Пусто -> None. end может быть
    None («до конца»). Инъекция невозможна: возвращаются только числа."""
    start = parse_timecode(from_s)
    end = parse_timecode(to_s)
    if start is None and end is None:
        return None
    if start is None:
        start = 0.0
    if end is not None and end <= start:
        raise ValueError("bad_clip")
    return start, end


def clip_label(start: float, end: float | None) -> str:
    def fmt_t(x):
        x = int(x); h, m, s = x // 3600, (x % 3600) // 60, x % 60
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return f"{fmt_t(start)}–{fmt_t(end) if end is not None else 'конец'}"


def clip_range_opts(start: float, end: float | None) -> dict:
    """Опции yt-dlp для частичного скачивания только отрезка (сайты с прямыми
    форматами: X, Vimeo и т.п.). Для YouTube не годится — там HLS зависает,
    его режем полным скачиванием + ffmpeg (см. jobrunner.trim_file)."""
    rng_end = end if end is not None else float("inf")
    return {
        "download_ranges": yt_dlp.utils.download_range_func(None, [(start, rng_end)]),
        # Точная резка по границам: без этого концы съезжают к ключевым кадрам.
        "force_keyframes_at_cuts": True,
    }


def build_from_format_id(format_id: str, container: str = "auto") -> tuple[str, dict, str]:
    """Точный выбор формата из того, что yt-dlp отдал для этого URL."""
    fid = (format_id or "").strip()
    if not _FORMAT_ID_RE.fullmatch(fid):
        raise ValueError("bad_format_id")
    if any(part.lower() in _FORMAT_KEYWORDS for part in fid.split("+")):
        raise ValueError("bad_format_id")
    extra: dict = {}
    if "+" in fid:
        # Связка video+audio всегда требует склейки. mkv принимает любое
        # сочетание кодеков, поэтому он — безопасный выбор по умолчанию,
        # когда пользователь смешал произвольные дорожки.
        extra["merge_output_format"] = (
            container if container in _MERGE_CONTAINERS else "mkv")
    return fid, extra, f"формат {fid}"


# --- Постобработка: чекбоксы UI -> опции yt-dlp ---
# Строгий белый список булевых флагов. Всё, чего здесь нет, игнорируется:
# клиент по-прежнему не может передать произвольную опцию yt-dlp.
_FEATURE_KEYS = (
    "embed_subs", "embed_thumbnail", "write_thumbnail", "write_description",
    "embed_metadata", "embed_chapters", "sponsorblock", "no_merge",
)
# Флаги, способные породить более одного файла -> итог упаковываем в ZIP.
_MULTIFILE_FEATURES = {"write_thumbnail", "write_description", "no_merge"}

_FEATURE_LABELS = {
    "embed_subs": "субтитры",
    "embed_thumbnail": "обложка",
    "write_thumbnail": "обложка-файл",
    "write_description": "описание",
    "embed_metadata": "метаданные",
    "embed_chapters": "главы",
    "sponsorblock": "без рекламы",
    "no_merge": "без склейки",
}


def parse_features(data) -> dict:
    """Булевы флаги постобработки из запроса, только по белому списку."""
    raw = data if isinstance(data, dict) else {}
    return {k: True for k in _FEATURE_KEYS if raw.get(k)}


# Порядок постпроцессоров (по образцу yt-dlp CLI). Неизвестные ключи —
# в начало, сохраняя исходный порядок (сортировка устойчивая).
_PP_ORDER = ("FFmpegExtractAudio", "SponsorBlock", "ModifyChapters",
             "FFmpegMetadata", "FFmpegEmbedSubtitle", "EmbedThumbnail")


def apply_features(fmt: str, extra: dict, features: dict,
                   kind: str) -> tuple[str, dict, str, bool]:
    """Дополнить (fmt, extra) опциями постобработки.

    Возвращает (fmt, extra, подпись, bundle). extra уже может нести
    postprocessors (извлечение аудио) и merge_output_format — их дополняем,
    не затирая. bundle=True, если ожидается несколько файлов (нужен ZIP).
    """
    if not features:
        return fmt, extra, "", False
    extra = dict(extra)
    pps = list(extra.get("postprocessors") or [])
    is_audio = (kind == "audio")

    # Субтитры вшиваем только в видео: в аудиоконтейнер их не положить.
    if features.get("embed_subs") and not is_audio:
        extra["writesubtitles"] = True
        extra["subtitleslangs"] = ["all", "-live_chat"]
        pps.append({"key": "FFmpegEmbedSubtitle",
                    "already_have_subtitle": False})

    if features.get("embed_thumbnail"):
        extra["writethumbnail"] = True
        # already_have_thumbnail=True -> файл обложки НЕ удаляется после
        # встраивания; ставим его, только когда обложку просят и отдельным
        # файлом тоже. Иначе обложка удаляется и остаётся один файл.
        pps.append({"key": "EmbedThumbnail",
                    "already_have_thumbnail": bool(features.get("write_thumbnail"))})

    # Метаданные и главы — один проход FFmpegMetadata.
    if features.get("embed_metadata") or features.get("embed_chapters"):
        pps.append({
            "key": "FFmpegMetadata",
            "add_metadata": bool(features.get("embed_metadata")),
            "add_chapters": True if features.get("embed_chapters") else None,
        })

    # SponsorBlock: сначала получить сегменты, затем вырезать. Порядок в
    # списке важен — SponsorBlock должен идти раньше ModifyChapters.
    if features.get("sponsorblock"):
        pps.append({"key": "SponsorBlock", "categories": ["sponsor"],
                    "api": "https://sponsor.ajay.app"})
        pps.append({"key": "ModifyChapters",
                    "remove_sponsor_segments": ["sponsor"]})

    # Отдельные файлы обложки/описания.
    if features.get("write_thumbnail"):
        extra["writethumbnail"] = True
    if features.get("write_description"):
        extra["writedescription"] = True

    # Не объединять аудио/видео: осмысленно для явной связки a+b без каскада
    # (выбор конкретного формата во «Все форматы»). Меняем '+' на ',' — yt-dlp
    # скачает дорожки раздельно, склейку убираем. Пресетные каскады с '/'
    # не трогаем: там всегда есть запасные варианты, и запятая их сломала бы.
    if features.get("no_merge") and "+" in fmt and "/" not in fmt:
        fmt = fmt.replace("+", ",")
        extra.pop("merge_output_format", None)

    if pps:
        # Порядок как у самого yt-dlp: обложка — последней. Если вставить её
        # раньше метаданных, ffmpeg перепаковывает Opus/M4A с огромным тегом
        # картинки и падает («Conversion failed!» на аудио «Оригинал»).
        order = {k: n for n, k in enumerate(_PP_ORDER)}
        pps.sort(key=lambda pp: order.get(pp.get("key"), -1))
        extra["postprocessors"] = pps
    label = ", ".join(_FEATURE_LABELS[k] for k in _FEATURE_KEYS
                      if features.get(k))
    bundle = any(features.get(k) for k in _MULTIFILE_FEATURES)
    return fmt, extra, label, bundle


# Параметры, не влияющие на то, какой это ролик: метки переходов, тайм-коды,
# идентификаторы сессий. Их нельзя публиковать и нельзя учитывать при
# сравнении ссылок.
_JUNK_QUERY = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "feature", "fbclid", "gclid", "yclid", "si", "pp", "t", "start",
    "ref", "ref_src", "referrer", "from", "app", "_r", "rtc", "list", "index",
    # Похожее на секреты выбрасываем всегда: лучше потерять работоспособность
    # ссылки в ленте, чем опубликовать чужой токен доступа.
    "token", "access_token", "auth", "authorization", "key", "api_key",
    "apikey", "session", "sessionid", "sid", "signature", "sig", "hash",
    "password", "pwd", "secret",
}

_YT_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_VK_HOSTS = {"vk.com", "vk.ru", "m.vk.com", "vkvideo.ru"}


def canonical_url(url: str) -> str:
    """Привести ссылку к единому виду для публичной ленты.

    Решает две задачи сразу:
    * один ролик — одна запись. Без этого `youtu.be/X`, `youtube.com/watch?v=X`
      и та же ссылка с тайм-кодом считались тремя разными роликами, и счётчик
      популярности размазывался;
    * из ссылки убирается всё лишнее. Query-строка может нести метки перехода
      и идентификаторы сессии, а лента публичная — публиковать их нельзя.
    """
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return url
    host = (p.hostname or "").lower().removeprefix("www.")
    path = p.path.rstrip("/")

    if host in _YT_HOSTS:
        vid = None
        if host == "youtu.be":
            vid = path.lstrip("/").split("/")[0] or None
        elif path == "/watch":
            vid = parse_qs(p.query).get("v", [None])[0]
        else:
            m = re.match(r"^/(?:shorts|embed|live|v)/([\w-]+)", path)
            if m:
                vid = m.group(1)
        if vid and re.fullmatch(r"[\w-]{6,20}", vid):
            return f"https://www.youtube.com/watch?v={vid}"

    if host in _VK_HOSTS:
        m = re.match(r"^/(?:video|clip)(-?\d+_\d+)", path)
        if m:
            return f"https://vkvideo.ru/video{m.group(1)}"

    # Общий случай: приводим регистр хоста и порядок параметров, но
    # сохраняем то, что отличает один ресурс от другого.
    #
    # Раньше здесь терялись порт, схема и фрагмент. Из-за этого
    # example.com:8080/v/1 и example.com:9090/v/1 считались одной записью,
    # http-only сайт получал нерабочую https-ссылку, а одностраничные
    # приложения, где идентификатор живёт в якоре (site/#/video/111),
    # схлопывались все в одну строку.
    keep = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
                  if k.lower() not in _JUNK_QUERY)
    query = urlencode(keep)

    netloc = host
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]"          # IPv6-литерал без скобок нечитаем
    if p.port:
        netloc = f"{netloc}:{p.port}"
    scheme = p.scheme if p.scheme in ("http", "https") else "https"
    return urlunparse((scheme, netloc, path or "/", "", query, p.fragment))


def _is_public_host(host: str) -> bool:
    """Резолвим имя и требуем, чтобы ВСЕ адреса были публичными.

    Без этого генерик-экстрактор yt-dlp сходит по любому http(s)-адресу:
    на метаданные облака (169.254.169.254), на localhost и во внутреннюю
    сеть хоста. Для публичного сервиса это SSRF.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def validate_url(url: str) -> str:
    url = (url or "").strip()
    if not url or len(url) > 2048:
        raise ValueError("empty_or_too_long")
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("bad_scheme")
    host = p.hostname
    if not host:
        raise ValueError("bad_scheme")
    if not _is_public_host(host):
        raise ValueError("private_host")
    return url


# --- Извлечение метаданных без скачивания ---
def _best_image(entry: dict) -> str | None:
    """Лучший URL картинки записи: yt-dlp сортирует thumbnails от худшей к
    лучшей, берём последнюю с http-адресом."""
    for t in reversed(entry.get("thumbnails") or []):
        u = t.get("url")
        if u and u.startswith(("http://", "https://")):
            return u
    u = entry.get("display_url") or entry.get("url")
    return u if u and str(u).startswith(("http://", "https://")) else None


def collect_images(info: dict) -> list[str]:
    """URL картинок из поста без видео (галереи Instagram/Twitter).

    Берём только записи без видеоформатов, чтобы не хватать превью настоящих
    роликов. Возвращаем уникальные адреса в исходном порядке.
    """
    entries = info.get("entries")
    src = entries if entries is not None else [info]
    urls, seen = [], set()
    for e in src or []:
        if not isinstance(e, dict) or (e.get("formats") or []):
            continue
        u = _best_image(e)
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def _img_ext(content_type: str | None, url: str) -> str:
    """Расширение картинки по Content-Type, с запасным разбором URL."""
    ct = (content_type or "").split(";")[0].strip().lower()
    by_ct = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
             "image/webp": "webp", "image/gif": "gif", "image/heic": "heic"}
    if ct in by_ct:
        return by_ct[ct]
    m = re.search(r"\.(jpe?g|png|webp|gif|heic)(?:[?&]|$)", url, re.I)
    return (m.group(1).lower().replace("jpeg", "jpg")) if m else "jpg"


# Признаки того, что контент забанен/недоступен ИМЕННО с этого IP или в
# регионе — тогда осмысленно повторить через запасной egress-прокси.
# Только явный бан по IP/региону. 403 сюда НЕ входит: у YouTube он часто
# транзиентный, и повтор через медленный egress лишь замедлял бы загрузку.
_BAN_SIGNATURES = (
    "your ip address is blocked", "ip address is blocked",
    "not available in your country", "not available from your location",
    "not available in your region", "geo restricted", "geo-restricted",
    "blocked it in your country", "this content is not available in your",
    "video is not available from your",
    # YouTube: «Sign in to confirm you're not a bot» — IP сервера попал под
    # антибот-проверку; с egress-узла тот же ролик открывается.
    "not a bot",
)


def _is_ban_error(msg: str) -> bool:
    m = (msg or "").lower()
    return any(s in m for s in _BAN_SIGNATURES)


def is_youtube_list(url: str) -> bool:
    """Ссылка на плейлист или канал YouTube (а не на конкретный ролик)."""
    if not is_youtube(url):
        return False
    p = urlparse(url)
    q = parse_qs(p.query)
    if "v" in q or p.path.startswith(("/watch", "/shorts/", "/live/")) or \
            (p.hostname or "").endswith("youtu.be"):
        return False
    return "list" in q or p.path.startswith(("/playlist", "/@", "/channel/", "/c/", "/user/"))


PLAYLIST_MAX = 200     # сколько роликов списка показываем


def probe_list(url: str) -> dict:
    """Плейлист/канал: только список роликов, без разбора каждого (быстро).
    Раньше yt-dlp разбирал ВСЕ ролики списка — минуты «Проверяем…», а
    потом ошибка «нет форматов»."""
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extract_flat": "in_playlist", "playlistend": PLAYLIST_MAX,
        "socket_timeout": 30, "logger": _QuietLogger(),
        **({"source_address": config.YT_SOURCE_IP} if config.YT_SOURCE_IP else {}),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.sanitize_info(ydl.extract_info(url, download=False))
    entries = []
    for e in info.get("entries") or []:
        if not isinstance(e, dict):
            continue
        vid = e.get("id")
        u = e.get("url") or e.get("webpage_url")
        if vid and not (u or "").startswith("http"):
            u = f"https://www.youtube.com/watch?v={vid}"
        if not u or not u.startswith("https://"):
            continue
        thumbs = e.get("thumbnails") or []
        entries.append({
            "url": u,
            "title": (e.get("title") or "Без названия")[:200],
            "duration": e.get("duration"),
            "thumbnail": (thumbs[-1].get("url") if thumbs else None),
        })
    return {
        "is_playlist": True,
        "title": info.get("title") or "Плейлист",
        "uploader": info.get("uploader") or info.get("channel"),
        "count": info.get("playlist_count") or len(entries),
        "entries": entries,
        "webpage_url": info.get("webpage_url") or url,
    }


def _probe_extract(url: str, proxy: str | None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # Посты с одними картинками (Instagram/Twitter) иначе валят extract_info
        # ошибкой «No video formats found»; так мы их разбираем и предлагаем фото.
        "ignore_no_formats_error": True,
        "noplaylist": True,
        # 30с, а не 20: некоторые сайты (pornhub и пр.) отвечают медленно из
        # дата-центра и делают несколько запросов подряд — на 20с не укладывались.
        "socket_timeout": 30,
        # без своего логгера yt-dlp печатает ошибки со ссылкой в stderr
        "logger": _QuietLogger(),
        **({"proxy": proxy} if proxy else {}),
        # YouTube напрямую — со своего IP (config.YT_SOURCE_IP).
        **({"source_address": config.YT_SOURCE_IP}
           if not proxy and config.YT_SOURCE_IP and is_youtube(url) else {}),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.sanitize_info(ydl.extract_info(url, download=False))


# --- Кэш разбора: загрузка берёт готовый результат «Проверить» ------------
# Без него загрузка разбирала ролик заново (~2 с и лишний запрос к YouTube,
# а частые запросы с нашего IP YouTube принимает за бота). Только память
# процесса, не диск: ~130 КБ на ролик (автосубтитры вырезаны), не больше
# INFO_CACHE_MAX записей, каждая живёт INFO_CACHE_SEC. Кладёт сюда только
# сервер — от клиента разбор не принимается.
INFO_CACHE_SEC = 600
INFO_CACHE_MAX = 50
_info_cache: "OrderedDict[str, tuple[float, bool, dict]]" = OrderedDict()
_info_lock = threading.Lock()
# Тяжёлые поля, не нужные для скачивания (автосубтитры — сотни языков).
_INFO_DROP = ("automatic_captions", "heatmap")


def _cache_key(url: str) -> str:
    try:
        return canonical_url(url)
    except Exception:                      # noqa: BLE001
        return url


def cache_info(urls, via_egress: bool, info: dict) -> None:
    slim = {k: v for k, v in info.items() if k not in _INFO_DROP}
    now = time.time()
    with _info_lock:
        for u in {_cache_key(u) for u in urls if u}:
            _info_cache[u] = (now, via_egress, slim)
            _info_cache.move_to_end(u)
        while len(_info_cache) > INFO_CACHE_MAX:
            _info_cache.popitem(last=False)


def cached_info(url: str, via_egress: bool) -> dict | None:
    """Свежий разбор этой ссылки, сделанный тем же путём (напрямую/egress):
    ссылки на файлы привязаны к IP, с которого их получили."""
    with _info_lock:
        hit = _info_cache.get(_cache_key(url))
    if not hit:
        return None
    at, via, info = hit
    if time.time() - at > INFO_CACHE_SEC or via != via_egress:
        return None
    return info


def probe(url: str) -> dict:
    if is_youtube_list(url):
        return probe_list(url)
    # Прямой доступ; при бане (IP-блок/гео/403) — повтор через egress-прокси.
    via_egress = False
    try:
        info = _probe_extract(url, config.PROXY or None)
    except yt_dlp.utils.DownloadError as e:
        if config.EGRESS_PROXY and _is_ban_error(str(e)):
            info = _probe_extract(url, config.EGRESS_PROXY)
            via_egress = True
        else:
            raise
    # Ролик без единого формата — часто тот же бан, только молчаливый:
    # при ignore_no_formats_error YouTube-антибот («Sign in to confirm you're
    # not a bot») не бросает ошибку, а отдаёт страницу без форматов.
    if (not via_egress and config.EGRESS_PROXY and not info.get("formats")
            and (info.get("duration") or is_youtube(url))):
        try:
            alt = _probe_extract(url, config.EGRESS_PROXY)
            if alt.get("formats"):
                info, via_egress = alt, True
        except yt_dlp.utils.DownloadError:
            pass

    # Прямые эфиры: идущий можно записывать, будущий — ещё нельзя.
    live_status = info.get("live_status")
    is_live = bool(info.get("is_live")) or live_status == "is_live"
    # Готовый разбор — загрузке (кроме эфира: его ссылки живут недолго).
    if info.get("formats") and not is_live:
        cache_info((url, info.get("webpage_url")), via_egress, info)
    if live_status == "is_upcoming":
        raise ValueError("upcoming")

    duration = info.get("duration")
    # У идущего эфира длительности нет — ограничение по времени к нему
    # неприменимо (его границу задаёт размер файла и ручная остановка).
    if duration and not is_live and duration > config.MAX_DURATION_SEC:
        raise ValueError("too_long")

    # доступные высоты видео -> для простого селектора качества
    heights = set()
    for f in info.get("formats") or []:
        h = f.get("height")
        # None = кодек неизвестен, но видео у формата есть; отбрасываем
        # только явное "none" (см. пояснение ниже про два разных значения).
        if h and f.get("vcodec") != "none":
            heights.add(int(h))
    avail = [str(h) for h in sorted(heights, reverse=True) if str(h) in _HEIGHTS]

    # полный список форматов -> продвинутый режим «всё что есть»
    formats = []
    for f in info.get("formats") or []:
        fid = f.get("format_id")
        if not fid or f.get("format_note") == "storyboard":
            continue
        # Различаем два разных значения: строка "none" означает, что дорожки
        # точно нет, а None — что кодек просто неизвестен. Многие экстракторы
        # (кроме YouTube) кодеки не сообщают, и прежняя запись
        # `f.get("vcodec") or "none"` превращала неизвестный в отсутствующий,
        # из-за чего выбрасывались ВСЕ форматы таких сайтов.
        v, a = f.get("vcodec"), f.get("acodec")
        if v == "none" and a == "none":
            continue
        has_v, has_a = v != "none", a != "none"
        kind = "both" if (has_v and has_a) else ("video" if has_v else "audio")
        formats.append({
            "format_id": fid,
            "kind": kind,
            "ext": f.get("ext"),
            "height": f.get("height"),
            "fps": f.get("fps"),
            "resolution": f.get("resolution") or (
                f"{f.get('width')}x{f.get('height')}" if f.get("height") else None),
            # v/a может быть None (кодек неизвестен) — тогда split() упал бы
            "vcodec": v.split(".")[0] if v and v != "none" else None,
            "acodec": a.split(".")[0] if a and a != "none" else None,
            "abr": f.get("abr"),
            "tbr": f.get("tbr"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "note": f.get("format_note"),
        })
    # крупные/качественные сверху
    formats.sort(key=lambda x: (x["height"] or 0, x["tbr"] or 0), reverse=True)

    # Пост без видео, но с картинками -> предлагаем скачать фото (галерея).
    # Но ролик (есть длительность, или это YouTube) галереей не бывает: у него
    # форматов нет по другой причине — чаще всего возрастное ограничение, и
    # раньше такой ролик предлагалось «скачать фото» — его обложку.
    images = collect_images(info)
    is_video_page = bool(info.get("duration")) or is_youtube(url)
    is_gallery = bool(images) and not formats and not is_video_page
    if not formats and is_video_page:
        if (info.get("age_limit") or 0) >= 18:
            raise ValueError("age_restricted")
        raise ValueError("no_formats")

    # Языки аудиодорожек (дубляж YouTube). Показываем выбор только если их >1.
    audio_langs, seen_langs = [], set()
    for f in info.get("formats") or []:
        if f.get("acodec") in (None, "none") or f.get("vcodec") not in (None, "none"):
            continue
        code = f.get("language")
        if not code or code in seen_langs or not _LANG_RE.fullmatch(code):
            continue
        seen_langs.add(code)
        note = f.get("format_note") or ""
        # «Russian, low» -> «Russian»; «English original (default), low» -> ...
        lbl = note.split(",")[0].strip() or code
        audio_langs.append({"code": code, "label": lbl,
                            "original": "original" in note.lower()})
    if len(audio_langs) < 2:
        audio_langs = []
    else:
        # Оригинал наверх, остальные — по алфавиту.
        audio_langs.sort(key=lambda x: (not x["original"], x["label"].lower()))

    return {
        "formats": formats,
        "id": info.get("id"),
        "title": info.get("title") or "Без названия",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": duration,
        "thumbnail": info.get("thumbnail") or (images[0] if images else None),
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "heights": avail,
        "is_live": is_live,
        "is_gallery": is_gallery,
        "image_count": len(images),
        "via_egress": via_egress,
        "audio_langs": audio_langs,
    }


# --- Модель задачи ---
# Незавершённые статусы: задача ещё может дать файл, её файлы трогать нельзя.
ACTIVE_STATUSES = ("queued", "preparing", "downloading", "processing")


@dataclass
class Task:
    id: str
    url: str
    title: str
    fmt: str                     # селектор формата (уже провалидирован)
    extra: dict                  # доп. опции yt-dlp (merge/postprocessors)
    label: str                   # человекочитаемая подпись выбора
    thumbnail: str | None = None
    bundle: bool = False         # упаковать несколько файлов в один ZIP
    is_live: bool = False        # запись идущего эфира: стоп сохраняет записанное
    images_mode: bool = False    # скачать фото из поста-галереи (без видео)
    use_egress: bool = False     # качать через запасной egress-прокси (бан)
    clip: tuple | None = None    # (start,end) для пост-обрезки ffmpeg (YouTube)
    playlist: int = 0            # >0 — плейлист архивом: сколько роликов качать
    status: str = "queued"       # queued|preparing|downloading|processing|finished|error|cancelled
    phase: str | None = None     # до первых байт: connect | bypass (для подписи в UI)
    percent: float | None = None
    speed: float | None = None
    eta: int | None = None
    total_bytes: int | None = None
    filename: str | None = None      # безопасный basename на диске: <id>.<ext>
    display_name: str | None = None  # красивое имя для пользователя (с кириллицей)
    filesize: int | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    served_at: float | None = None      # когда пользователь забрал файл
    completed: bool = False             # on_complete уже вызывали
    # Метрики производительности для статистики (тайминги, движок). Без
    # ссылок: домен и числа, см. stats.record_perf.
    metrics: dict = field(default_factory=dict)
    cancel: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "label": self.label,
            "thumbnail": self.thumbnail,
            "is_live": self.is_live,
            "status": self.status,
            "phase": self.phase,
            "percent": self.percent,
            "speed": self.speed,
            "eta": self.eta,
            "filename": self.display_name,
            "filesize": self.filesize,
            "error": self.error,
            "created_at": self.created_at,
        }


class DownloadManager:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        # Вызывается при завершении задачи (любым исходом). Нужен для
        # обезличенных счётчиков; сам менеджер о статистике ничего не знает.
        self.on_complete = None
        self.lock = threading.Lock()
        # Очередь с постоянным пулом вместо потока на задачу.
        # Раньше каждая задача поднимала свой поток, который до получаса ждал
        # на семафоре: шесть запусков в минуту с одного адреса давали около
        # 180 висящих потоков при TasksMax=256, и сервис ложился с двух IP.
        # Время последнего УСПЕШНОГО прохода уборщика. Нужно для /readyz:
        # если уборщик тихо умер, файлы перестанут удаляться, диск заполнится,
        # и без этого признака поломка будет невидима.
        self.last_janitor_ok = time.time()
        self.queue: queue.Queue = queue.Queue(maxsize=config.QUEUE_MAX)
        for _ in range(config.MAX_CONCURRENT_DOWNLOADS):
            threading.Thread(target=self._worker, daemon=True).start()
        config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self._sweep_orphans()
        self._start_janitor()

    def _sweep_orphans(self) -> None:
        """Подмести файлы, оставшиеся от прошлого запуска.

        Состояние задач живёт только в памяти процесса, поэтому после
        перезапуска ни один файл в каталоге никому не принадлежит —
        забрать его всё равно уже нельзя. Без этого мусор копится до
        срабатывания TTL.
        """
        freed = 0
        for p in config.DOWNLOAD_DIR.iterdir():
            if p.is_file() and p.name != ".gitkeep":
                try:
                    freed += p.stat().st_size
                    p.unlink()
                except OSError:
                    pass
        if freed:
            print(f"[janitor] подметено осиротевших файлов: "
                  f"{freed / 1048576:.0f} МБ", flush=True)

    # ---- очистка старых файлов/задач и контроль квоты ----
    def _dir_size_mb(self) -> float:
        total = 0
        for p in config.DOWNLOAD_DIR.glob("**/*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total / (1024 * 1024)

    def _janitor_pass(self) -> None:
        now = time.time()

        # 1) файлы, которые пользователь уже забрал -> удалить после grace-периода
        if config.DELETE_AFTER_SERVE:
            with self.lock:
                served = [t for t in self.tasks.values()
                          if t.served_at and t.filename
                          and now - t.served_at >= config.SERVED_GRACE_SEC]
            for t in served:
                p = config.DOWNLOAD_DIR / t.filename
                try:
                    if p.is_file():
                        p.unlink()
                except OSError:
                    pass
                t.status = "served"
                t.filename = None

        # 2) страховочное удаление файлов, которые так и не забрали
        # Файлы задач, которые прямо сейчас качаются, трогать нельзя:
        # загрузка длиннее FILE_TTL теряла из-под yt-dlp уже скачанную
        # дорожку и падала после часов работы.
        with self.lock:
            live = {t.id for t in self.tasks.values()
                    if t.status in ACTIVE_STATUSES}

        cutoff = now - config.FILE_TTL_MINUTES * 60
        for p in config.DOWNLOAD_DIR.iterdir():
            if any(p.name.startswith(tid) for tid in live):
                continue
            # .gitkeep исключён и при подметании на старте: без этого
            # маркер каталога удалялся по TTL, и каталог переставал
            # восстанавливаться из репозитория.
            if p.is_file() and p.name != ".gitkeep":
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    pass
        # снять старые карточки задач (только в памяти)
        tcut = now - config.TASK_TTL_MINUTES * 60
        stuck: list[Task] = []
        with self.lock:
            for t in list(self.tasks.values()):
                if t.created_at >= tcut:
                    continue
                if t.status in ("finished", "error", "cancelled", "served"):
                    self.tasks.pop(t.id, None)
                elif t.created_at < now - config.STUCK_TASK_SEC:
                    # Зависшая задача (умер воркер, встал постпроцессинг)
                    # раньше не выселялась НИКОГДА: карточки копились до
                    # TASKS_MAX, и сервис отвечал вечным «перегружен».
                    was = t.status
                    # Флаг отмены обязателен: без него воркер продолжал
                    # работать, вечно занимал слот из трёх, а уборщик на
                    # следующем проходе сносил файлы у него из-под рук —
                    # и по завершении ссылка всё равно уходила в ленту.
                    t.cancel.set()
                    t.error = "Задача не завершилась и была снята"
                    t.status = "error"
                    stuck.append(t)
                    logging.warning("снята зависшая задача, была в статусе %s", was)

        # on_complete зовём ВНЕ блокировки: он ходит в БД.
        for t in stuck:
            if self.on_complete:
                try:
                    self.on_complete(t)
                except Exception:
                    logging.exception("не удалось учесть снятую задачу")

    def _start_janitor(self) -> None:
        def loop():
            fails = 0
            while True:
                time.sleep(config.JANITOR_INTERVAL_SEC)
                try:
                    self._janitor_pass()
                    fails = 0
                    self.last_janitor_ok = time.time()
                except Exception:
                    # Молчать нельзя: если уборщик умер (например, каталог
                    # стал недоступен на запись), файлы перестают удаляться,
                    # диск заполняется — и в журнале при этом пусто.
                    fails += 1
                    if fails in (1, 10) or fails % 100 == 0:
                        logging.exception("уборщик не смог отработать "
                                          "(подряд неудач: %d)", fails)
        threading.Thread(target=loop, daemon=True).start()

    def quota_exceeded(self) -> bool:
        """Своя квота ИЛИ нехватка места на разделе хоста."""
        if self._dir_size_mb() >= config.DISK_QUOTA_MB:
            return True
        try:
            free_mb = shutil.disk_usage(config.DOWNLOAD_DIR).free / 1048576
        except OSError:
            return False
        return free_mb < config.MIN_FREE_DISK_MB

    # ---- запуск задачи ----
    def create(self, url: str, fmt: str, extra: dict, label: str,
               title: str, thumbnail: str | None, bundle: bool = False,
               is_live: bool = False, images_mode: bool = False,
               use_egress: bool = False, clip: tuple | None = None,
               playlist: int = 0) -> Task:
        # Идентификатор — единственное, что защищает чужой файл от выдачи,
        # поэтому берём его целиком, а не первые 12 символов.
        task = Task(id=uuid.uuid4().hex, url=url, title=title,
                    fmt=fmt, extra=extra, label=label, thumbnail=thumbnail,
                    bundle=bundle, is_live=is_live, images_mode=images_mode,
                    use_egress=use_egress, clip=clip, playlist=playlist)
        with self.lock:
            if len(self.tasks) >= config.TASKS_MAX:
                raise Overloaded("too_many_tasks")
            self.tasks[task.id] = task
        try:
            self.queue.put_nowait(task)
        except queue.Full:
            with self.lock:
                self.tasks.pop(task.id, None)
            raise Overloaded("queue_full") from None
        return task

    def _worker(self) -> None:
        while True:
            try:
                task = self.queue.get()
            except BaseException:                  # noqa: BLE001
                logging.exception("воркер не смог взять задачу")
                time.sleep(1)
                continue
            try:
                self._run(task)
            except BaseException:                  # noqa: BLE001
                # Именно BaseException: при Exception поток воркера умирал
                # молча, пул усыхал до нуля, и сервис отвечал вечным
                # «Сервис перегружен» до перезапуска.
                logging.exception("сбой воркера загрузки")
                try:
                    task.status, task.error = "error", "Внутренняя ошибка"
                    if self.on_complete:
                        self.on_complete(task)
                except BaseException:              # noqa: BLE001
                    pass
            finally:
                self.queue.task_done()

    def mark_served(self, task: "Task") -> bool:
        """Отметить первую выдачу файла. True — если это именно первая.

        Проверка и установка обязаны быть атомарны: браузеры и менеджеры
        загрузок шлют параллельные Range-запросы, и раздельные проверка с
        присваиванием кратно завышали публичную статистику.
        """
        with self.lock:
            if task.served_at is not None:
                return False
            task.served_at = time.time()
            return True

    def pending(self) -> int:
        return self.queue.qsize()

    def health(self) -> dict:
        """Фактическое состояние: диск, очередь, задачи, уборщик."""
        try:
            usage = shutil.disk_usage(config.DOWNLOAD_DIR)
            free_mb = round(usage.free / 1048576)
        except OSError:
            free_mb = None
        with self.lock:
            tasks = list(self.tasks.values())
        active = sum(1 for t in tasks if t.status in ("preparing", "downloading", "processing"))
        return {
            "free_disk_mb": free_mb,
            "downloads_mb": round(self._dir_size_mb(), 1),
            "tasks_total": len(tasks),
            "tasks_active": active,
            "queue_pending": self.pending(),
            "janitor_age_sec": round(time.time() - self.last_janitor_ok),
        }

    def get(self, tid: str) -> Task | None:
        with self.lock:
            return self.tasks.get(tid)

    def list(self) -> list[dict]:
        with self.lock:
            return [t.public() for t in
                    sorted(self.tasks.values(), key=lambda t: t.created_at, reverse=True)]

    def cancel(self, tid: str) -> bool:
        t = self.get(tid)
        if t and t.status in ACTIVE_STATUSES:
            t.cancel.set()
            # Обычная загрузка отменяется сразу: пользователь видит «Отменено»
            # мгновенно, а процесс добивает сторож в _execute. Эфир — нет:
            # для него «стоп» значит «сохранить записанное», итог будет позже.
            if not t.is_live:
                t.status = "cancelled"
            return True
        return False

    # ---- выполнение задачи в отдельном процессе ----
    def _task_proxy(self, task: Task) -> str | None:
        """Прокси для задачи: запасной egress при бане, иначе основной PROXY.
        YouTube через egress идёт через пул туннелей (racefd раздаёт их
        потокам): один туннель провайдер режет до ~300 КБ/с."""
        if task.use_egress:
            if config.EGRESS_POOL and is_youtube(task.url):
                return config.EGRESS_POOL[0]
            if config.EGRESS_PROXY:
                return config.EGRESS_PROXY
        return config.PROXY or None

    def _spec(self, task: Task) -> dict:
        return {
            "id": task.id, "url": task.url, "title": task.title,
            "fmt": task.fmt, "extra": task.extra, "is_live": task.is_live,
            "images_mode": task.images_mode, "bundle": task.bundle,
            "proxy": self._task_proxy(task),
            "clip": list(task.clip) if task.clip else None,
            "playlist": task.playlist,
            # Готовый разбор из «Проверить»: без повторного разбора ролика.
            "info": (None if task.is_live or task.images_mode
                     else cached_info(task.url, task.use_egress)),
        }

    def _spawn(self, task: Task) -> subprocess.Popen:
        # Своя группа процессов: при жёсткой остановке сигнал получает и
        # ffmpeg, запущенный yt-dlp, а не только сам исполнитель.
        return subprocess.Popen(
            [sys.executable, "-m", "jobrunner"],
            cwd=str(config.BASE_DIR),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
            start_new_session=True, text=True, encoding="utf-8", bufsize=1,
        )

    @staticmethod
    def _kill_group(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _apply_event(self, task: Task, ev: dict) -> dict | None:
        """Применить событие исполнителя к задаче. Возвращает итог (done)."""
        kind = ev.get("ev")
        now = time.monotonic()
        tm = task.metrics
        if task.cancel.is_set() and not task.is_live:
            # Отменённая задача: запоздалые события процесса статус не меняют.
            return ev if kind == "done" else None
        if kind == "status" and ev.get("status") in ("preparing", "downloading",
                                                     "processing"):
            task.status = ev["status"]
            if task.status == "downloading":
                tm.setdefault("_first_byte", now)
                tm["_dl_end"] = now
        elif kind == "phase" and ev.get("phase") in ("connect", "bypass"):
            task.phase = ev["phase"]
        elif kind == "progress":
            if (ev.get("percent") or 0) > 0:
                task.phase = None                  # пошли байты — фаза не нужна
            if task.status == "downloading":
                tm.setdefault("_first_byte", now)
                tm["_dl_end"] = now
            for key in ("percent", "speed", "eta"):
                setattr(task, key, ev.get(key))
            task.total_bytes = ev.get("total")
        elif kind == "done":
            return ev
        return None

    def _execute(self, task: Task) -> dict:
        """Запустить исполнителя и сопровождать его до конца.

        Сторожит три вещи: отмену пользователем (SIGTERM, затем SIGKILL
        группы), отсутствие прогресса дольше STALL_SEC (источник завис) и
        общее время обработки. Возвращает итоговое событие done.
        """
        task.metrics["_spawned"] = time.monotonic()
        proc = self._spawn(task)
        try:
            proc.stdin.write(json.dumps(self._spec(task), ensure_ascii=False) + "\n")
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        last_activity = time.monotonic()
        term_sent_at: float | None = None
        killed_reason: str | None = None
        result: dict | None = None
        try:
            while True:
                if sel.select(timeout=0.2):
                    line = proc.stdout.readline()
                    if not line:
                        break                                  # EOF: процесс вышел
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    last_activity = time.monotonic()
                    done = self._apply_event(task, ev)
                    if done is not None:
                        result = done
                now = time.monotonic()
                # Отмена: сначала просим (эфир успеет сохранить записанное),
                # не послушался — останавливаем принудительно.
                if task.cancel.is_set() and term_sent_at is None:
                    # Эфир просим остановиться мягко — он сохранит записанное.
                    # Обычную загрузку останавливаем сразу: ждать нечего, файлы
                    # всё равно удаляются.
                    self._kill_group(proc, signal.SIGTERM if task.is_live else signal.SIGKILL)
                    term_sent_at = now
                elif term_sent_at is not None and now - term_sent_at > config.CANCEL_GRACE_SEC:
                    self._kill_group(proc, signal.SIGKILL)
                    killed_reason = killed_reason or "cancel"
                # Сторож зависаний. Пока идёт склейка/перекодирование, событий
                # нет законно, поэтому у этой фазы свой, больший предел.
                limit = (config.PROCESSING_TIMEOUT_SEC if task.status == "processing"
                         else config.STALL_SEC)
                if term_sent_at is None and now - last_activity > limit:
                    task.error = ("Источник перестал отдавать данные — "
                                  "попробуйте ещё раз позже")
                    task.cancel.set()
                    killed_reason = "stall"
        finally:
            sel.close()
            try:
                proc.wait(timeout=config.CANCEL_GRACE_SEC)
            except subprocess.TimeoutExpired:
                self._kill_group(proc, signal.SIGKILL)
                proc.wait()
            # Процесс мог выйти, а его ffmpeg — ещё нет.
            self._kill_group(proc, signal.SIGKILL)
            proc.stdout.close()

        if result is not None:
            return result
        if killed_reason == "stall":
            return {"status": "error", "error": task.error}
        if task.cancel.is_set():
            # Остановлен без итога: для эфира сохраняем записанное.
            if task.is_live and not task.error:
                saved = finalize_partial(task.id)
                if saved:
                    return {"status": "finished", "filename": os.path.basename(saved)}
            return {"status": "error", "error": task.error} if task.error \
                else {"status": "cancelled"}
        logging.error("исполнитель задачи завершился без итога (код %s, %s)",
                      proc.returncode, _host_of(task.url))
        return {"status": "error", "error": "Внутренняя ошибка, попробуйте ещё раз"}

    def _run(self, task: Task) -> None:
        task.metrics["queue_ms"] = int((time.time() - task.created_at) * 1000)
        try:
            if task.cancel.is_set():
                task.status = "cancelled"
                return
            result = self._execute(task)
            task.metrics.update(result.get("metrics") or {})
            status = result.get("status")
            if task.playlist and result.get("pl_done") is not None:
                task.label = f"{task.label} · скачано {result['pl_done']} из {task.playlist}"
            # Отмена могла прийти уже после последнего события. Публиковать
            # такую загрузку нельзя: пользователь нажал «Отмена» и получил
            # подтверждение.
            if task.cancel.is_set() and not task.is_live and not task.error:
                status = "cancelled"
            if status == "finished":
                path = safe_download_path(result.get("filename") or "")
                if path is None or not path.name.startswith(task.id):
                    task.error, task.status = "Файл не найден после скачивания", "error"
                    return
                task.filename = path.name                     # <id>.<ext>
                task.display_name = pretty_filename(task.title, path.suffix)
                task.filesize = path.stat().st_size
                task.percent = 100
                task.status = "finished"
            elif status == "cancelled":
                task.status = "cancelled"
            else:
                # Текст ПЕРЕД статусом: клиент закрывает поток по терминальному
                # статусу и успевал прочитать «Ошибка: » без причины.
                task.error = result.get("error") or task.error or "Ошибка скачивания"
                task.status = "error"
        except Exception:                                      # noqa: BLE001
            task.error = "Внутренняя ошибка, попробуйте другой формат"
            task.status = "error"
            logging.exception("внутренний сбой задачи (%s)", _host_of(task.url))
        finally:
            task.completed = True
            self._finish_metrics(task)
            # Уборка на ВСЕХ путях выхода: мусор от сбоев считается в квоте,
            # и иначе сервис начинал отвечать «нет места» здоровым людям.
            if task.status != "finished":
                cleanup_partials(task.id)
            if self.on_complete:
                try:
                    self.on_complete(task)
                except Exception:      # статистика не должна ломать загрузку
                    pass


    @staticmethod
    def _finish_metrics(task: Task) -> None:
        """Свести тайминги задачи в миллисекунды (служебные отметки — прочь)."""
        tm = task.metrics
        end = time.monotonic()
        spawned = tm.pop("_spawned", None)
        first = tm.pop("_first_byte", None)
        dl_end = tm.pop("_dl_end", None)
        ms = lambda a, b: int((b - a) * 1000) if a is not None and b is not None else None  # noqa: E731
        tm["prepare_ms"] = ms(spawned, first)             # до первого байта
        tm["download_ms"] = ms(first, dl_end)
        tm["process_ms"] = ms(dl_end, end)                # склейка, MP3, обрезка
        tm["total_ms"] = ms(spawned, end)
        if task.filesize and tm["download_ms"]:
            tm["avg_speed"] = int(task.filesize / max(tm["download_ms"], 1) * 1000)


# ---- файлы задачи на диске ----
_PARTIAL_SUFFIXES = {".part", ".ytdl", ".temp"}


def task_files(tid: str) -> list[Path]:
    """Готовые файлы задачи (без недокачанных кусков), по порядку."""
    return [p for p in sorted(config.DOWNLOAD_DIR.glob(f"{tid}.*"))
            if p.is_file() and p.suffix.lower() not in _PARTIAL_SUFFIXES
            and ".part-" not in p.name]


def cleanup_partials(tid: str) -> None:
    """Удалить все файлы задачи: имена начинаются с её id."""
    for p in config.DOWNLOAD_DIR.glob(f"{tid}.*"):
        try:
            p.unlink()
        except OSError:
            pass


def finalize_partial(tid: str) -> str | None:
    """Превратить прерванную запись эфира в готовый файл: берём самый
    крупный кусок и снимаем суффикс .part (MPEG-TS остаётся проигрываемым)."""
    best, best_size = None, 0
    for p in config.DOWNLOAD_DIR.glob(f"{tid}.*"):
        if p.suffix in (".ytdl", ".temp"):
            continue
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        if sz > best_size:
            best, best_size = p, sz
    if not best:
        return None
    if best.name.endswith(".part"):
        final = best.with_name(best.name[:-len(".part")])
        try:
            best.replace(final)
            return str(final)
        except OSError:
            return str(best)
    return str(best)


class Overloaded(Exception):
    """Очередь или таблица задач переполнены."""


class _QuietLogger:
    """Перехватывает вывод yt-dlp, чтобы ссылки не попадали в журнал.

    Опции quiet и no_warnings глушат обычные сообщения, но ОШИБКИ yt-dlp
    всё равно печатает в stderr — вместе с полным адресом, который вставил
    пользователь. systemd подхватывает stderr, и ссылки оседают в journald,
    хотя сервис обещает их не записывать.

    Текст ошибки нам всё равно возвращается через исключение, поэтому
    здесь достаточно ничего не печатать; для диагностики оставляем только
    домен, без пути и параметров.
    """

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        # Не логируем НИЧЕГО из текста ошибки. Вырезать оттуда ссылку
        # недостаточно: yt-dlp подставляет в сообщение идентификатор,
        # выкроенный из пути, и в нём может оказаться токен из адреса
        # (проверено на реальной ссылке). Домен и класс ошибки пишутся
        # отдельно в _run — этого хватает для диагностики.
        pass


def _host_of(url: str) -> str:
    """Домен для журнала. Полный URL не пишем — это приватность пользователя."""
    try:
        return urlparse(url).hostname or "?"
    except ValueError:
        return "?"


def _clean_err(msg: str) -> str:
    """Подготовить текст ошибки yt-dlp для показа пользователю.

    Сообщения yt-dlp/ffmpeg полезны, но могут содержать локальные пути,
    имя пользователя и внутренние адреса — наружу это отдавать нельзя.
    """
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)                 # ANSI
    msg = msg.replace("ERROR:", "").strip()
    # Только пути в файловой системе. Общая регулярка на «слэши» съедала
    # путь внутри URL и превращала ссылку пользователя в «https:/<путь>»,
    # делая сообщение бесполезным.
    msg = re.sub(r"(?<![\w/:])/(?:home|root|tmp|var|etc|usr|opt|srv|proc)"
                 r"(?:/[\w.\-]+)*", "<путь>", msg)
    msg = re.sub(r"\b\d{1,3}(\.\d{1,3}){3}\b", "<адрес>", msg)   # IPv4
    msg = re.sub(r"\b(?:127\.0\.0\.1|localhost)(?::\d+)?\b", "<адрес>", msg)

    # Самая частая ошибка — ссылка, для которой у yt-dlp нет экстрактора
    # (например, аудио ВКонтакте). Сырая формулировка ничего не объясняет.
    if "Unsupported URL" in msg:
        return ("Этот сайт или тип ссылки не поддерживается. "
                "Проверьте, что это страница видео, а не аудио или плейлиста.")
    if "Private video" in msg or "private" in msg.lower():
        return "Это приватное видео — доступ к нему закрыт"
    if "Video unavailable" in msg:
        return "Видео недоступно"
    if "age" in msg.lower() and "restrict" in msg.lower():
        return "Видео с возрастным ограничением — скачать нельзя"
    return msg[:300] if msg else "Ошибка скачивания"


def pretty_filename(title: str, ext: str) -> str:
    """Имя, которое увидит пользователь. Кириллицу сохраняем — её корректно
    закодирует Content-Disposition (RFC 5987). Убираем только то, что ломает
    файловые системы."""
    name = (title or "видео").strip()
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "", name)   # запрещённые символы
    name = re.sub(r"\s+", " ", name).strip(" .")          # схлопнуть пробелы, снять точки по краям
    if not name:
        name = "видео"
    return name[:120] + ext


def safe_download_path(basename: str) -> Path | None:
    """Защита от path traversal при отдаче файла.

    Имя на диске мы генерируем сами (<id>.<ext>), но проверку оставляем:
    полагаемся на разрешение реального пути, а не на поиск подстроки '..' —
    та отвергала легитимные имена (например, заголовок, кончающийся на '..').
    """
    if not basename or "\x00" in basename:
        return None
    if os.path.basename(basename) != basename:   # любые разделители пути
        return None
    root = config.DOWNLOAD_DIR.resolve()
    target = (root / basename).resolve()
    if target.parent != root:                    # только прямой потомок
        return None
    return target if target.is_file() else None
