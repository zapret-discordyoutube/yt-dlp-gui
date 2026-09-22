"""Исполнитель одной загрузки в отдельном процессе.

Менеджер (downloader.DownloadManager) запускает `python -m jobrunner` на
каждую задачу, пишет в stdin описание задачи одним JSON и читает из stdout
события — по JSON на строку:

    {"ev": "status",   "status": "preparing" | "downloading" | "processing"}
    {"ev": "progress", "percent": .., "speed": .., "eta": .., "total": ..}
    {"ev": "done",     "status": "finished", "filename": "<id>.<ext>"}
    {"ev": "done",     "status": "error", "error": "<текст для пользователя>"}
    {"ev": "done",     "status": "cancelled"}

Почему процесс, а не поток в gunicorn: yt-dlp — это Python, и 32 загрузки
в одном процессе делили один GIL и одну кучу (пик памяти упирался в
MemoryMax, сервис уходил в своп). Отдельный процесс даёт настоящий
параллелизм, возвращает память системе после каждой задачи и позволяет
надёжно остановить зависшую загрузку сигналом.

Остановка: SIGTERM — мягкая отмена (ближайший вызов хука прогресса бросает
DownloadCancelled; запись эфира при этом сохраняется). Если процесс не
вышел сам, менеджер добивает всю группу процессов (вместе с ffmpeg).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import zipfile

import yt_dlp

import config
import downloader as dl
import racefd

# Как часто отправлять прогресс. Хук yt-dlp вызывается на каждый блок
# данных — сотни раз в секунду; родителю столько не нужно.
_PROGRESS_EVERY_SEC = 0.5


class Job:
    """Состояние одной задачи внутри дочернего процесса."""

    def __init__(self, spec: dict, out) -> None:
        self.id: str = spec["id"]
        self.url: str = spec["url"]
        self.title: str = spec.get("title") or "видео"
        self.fmt: str = spec.get("fmt") or ""
        self.extra: dict = spec.get("extra") or {}
        self.is_live: bool = bool(spec.get("is_live"))
        self.images_mode: bool = bool(spec.get("images_mode"))
        self.bundle: bool = bool(spec.get("bundle"))
        self.proxy: str | None = spec.get("proxy") or None
        clip = spec.get("clip")
        self.clip: tuple[float, float | None] | None = (
            (float(clip[0]), None if clip[1] is None else float(clip[1]))
            if clip else None)
        self.cancelled = False
        self.error: str | None = None     # причина, если оборвал сторож
        self._out = out
        self._last_progress = 0.0

    # ---- связь с родителем ----
    def emit(self, **ev) -> None:
        try:
            self._out.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self._out.flush()
        except (BrokenPipeError, ValueError):
            # Родитель ушёл (рестарт сервиса) — дальше работать незачем.
            self.cancelled = True

    def status(self, st: str) -> None:
        self.emit(ev="status", status=st)

    # ---- хук прогресса yt-dlp ----
    def make_hook(self):
        per_file: dict[str, int] = {}
        last_disk_check = [0.0]
        # Сглаженная скорость (EMA): у YouTube отдача рваная, и без
        # сглаживания таймер «осталось» прыгал от секунд до минут.
        ema = [None]
        started = [False]

        def hook(d):
            if self.cancelled:
                raise yt_dlp.utils.DownloadCancelled()

            # Сторож размера. max_filesize yt-dlp смотрит только на
            # Content-Length и не работает для HLS/DASH — считаем сами,
            # по каждому файлу отдельно (видео и звук качаются порознь).
            per_file[d.get("filename") or "?"] = d.get("downloaded_bytes") or 0
            if sum(per_file.values()) > config.MAX_FILESIZE_MB * 1048576:
                self.error = "Файл превышает допустимый размер"
                raise yt_dlp.utils.DownloadCancelled()

            now = time.monotonic()
            if now - last_disk_check[0] > 5:
                last_disk_check[0] = now
                try:
                    free_mb = shutil.disk_usage(config.DOWNLOAD_DIR).free / 1048576
                except OSError:
                    free_mb = None
                if free_mb is not None and free_mb < config.MIN_FREE_DISK_MB:
                    self.error = "На сервере закончилось место"
                    raise yt_dlp.utils.DownloadCancelled()

            st = d.get("status")
            if st == "downloading":
                if not started[0]:
                    started[0] = True
                    self.status("downloading")
                sp = d.get("speed")
                if sp and sp > 0:
                    ema[0] = sp if ema[0] is None else ema[0] * 0.8 + sp * 0.2
                if now - self._last_progress < _PROGRESS_EVERY_SEC:
                    return
                self._last_progress = now
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                speed = ema[0] or sp
                if speed and total and total > done:
                    eta = int((total - done) / speed)
                else:
                    eta = d.get("eta")
                self.emit(ev="progress",
                          percent=round(done / total * 100, 1) if total else None,
                          speed=speed, eta=eta, total=total)
            elif st == "finished":
                # Маленький файл успевает скачаться до первого замера — тогда
                # объявляем «скачивание» задним числом, иначе у задачи нет ни
                # момента первого байта, ни скорости в метриках.
                if not started[0]:
                    started[0] = True
                    self.status("downloading")
                # Дорожка скачана; дальше может быть склейка/перекодирование.
                self.emit(ev="progress", percent=100, speed=None, eta=None,
                          total=None)
                self.status("processing")
        return hook

    # ---- основной путь ----
    def run(self) -> dict:
        self.status("preparing")
        if self.images_mode:
            final = self.download_images()
        else:
            final = self.download_media()
        if self.cancelled:
            raise yt_dlp.utils.DownloadCancelled()
        # Отрезок на YouTube режем после полного скачивания: HLS не даёт
        # скачать секцию (download_ranges на нём зависает).
        if self.clip and dl.is_youtube(self.url) and final and os.path.exists(final):
            self.status("processing")
            final = trim_file(final, self.clip) or final
        if self.bundle:
            self.status("processing")
            final = bundle_outputs(self.id, self.title) or final
        if final and os.path.exists(final):
            return {"status": "finished", "filename": os.path.basename(final)}
        return {"status": "error",
                "error": self.error or "Файл не найден после скачивания"}

    def ydl_opts(self) -> dict:
        opts = {
            "paths": {"home": str(config.DOWNLOAD_DIR)},
            # Имя на диске — только id задачи: не зависит от заголовка
            # (кириллица, эмодзи, разделители пути). Красивое имя
            # пользователь получает при отдаче файла.
            "outtmpl": {"default": f"{self.id}.%(ext)s"},
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "consoletitle": False,
            "socket_timeout": 30,
            # Транзиентные сбои googlevideo (TLS-таймаут фрагмента) иначе
            # роняли загрузку на середине.
            "retries": 10,
            "fragment_retries": 20,
            "file_access_retries": 5,
            "concurrent_fragment_downloads": config.CONCURRENT_FRAGMENTS,
            "logger": dl._QuietLogger(),
            "format": self.fmt,
            "max_filesize": config.MAX_FILESIZE_MB * 1024 * 1024,
            "progress_hooks": [self.make_hook()],
        }
        if self.proxy:
            opts["proxy"] = self.proxy
        # Эфир пишем в MPEG-TS: он остаётся проигрываемым, даже если запись
        # оборвать на середине (у mp4 не будет moov-атома).
        if self.is_live:
            opts["hls_use_mpegts"] = True
        # Отрезок вне YouTube — частичная загрузка только нужного куска.
        if self.clip and not dl.is_youtube(self.url):
            opts.update(dl.clip_range_opts(*self.clip))
        opts.update(self.extra)
        return opts

    def download_media(self) -> str | None:
        with yt_dlp.YoutubeDL(self.ydl_opts()) as ydl:
            info = ydl.extract_info(self.url, download=True)
            reqs = (info.get("requested_downloads") or []) if isinstance(info, dict) else []
            final = reqs[0].get("filepath") if reqs else None
            if final:
                return final
            final = ydl.prepare_filename(info)
        # Постпроцессор аудио меняет расширение.
        for pp in self.extra.get("postprocessors") or []:
            if pp.get("key") == "FFmpegExtractAudio":
                pref = pp.get("preferredcodec")
                if pref in (None, "best"):
                    # «Оригинал» расширение не меняет, но угадать его нельзя.
                    found = dl.task_files(self.id)
                    return str(found[0]) if found else final
                ext = {"aac": "m4a"}.get(pref, pref)
                return os.path.splitext(final)[0] + f".{ext}"
        return final

    def download_images(self) -> str | None:
        """Картинки поста-галереи (Instagram/Twitter). Адреса берём заново
        из yt-dlp, а не от клиента."""
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "ignore_no_formats_error": True, "socket_timeout": 30,
            "logger": dl._QuietLogger(),
            **({"proxy": self.proxy} if self.proxy else {}),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.sanitize_info(ydl.extract_info(self.url, download=False))
        urls = dl.collect_images(info)
        if not urls:
            self.error = "В посте не найдено изображений"
            return None
        self.status("downloading")
        from curl_cffi import requests as cffi
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        total = saved = 0
        for i, u in enumerate(urls, 1):
            if self.cancelled:
                raise yt_dlp.utils.DownloadCancelled()
            try:
                r = cffi.get(u, impersonate="chrome", timeout=30, proxies=proxies)
            except Exception:                       # noqa: BLE001
                continue
            data = r.content if getattr(r, "status_code", 0) == 200 else b""
            if not data:
                continue
            total += len(data)
            if total > config.MAX_FILESIZE_MB * 1048576:
                self.error = "Файлы превышают допустимый размер"
                raise yt_dlp.utils.DownloadCancelled()
            ext = dl._img_ext(r.headers.get("content-type"), u)
            # Номер с ведущим нулём — файлы идут в порядке карусели.
            p = config.DOWNLOAD_DIR / f"{self.id}.{i:02d}.{ext}"
            try:
                p.write_bytes(data)
                saved += 1
            except OSError:
                pass
            self.emit(ev="progress", percent=round(i / len(urls) * 100, 1),
                      speed=None, eta=None, total=total)
        if saved == 0:
            self.error = self.error or "Не удалось скачать изображения"
            return None
        self.bundle = saved > 1
        files = dl.task_files(self.id)
        return str(files[0]) if files else None


def trim_file(path: str, clip: tuple) -> str | None:
    """Вырезать отрезок из готового файла через ffmpeg -c copy (без
    перекодирования; -ss до -i садится на ближайший ключевой кадр).
    Возвращает путь к результату или None — тогда остаётся целый файл."""
    start, end = clip
    root, ext = os.path.splitext(path)
    out = f"{root}.clip{ext}"
    cmd = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", path]
    if end is not None:
        cmd += ["-t", f"{end - start:.3f}"]
    cmd += ["-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero"]
    if ext.lower() in (".mp4", ".m4a", ".mov"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(out)
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        try:
            os.path.exists(out) and os.unlink(out)
        except OSError:
            pass
        return None
    try:
        os.replace(out, path)
    except OSError:
        return out
    return path


def bundle_outputs(tid: str, title: str) -> str | None:
    """Собрать все файлы задачи в один ZIP, если их больше одного."""
    files = dl.task_files(tid)
    if len(files) <= 1:
        return str(files[0]) if files else None
    zpath = config.DOWNLOAD_DIR / f"{tid}.zip"
    stem = dl.pretty_filename(title, "")
    used: set[str] = set()
    # ZIP_STORED: медиа уже сжато, а хост — гипервизор; не тратим CPU
    # соседних ВМ на бесполезную компрессию.
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        for p in files:
            ext = p.suffix or ""
            arc, n = f"{stem}{ext}", 1
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


def execute(spec: dict, out) -> dict:
    """Выполнить задачу и вернуть итоговое событие done (без "ev")."""
    job = Job(spec, out)
    result = _execute(job)
    # Метрики движка для статистики производительности (без ссылок).
    engine = ("images" if job.images_mode
              else "racefd" if racefd.STATS["files"] else "ytdlp")
    result["metrics"] = {"engine": engine, "mirrors": racefd.STATS["mirrors"],
                         "conn_ok": racefd.STATS["conn_ok"],
                         "conn_fail": racefd.STATS["conn_fail"]}
    return result


def _execute(job: Job) -> dict:

    def on_term(signum, frame):              # noqa: ARG001
        job.cancelled = True
    signal.signal(signal.SIGTERM, on_term)

    try:
        return job.run()
    except yt_dlp.utils.DownloadCancelled:
        # Ручная остановка эфира — это «сохранить записанное». Сторож же
        # (размер, место) ставит job.error — тогда это ошибка.
        if job.is_live and not job.error:
            saved = dl.finalize_partial(job.id)
            if saved:
                return {"status": "finished", "filename": os.path.basename(saved)}
        if job.error:
            return {"status": "error", "error": job.error}
        return {"status": "cancelled"}
    except (yt_dlp.utils.DownloadError, yt_dlp.utils.UnavailableVideoError) as e:
        if job.cancelled:
            return {"status": "cancelled"}
        logging.warning("загрузка не удалась (%s): %s",
                        dl._host_of(job.url), type(e).__name__)
        return {"status": "error", "error": dl._clean_err(str(e))}
    except Exception:                          # noqa: BLE001
        # Текст внутренней ошибки наружу не отдаём.
        logging.exception("внутренний сбой задачи (%s)", dl._host_of(job.url))
        return {"status": "error",
                "error": "Внутренняя ошибка, попробуйте другой формат"}


def main() -> int:
    # Канал событий — отдельный дескриптор, а stdout процесса уводим в
    # /dev/null: всё, что случайно напечатает yt-dlp или библиотека, не
    # должно ни ломать протокол, ни утекать в журнал вместе со ссылкой.
    out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    logging.basicConfig(level=logging.WARNING, format="[job] %(message)s")
    # Видео с googlevideo — своим параллельным загрузчиком (см. racefd).
    if config.YT_PARALLEL:
        racefd.install()
    try:
        spec = json.loads(sys.stdin.readline())
    except (ValueError, OSError):
        return 2
    result = execute(spec, out)
    try:
        out.write(json.dumps({"ev": "done", **result}, ensure_ascii=False) + "\n")
        out.flush()
    except (BrokenPipeError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
