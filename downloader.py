"""Ядро: работа с yt-dlp, менеджер фоновых задач, пресеты форматов."""
from __future__ import annotations

import os
import re
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

# Аудио-кодеки: ключ -> (yt-dlp preferredcodec, расширение, подпись)
_ACODECS = {
    "mp3":  ("mp3",  "mp3", "MP3"),
    "aac":  ("aac",  "m4a", "AAC (m4a)"),
    "opus": ("opus", "opus", "Opus"),
}
# Видео-контейнеры: ключ -> (merge_output_format, подпись)
_VCONTAINERS = {
    "mp4":  ("mp4",  "MP4 (H.264)"),
    "webm": ("webm", "WebM (VP9)"),
}


def build_format(kind: str, height: str = "auto",
                 acodec: str = "mp3", vcontainer: str = "mp4") -> tuple[str, dict, str]:
    """Возвращает (format_selector, extra_opts, label) по безопасному выбору.

    kind='audio': acodec in {mp3,aac,opus}
    kind='video': height in {auto,2160,...,360}, vcontainer in {mp4,webm}
    """
    if kind == "audio":
        if acodec not in _ACODECS:
            raise ValueError("unknown_acodec")
        pref, _ext, label = _ACODECS[acodec]
        return "bestaudio/best", {
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": pref,
                "preferredquality": "192",
            }],
        }, label

    if kind == "video":
        if vcontainer not in _VCONTAINERS:
            raise ValueError("unknown_container")
        merge, cname = _VCONTAINERS[vcontainer]
        if vcontainer == "mp4":
            vsel = "bestvideo[ext=mp4]/bestvideo[vcodec^=avc1]/bestvideo"
            asel = "bestaudio[ext=m4a]/bestaudio"
        else:
            vsel = "bestvideo[ext=webm]/bestvideo[vcodec^=vp9]/bestvideo"
            asel = "bestaudio[ext=webm]/bestaudio"
        if height == "auto":
            sel = f"{vsel}+{asel}/best"
            hlabel = "Авто"
        elif height in _HEIGHTS:
            hpart = f"[height<={height}]"
            sel = (f"bestvideo{hpart}+bestaudio/"
                   f"best{hpart}/best")
            hlabel = f"{height}p"
        else:
            raise ValueError("unknown_height")
        return sel, {"merge_output_format": merge}, f"{hlabel} · {cname}"

    raise ValueError("unknown_kind")


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
        # связка video+audio всегда требует merge
        extra["merge_output_format"] = "mp4" if container == "auto" else container
    elif container in _VCONTAINERS:
        extra["merge_output_format"] = _VCONTAINERS[container][0]
    return fid, extra, f"format {fid}"


def validate_url(url: str) -> str:
    url = (url or "").strip()
    if not url or len(url) > 2048:
        raise ValueError("empty_or_too_long")
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("bad_scheme")
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
    filename: str | None = None  # basename готового файла
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
            "filename": self.filename,
            "filesize": self.filesize,
            "error": self.error,
            "created_at": self.created_at,
        }


class DownloadManager:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.lock = threading.Lock()
        self.sema = threading.Semaphore(config.MAX_CONCURRENT_DOWNLOADS)
        config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self._start_janitor()

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
        return self._dir_size_mb() >= config.DISK_QUOTA_MB

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
                "outtmpl": {"default": "%(title).150B [%(id)s].%(ext)s"},
                "restrictfilenames": True,
                "windowsfilenames": True,
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
                task.filename = os.path.basename(final)
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

    def _cleanup_partials(self, task: Task) -> None:
        for p in config.DOWNLOAD_DIR.glob("*.part"):
            try:
                p.unlink()
            except OSError:
                pass


def _clean_err(msg: str) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)          # ANSI
    msg = msg.replace("ERROR:", "").strip()
    return msg[:300] if msg else "Ошибка скачивания"


def safe_download_path(basename: str) -> Path | None:
    """Защита от path traversal при отдаче файла."""
    if not basename or "/" in basename or "\\" in basename or ".." in basename:
        return None
    target = (config.DOWNLOAD_DIR / basename).resolve()
    root = config.DOWNLOAD_DIR.resolve()
    if not str(target).startswith(str(root) + os.sep):
        return None
    return target if target.is_file() else None
