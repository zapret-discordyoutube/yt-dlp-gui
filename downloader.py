"""Ядро: работа с yt-dlp, менеджер фоновых задач, пресеты форматов."""
from __future__ import annotations

import ipaddress
import logging
import os
import queue
import re
import socket
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

import yt_dlp

import config

# --- Пресеты формата (клиент НЕ может передать произвольные опции yt-dlp) ---
# Всё строится из фиксированного перечня: kind + height + codec.
_HEIGHTS = {"2160", "1440", "1080", "720", "480", "360"}

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
                 vcodec: str = "auto", vaudio: str = "auto") -> tuple[str, dict, str]:
    """Возвращает (format_selector, extra_opts, label) по безопасному выбору.

    kind='audio': acodec in {best,mp3,aac,opus}
    kind='video': height in {auto,2160..360}, vcodec in {auto,h264,av1,vp9},
                  vaudio in {auto,aac,opus}
    """
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
            "aac":  "bestaudio[acodec^=mp4a]/bestaudio/best",
            "opus": "bestaudio[acodec^=opus]/bestaudio/best",
        }.get(acodec, "bestaudio/best")
        return prefer, {"postprocessors": [pp]}, label

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
            f"{v}+bestaudio{afilter}" if afilter else None,
            f"{v}+bestaudio",
            f"bestvideo{hfilter}+bestaudio" if vfilter else None,
            f"best{hfilter}{vfilter}" if vfilter else None,
            f"best{hfilter}",
            "worst" if hfilter else "best",
        ]))
        label = " · ".join([hlabel, vlabel] + ([alabel] if vaudio != "auto" else []))
        return sel, {"merge_output_format": container}, label

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
def probe(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        # без своего логгера yt-dlp печатает ошибки со ссылкой в stderr
        "logger": _QuietLogger(),
        **({"proxy": config.PROXY} if config.PROXY else {}),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.sanitize_info(ydl.extract_info(url, download=False))

    # Прямые эфиры: идущий можно записывать, будущий — ещё нельзя.
    live_status = info.get("live_status")
    is_live = bool(info.get("is_live")) or live_status == "is_live"
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

    return {
        "formats": formats,
        "id": info.get("id"),
        "title": info.get("title") or "Без названия",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": duration,
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "heights": avail,
        "is_live": is_live,
    }


# --- Модель задачи ---
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
    status: str = "queued"       # queued|downloading|processing|finished|error|cancelled
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
    cancel: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "label": self.label,
            "thumbnail": self.thumbnail,
            "status": self.status,
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
                    if t.status in ("queued", "downloading", "processing")}

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
               is_live: bool = False) -> Task:
        # Идентификатор — единственное, что защищает чужой файл от выдачи,
        # поэтому берём его целиком, а не первые 12 символов.
        task = Task(id=uuid.uuid4().hex, url=url, title=title,
                    fmt=fmt, extra=extra, label=label, thumbnail=thumbnail,
                    bundle=bundle, is_live=is_live)
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
        active = sum(1 for t in tasks if t.status in ("downloading", "processing"))
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
        if t and t.status in ("queued", "downloading", "processing"):
            t.cancel.set()
            return True
        return False

    def _make_hook(self, task: Task):
        last_disk_check = [0.0]
        # Байты по каждому файлу отдельно: на связке video+audio yt-dlp
        # начинает счёт заново для второй дорожки, и проверка «по текущему
        # файлу» пропускала суммарно до двух потолков на диск.
        per_file: dict[str, int] = {}

        def hook(d):
            if task.cancel.is_set():
                raise yt_dlp.utils.DownloadCancelled()

            # Сторожевой контроль прямо во время загрузки.
            # max_filesize у yt-dlp проверяется по заголовку Content-Length и
            # не работает для HLS/DASH и chunked-ответов: такой поток качался
            # бы без ограничения размера. А хост — гипервизор, заполнить его
            # раздел нельзя.
            name = d.get("filename") or "?"
            per_file[name] = d.get("downloaded_bytes") or 0
            # Размер проверяем на КАЖДОМ вызове: он почти бесплатен, а по
            # таймеру быстрая загрузка успевала закончиться между замерами
            # и не проверялась вовсе.
            if sum(per_file.values()) > config.MAX_FILESIZE_MB * 1048576:
                task.error = "Файл превышает допустимый размер"
                task.cancel.set()
                raise yt_dlp.utils.DownloadCancelled()

            now = time.time()
            if now - last_disk_check[0] > 5:      # обращение к ФС — реже
                last_disk_check[0] = now
                try:
                    free_mb = shutil.disk_usage(config.DOWNLOAD_DIR).free / 1048576
                except OSError:
                    free_mb = None
                if free_mb is not None and free_mb < config.MIN_FREE_DISK_MB:
                    task.error = "На сервере закончилось место"
                    task.cancel.set()
                    raise yt_dlp.utils.DownloadCancelled()

            st = d.get("status")
            if st == "downloading":
                task.status = "downloading"
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                task.total_bytes = total
                if total:
                    task.percent = round(d.get("downloaded_bytes", 0) / total * 100, 1)
                task.speed = d.get("speed")
                task.eta = d.get("eta")
            elif st == "finished":
                # скачивание завершено, дальше возможен постпроцессинг (merge/mp3)
                task.percent = 100
                task.status = "processing"
        return hook

    def _run(self, task: Task) -> None:
        try:
            if task.cancel.is_set():
                task.status = "cancelled"
                return
            fmt, extra = task.fmt, task.extra
            opts = {
                "paths": {"home": str(config.DOWNLOAD_DIR)},
                # Имя на диске задаём сами: только id задачи. Так оно не зависит
                # от заголовка (кириллица, эмодзи, точки) и не может содержать
                # разделителей пути. Красивое имя пользователь получает через
                # download_name при отдаче.
                "outtmpl": {"default": f"{task.id}.%(ext)s"},
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,      # не писать активность пользователя в лог
                "consoletitle": False,
                "socket_timeout": 30,
                "retries": 5,
                "concurrent_fragment_downloads": config.CONCURRENT_FRAGMENTS,
                "logger": _QuietLogger(),
                **({"proxy": config.PROXY} if config.PROXY else {}),
                # Эфир пишем в MPEG-TS: такой контейнер остаётся проигрываемым,
                # даже если запись оборвать на середине (нет moov-атома, как у
                # mp4). Именно это делает «стоп и сохранить» осмысленным.
                **({"hls_use_mpegts": True} if task.is_live else {}),
                "format": fmt,
                "max_filesize": config.MAX_FILESIZE_MB * 1024 * 1024,
                "progress_hooks": [self._make_hook(task)],
                **extra,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(task.url, download=True)
                # определить итоговый файл (учёт смены расширения постпроцессором)
                final = None
                reqs = (info.get("requested_downloads") or []) if isinstance(info, dict) else []
                if reqs:
                    final = reqs[0].get("filepath")
                if not final:
                    final = ydl.prepare_filename(info)
                    # постпроцессор аудио меняет расширение
                    for pp in task.extra.get("postprocessors") or []:
                        if pp.get("key") == "FFmpegExtractAudio":
                            pref = pp.get("preferredcodec")
                            if pref in (None, "best"):
                                # «Оригинал» не меняет расширение — угадать
                                # его нельзя, ищем файл задачи на диске.
                                found = sorted(
                                    config.DOWNLOAD_DIR.glob(f"{task.id}.*"))
                                final = str(found[0]) if found else final
                            else:
                                ext = {"aac": "m4a"}.get(pref, pref)
                                final = os.path.splitext(final)[0] + f".{ext}"
                            break
            if task.cancel.is_set():
                task.status = "cancelled"
                self._cleanup_partials(task)
                return
            if task.cancel.is_set():
                # Отмена могла прийти уже после последнего хука. Публиковать
                # такую загрузку в ленту и засчитывать её нельзя: пользователь
                # нажал «Отмена» и получил подтверждение.
                task.status = "cancelled"
                self._cleanup_partials(task)
                return
            # Несколько выходных файлов (обложка/описание отдельно, дорожки
            # без склейки) не влезают в отдачу «один файл» — пакуем в ZIP.
            if task.bundle:
                final = self._bundle_outputs(task) or final
            if final and os.path.exists(final):
                task.filename = os.path.basename(final)          # <id>.<ext>
                ext = os.path.splitext(final)[1]
                task.display_name = pretty_filename(task.title, ext)
                task.filesize = os.path.getsize(final)
                task.percent = 100
                task.status = "finished"
            else:
                task.error = "Файл не найден после скачивания"
                task.status = "error"
        except yt_dlp.utils.DownloadCancelled:
            # Для эфира ручная остановка (без ошибки сторожа) означает
            # «сохранить записанное», а не выбросить. Сторож же ставит
            # task.error и обрывает — тогда чистим, как обычную ошибку.
            saved = (self._finalize_partial(task)
                     if task.is_live and not task.error else None)
            if saved:
                task.filename = os.path.basename(saved)
                ext = os.path.splitext(saved)[1]
                task.display_name = pretty_filename(task.title, ext)
                task.filesize = os.path.getsize(saved)
                task.percent = 100
                task.status = "finished"
            else:
                # Сторож мог прервать загрузку по размеру или нехватке места —
                # тогда это ошибка с причиной, а не тихая отмена пользователем.
                task.status = "error" if task.error else "cancelled"
                self._cleanup_partials(task)
        except (yt_dlp.utils.DownloadError,
                yt_dlp.utils.UnavailableVideoError) as e:
            # осмысленное сообщение самого yt-dlp — показываем очищенным
            # Текст ПЕРЕД статусом: клиент закрывает поток по терминальному
            # статусу и успевал прочитать «Ошибка: » без причины.
            task.error = _clean_err(str(e))
            task.status = "error"
            logging.warning("загрузка не удалась (%s): %s",
                            _host_of(task.url), type(e).__name__)
        except Exception:
            # что угодно иное — внутренняя ошибка; наружу её текст не отдаём
            task.error = "Внутренняя ошибка, попробуйте другой формат"
            task.status = "error"
            logging.exception("внутренний сбой задачи (%s)", _host_of(task.url))
        finally:
            task.completed = True
            # Уборка на ВСЕХ путях выхода. Раньше она была только в ветке
            # отмены, а самый частый исход в проде — сетевой сбой, 403 или
            # падение ffmpeg — оставлял до двух потолков размера мусора на
            # FILE_TTL. Он считается в квоте, и сервис начинал отвечать
            # «нет места» здоровым пользователям.
            if task.status != "finished":
                self._cleanup_partials(task)
            if self.on_complete:
                try:
                    self.on_complete(task)
                except Exception:      # статистика не должна ломать загрузку
                    pass

    def _bundle_outputs(self, task: Task) -> str | None:
        """Собрать все файлы задачи в один ZIP, если их больше одного.

        Возвращает путь к архиву (или к единственному файлу, если пакуемого
        оказалось не больше одного). Исходные файлы после упаковки удаляем.
        """
        files = [p for p in sorted(config.DOWNLOAD_DIR.glob(f"{task.id}.*"))
                 if p.suffix.lower() not in {".part", ".ytdl", ".temp"}
                 and ".part-" not in p.name]
        if len(files) <= 1:
            return str(files[0]) if files else None
        zpath = config.DOWNLOAD_DIR / f"{task.id}.zip"
        stem = pretty_filename(task.title, "")
        used: set[str] = set()
        # ZIP_STORED: медиа уже сжато, а хост — гипервизор; не тратим CPU
        # соседних ВМ на бесполезную компрессию.
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
            for p in files:
                ext = p.suffix or ""
                arc = f"{stem}{ext}"
                n = 1
                while arc in used:
                    n += 1
                    arc = f"{stem} ({n}){ext}"
                used.add(arc)
                z.write(p, arcname=arc)
        for p in files:
            try:
                p.unlink()
            except OSError:
                pass
        return str(zpath)

    def _finalize_partial(self, task: Task) -> str | None:
        """Превратить прерванную запись эфира в готовый файл.

        Берём самый крупный кусок задачи и снимаем суффикс .part. Для эфира
        в MPEG-TS такой файл остаётся проигрываемым.
        """
        best = None
        best_size = 0
        for p in config.DOWNLOAD_DIR.glob(f"{task.id}.*"):
            if p.suffix in (".ytdl", ".temp"):
                continue
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            if sz > best_size:
                best, best_size = p, sz
        if not best or best_size == 0:
            return None
        if best.name.endswith(".part"):
            final = best.with_name(best.name[:-len(".part")])
            try:
                best.replace(final)
                return str(final)
            except OSError:
                return str(best)
        return str(best)

    def _cleanup_partials(self, task: Task) -> None:
        # только файлы этой задачи: имена начинаются с её id
        for p in config.DOWNLOAD_DIR.glob(f"{task.id}.*"):
            try:
                p.unlink()
            except OSError:
                pass


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
