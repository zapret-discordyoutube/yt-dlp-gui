"""Ядро: работа с yt-dlp, менеджер фоновых задач, пресеты форматов."""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

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
        return "bestaudio/best", {"postprocessors": [pp]}, label

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
        # Каскад запасных вариантов: сначала точный кодек и звук, затем
        # послабления, чтобы выбор редкого сочетания не приводил к отказу.
        sel = "/".join(filter(None, [
            f"{v}+bestaudio{afilter}" if afilter else None,
            f"{v}+bestaudio",
            f"best{hfilter}{vfilter}" if vfilter else None,
            f"best{hfilter}",
            "best",
        ]))
        label = " · ".join([hlabel, vlabel] + ([alabel] if vaudio != "auto" else []))
        return sel, {"merge_output_format": container}, label

    raise ValueError("unknown_kind")


_MERGE_CONTAINERS = {"mp4", "webm", "mkv", "mov"}

# Разрешаем произвольный выбор формата, но только как ID (и связку через '+').
# Запрещены скобки, фильтры, '/', пробелы — то есть синтаксис селекторов yt-dlp.
_FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,48}(\+[A-Za-z0-9_\-.]{1,48})?$")


def build_from_format_id(format_id: str, container: str = "auto") -> tuple[str, dict, str]:
    """Точный выбор формата из того, что yt-dlp отдал для этого URL."""
    fid = (format_id or "").strip()
    if not _FORMAT_ID_RE.fullmatch(fid):
        raise ValueError("bad_format_id")
    extra: dict = {}
    if "+" in fid:
        # Связка video+audio всегда требует склейки. mkv принимает любое
        # сочетание кодеков, поэтому он — безопасный выбор по умолчанию,
        # когда пользователь смешал произвольные дорожки.
        extra["merge_output_format"] = (
            container if container in _MERGE_CONTAINERS else "mkv")
    return fid, extra, f"формат {fid}"


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
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.sanitize_info(ydl.extract_info(url, download=False))

    duration = info.get("duration")
    if duration and duration > config.MAX_DURATION_SEC:
        raise ValueError("too_long")

    # доступные высоты видео -> для простого селектора качества
    heights = set()
    for f in info.get("formats") or []:
        h = f.get("height")
        if h and f.get("vcodec") not in (None, "none"):
            heights.add(int(h))
    avail = [str(h) for h in sorted(heights, reverse=True) if str(h) in _HEIGHTS]

    # полный список форматов -> продвинутый режим «всё что есть»
    formats = []
    for f in info.get("formats") or []:
        fid = f.get("format_id")
        if not fid or f.get("format_note") == "storyboard":
            continue
        v = f.get("vcodec") or "none"
        a = f.get("acodec") or "none"
        if v == "none" and a == "none":
            continue
        kind = ("audio" if v == "none" else
                "video" if a == "none" else "both")
        formats.append({
            "format_id": fid,
            "kind": kind,
            "ext": f.get("ext"),
            "height": f.get("height"),
            "fps": f.get("fps"),
            "resolution": f.get("resolution") or (
                f"{f.get('width')}x{f.get('height')}" if f.get("height") else None),
            "vcodec": None if v == "none" else v.split(".")[0],
            "acodec": None if a == "none" else a.split(".")[0],
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
        self.sema = threading.Semaphore(config.MAX_CONCURRENT_DOWNLOADS)
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
        cutoff = now - config.FILE_TTL_MINUTES * 60
        for p in config.DOWNLOAD_DIR.iterdir():
            if p.is_file():
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    pass
        # снять старые карточки задач (только в памяти)
        tcut = now - config.TASK_TTL_MINUTES * 60
        with self.lock:
            for tid in [t.id for t in self.tasks.values()
                        if t.created_at < tcut
                        and t.status in ("finished", "error", "cancelled", "served")]:
                self.tasks.pop(tid, None)

    def _start_janitor(self) -> None:
        def loop():
            while True:
                time.sleep(config.JANITOR_INTERVAL_SEC)
                try:
                    self._janitor_pass()
                except Exception:
                    pass
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
               title: str, thumbnail: str | None) -> Task:
        task = Task(id=uuid.uuid4().hex[:12], url=url, title=title,
                    fmt=fmt, extra=extra, label=label, thumbnail=thumbnail)
        with self.lock:
            self.tasks[task.id] = task
        threading.Thread(target=self._run, args=(task,), daemon=True).start()
        return task

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
        def hook(d):
            if task.cancel.is_set():
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
        acquired = self.sema.acquire(timeout=1800)
        if not acquired:
            task.status = "error"
            task.error = "Сервис занят, попробуйте позже"
            return
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
                            ext = {"aac": "m4a"}.get(pref, pref)
                            final = os.path.splitext(final)[0] + f".{ext}"
                            break
            if task.cancel.is_set():
                task.status = "cancelled"
                self._cleanup_partials(task)
                return
            if final and os.path.exists(final):
                task.filename = os.path.basename(final)          # <id>.<ext>
                ext = os.path.splitext(final)[1]
                task.display_name = pretty_filename(task.title, ext)
                task.filesize = os.path.getsize(final)
                task.percent = 100
                task.status = "finished"
            else:
                task.status = "error"
                task.error = "Файл не найден после скачивания"
        except yt_dlp.utils.DownloadCancelled:
            task.status = "cancelled"
            self._cleanup_partials(task)
        except yt_dlp.utils.DownloadError as e:
            task.status = "error"
            task.error = _clean_err(str(e))
        except Exception as e:  # noqa: BLE001
            task.status = "error"
            task.error = _clean_err(str(e))
        finally:
            self.sema.release()
            if self.on_complete:
                try:
                    self.on_complete(task)
                except Exception:      # статистика не должна ломать загрузку
                    pass

    def _cleanup_partials(self, task: Task) -> None:
        # только файлы этой задачи: имена начинаются с её id
        for p in config.DOWNLOAD_DIR.glob(f"{task.id}.*"):
            try:
                p.unlink()
            except OSError:
                pass


def _clean_err(msg: str) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)          # ANSI
    msg = msg.replace("ERROR:", "").strip()
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
