"""Исполнитель одной загрузки в отдельном процессе.

Менеджер (downloader.DownloadManager) запускает `python -m jobrunner` на
каждую задачу, пишет в stdin описание задачи одним JSON и читает из stdout
события — по JSON на строку:

    {"ev": "status",   "status": "preparing" | "downloading" | "processing"}
    {"ev": "progress", "percent": .., "speed": .., "eta": .., "total": ..}
    {"ev": "phase",    "phase": "connect" | "bypass"}   (до первых байт)
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

import copy
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

# Порядок форматов YouTube: сначала разрешение и fps, затем https вместо HLS.
YT_FORMAT_SORT = ("res", "fps", "hdr:12", "proto:https")

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
        self.extra: dict = dict(spec.get("extra") or {})
        # «Не объединять» при каскадном выборе — см. split_tracks.
        self.split: bool = bool(self.extra.pop(dl.SPLIT_KEY, False))
        # Смешанная карусель: к роликам — фото поста (см. download_playlist).
        self.post_images: bool = bool(self.extra.pop(dl.POST_IMAGES_KEY, False))
        self.is_live: bool = bool(spec.get("is_live"))
        self.images_mode: bool = bool(spec.get("images_mode"))
        self.bundle: bool = bool(spec.get("bundle"))
        self.proxy: str | None = spec.get("proxy") or None
        # Разбор из «Проверить» (сделан сервером тем же путём) — если есть,
        # ролик заново не разбираем.
        self.info: dict | None = spec.get("info") or None
        # Плейлист архивом: сколько роликов; названия — по номеру в плейлисте.
        self.playlist: int = int(spec.get("playlist") or 0)
        self.pl_titles: dict[int, str] = {}
        clip = spec.get("clip")
        self.clip: tuple[float, float | None] | None = (
            (float(clip[0]), None if clip[1] is None else float(clip[1]))
            if clip else None)
        # YouTube: отрезок режем из целого скачанного ролика (HLS не даёт
        # скачать секцию) — исходник меряем своим потолком, результат — общим.
        self.trim_after: bool = bool(self.clip) and dl.is_youtube(self.url)
        self.cancelled = False
        self.pl_done = 0
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
        # Дорожка докачана — «обработка». Но у видео со звуком дальше идёт
        # следующая дорожка: снова «скачивание», иначе сторож менеджера ждал
        # бы зависшую загрузку звука по пределу склейки (час), а не простоя.
        track_done = [False]

        def next_track():
            if track_done[0]:
                track_done[0] = False
                self.status("downloading")
                # Прогресс прошлой дорожки (100%) — прочь: до первых байт
                # новой карточка покажет «подключаемся», а не «100%».
                self.emit(ev="progress", percent=0, speed=None, eta=None, total=None)

        def hook(d):
            if self.cancelled:
                raise yt_dlp.utils.DownloadCancelled()

            # Сторож размера. max_filesize yt-dlp смотрит только на
            # Content-Length и не работает для HLS/DASH — считаем сами,
            # по каждому файлу отдельно (видео и звук качаются порознь).
            per_file[d.get("filename") or "?"] = d.get("downloaded_bytes") or 0
            if sum(per_file.values()) > self.source_max_mb * 1048576:
                self.error = self.too_big_error()
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

            # Фаза до первых байт от racefd: «подключаемся» / «в обход».
            if d.get("ytg_phase"):
                next_track()
                self.emit(ev="phase", phase=d["ytg_phase"])
                return
            st = d.get("status")
            # Плейлист: прогресс — общий по всем роликам, склейка отдельного
            # ролика «обработкой» всю задачу не объявляет.
            pl_idx = (d.get("info_dict") or {}).get("playlist_index") if self.playlist else None
            if pl_idx:
                self.pl_titles[int(pl_idx)] = (d.get("info_dict") or {}).get("title") or ""
            if self.playlist and st == "finished":
                if not started[0]:
                    started[0] = True
                    self.status("downloading")
                if pl_idx:
                    self.emit(ev="progress", percent=round(min(99.9, pl_idx / self.playlist * 100), 1),
                              speed=None, eta=None, total=None)
                return
            if st == "downloading":
                next_track()
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
                pct = round(done / total * 100, 1) if total else None
                if pl_idx and pct is not None:
                    # Общий прогресс: готовые ролики + доля текущего.
                    pct = round(min(99.9, ((pl_idx - 1) + pct / 100) / self.playlist * 100), 1)
                    eta = None
                self.emit(ev="progress", percent=pct, speed=speed, eta=eta, total=total)
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
                track_done[0] = True
        return hook

    @property
    def source_max_mb(self) -> int:
        """Потолок скачиваемого: для обрезки после скачивания — целый ролик."""
        return config.CLIP_SOURCE_MAX_MB if self.trim_after else config.MAX_FILESIZE_MB

    def too_big_error(self) -> str:
        if self.trim_after:
            return (f"Ролик целиком превышает {_fmt_gb(config.CLIP_SOURCE_MAX_MB)}: "
                    "отрезок с YouTube вырезается из целого ролика, а такие большие "
                    "не скачиваются. Выберите качество пониже")
        return "Файл превышает допустимый размер"

    def check_clip_source(self, ydl, info: dict) -> None:
        """До скачивания: целый ролик под отрезок влезает в потолок и в квоту.
        Размер — из разбора (выбор форматов без сети). Иначе пользователь
        ждал бы до обрыва на потолке, а мы тянули бы гигабайты впустую."""
        try:
            picked = ydl.process_ie_result(copy.deepcopy(info), download=False)
        except yt_dlp.utils.DownloadError:
            return                  # выбор не сошёлся — решит обычный путь
        parts = picked.get("requested_formats") or [picked]
        size = sum(f.get("filesize") or f.get("filesize_approx") or 0 for f in parts)
        if size > config.CLIP_SOURCE_MAX_MB * 1048576:
            self.error = self.too_big_error()
            raise yt_dlp.utils.DownloadCancelled()
        used = 0
        for p in config.DOWNLOAD_DIR.iterdir():
            if not p.name.startswith(self.id + "."):
                try:
                    used += p.stat().st_size
                except OSError:
                    pass
        if used + size > config.DISK_QUOTA_MB * 1048576:
            self.error = "Сейчас на сервере мало места под целый ролик, попробуйте позже"
            raise yt_dlp.utils.DownloadCancelled()

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
        if self.trim_after and final and os.path.exists(final):
            self.status("processing")
            final = trim_file(final, self.clip) or final
            # Исходник качали сверх MAX_FILESIZE_MB: если обрезка не удалась
            # (или отрезок почти во весь ролик) — такой файл не отдаём.
            if os.path.getsize(final) > config.MAX_FILESIZE_MB * 1048576:
                return {"status": "error", "error": "Не удалось вырезать отрезок, а ролик "
                                                    "целиком превышает допустимый размер"}
        if self.bundle:
            self.status("processing")
            final = bundle_outputs(self.id, self.title) or final
        if final and os.path.exists(final):
            out = {"status": "finished", "filename": os.path.basename(final)}
            if self.playlist:
                out["pl_done"] = self.pl_done
            return out
        return {"status": "error",
                "error": self.error or "Файл не найден после скачивания"}

    def ydl_opts(self) -> dict:
        opts = {
            "paths": {"home": str(config.DOWNLOAD_DIR)},
            # Имя на диске — только id задачи: не зависит от заголовка
            # (кириллица, эмодзи, разделители пути). Красивое имя
            # пользователь получает при отдаче файла.
            # Дорожки раздельно — с номером формата: у видео и звука
            # бывает одно расширение (webm), и файлы затёрли бы друг друга.
            "outtmpl": {"default": f"{self.id}.f%(format_id)s.%(ext)s"
                        if self.split or "," in self.fmt else f"{self.id}.%(ext)s"},
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
            "max_filesize": self.source_max_mb * 1024 * 1024,
            "progress_hooks": [self.make_hook()],
        }
        if self.proxy:
            opts["proxy"] = self.proxy
        elif config.YT_SOURCE_IP and dl.is_youtube(self.url):
            # YouTube напрямую — со своего IP (ссылки на файлы привязаны к нему).
            opts["source_address"] = config.YT_SOURCE_IP
        # Эфир пишем в MPEG-TS: он остаётся проигрываемым, даже если запись
        # оборвать на середине (у mp4 не будет moov-атома).
        if self.is_live:
            opts["hls_use_mpegts"] = True
        # YouTube: при равных разрешении и fps — https-формат вместо HLS.
        # «Премиальные» форматы (616 и т.п.) отдаются только через HLS, и
        # yt-dlp выбирал их, а фрагменты HLS идут штатным загрузчиком без
        # смены сервера — и висли на заблокированном видеосервере. Для
        # https-форматов работает racefd. Разрешение при этом не теряется.
        if dl.is_youtube(self.url):
            opts["format_sort"] = list(YT_FORMAT_SORT)
        if self.playlist:
            opts.update({
                "noplaylist": False,
                "playlistend": self.playlist,
                # Номер ролика в имени — по нему архив раскладывает файлы.
                "outtmpl": {"default": f"{self.id}.%(playlist_index)03d.%(ext)s"},
                # Недоступный ролик (18+, удалён) пропускаем, а не валим всё.
                "ignoreerrors": True,
                # Обложку/описание самого плейлиста не пишем — только ролики.
                "allow_playlist_files": False,
                # Пауза между роликами: разборы подряд YouTube принимает за бота.
                # Посту с несколькими видео (Twitter, Instagram) она не нужна.
                **({"sleep_interval": 1, "max_sleep_interval": 3}
                   if dl.is_youtube(self.url) else {}),
                # Длиннее лимита — пропустить (условия списком = «или»).
                "match_filter": yt_dlp.utils.match_filter_func(
                    [f"duration < {config.MAX_DURATION_SEC}", "!duration"]),
            })
        # Отрезок вне YouTube — частичная загрузка только нужного куска.
        if self.clip and not dl.is_youtube(self.url):
            opts.update(dl.clip_range_opts(*self.clip))
        opts.update(self.extra)
        return opts

    def download_playlist(self) -> str | None:
        """Плейлист: ролики по очереди (недоступные пропускаются), затем архив.
        Смешанная карусель: записи-фото yt-dlp пропускает (форматов нет) —
        их качаем следом, под их номерами в карусели."""
        with yt_dlp.YoutubeDL(self.ydl_opts()) as ydl:
            ydl.extract_info(self.url, download=True)
        if self.cancelled:
            raise yt_dlp.utils.DownloadCancelled()
        if self.post_images:
            pairs = [(i, u) for i, u in dl.collect_images_indexed(self.image_info())
                     if i <= self.playlist]
            base = sum(p.stat().st_size for p in dl.task_files(self.id))
            for i, _ in pairs:
                self.pl_titles.setdefault(i, self.title)
            self.save_images([(u, f"{i:03d}") for i, u in pairs], base)
        self.status("processing")
        path, self.pl_done = bundle_playlist(self.id, self.title, self.pl_titles)
        if not path:
            self.error = self.error or "Ни один ролик плейлиста не скачался"
        return path

    @staticmethod
    def split_tracks(ydl, info: dict) -> dict:
        """«Не объединять» при каскаде: выбрать форматы по всему каскаду
        (с его запасными вариантами), затем качать выбранные дорожки
        раздельно ('137+140' -> '137,140'). Возвращает разбор без выбора."""
        picked = ydl.process_ie_result(copy.deepcopy(info), download=False)
        fid = picked.get("format_id") or ""
        if "+" in fid:
            sel = fid.replace("+", ",")
            ydl.params["format"] = sel
            ydl.format_selector = ydl.build_format_selector(sel)
        return dl.unselected_info(picked)

    def download_media(self) -> str | None:
        if self.playlist:
            return self.download_playlist()
        with yt_dlp.YoutubeDL(self.ydl_opts()) as ydl:
            info = None
            if self.info and self.trim_after:
                self.check_clip_source(ydl, self.info)
            if self.info:
                try:
                    src = self.split_tracks(ydl, self.info) if self.split else self.info
                    info = ydl.process_ie_result(copy.deepcopy(src), download=True)
                except yt_dlp.utils.DownloadError:
                    # Готовые ссылки не сработали (истекли, другой путь) —
                    # обычный путь: разобрать ролик заново.
                    if self.cancelled or self.error:
                        raise
                    info = None
            if info is None and self.split:
                fresh = ydl.extract_info(self.url, download=False)
                info = ydl.process_ie_result(self.split_tracks(ydl, fresh), download=True)
            elif info is None:
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

    def image_info(self) -> dict:
        """Разбор поста ради картинок. Адреса берём заново из yt-dlp, а не
        от клиента."""
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "ignore_no_formats_error": True, "socket_timeout": 30,
            "logger": dl._QuietLogger(),
            **({"proxy": self.proxy} if self.proxy else {}),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.sanitize_info(ydl.extract_info(self.url, download=False))

    def download_images(self) -> str | None:
        """Картинки поста-галереи (Instagram/Twitter)."""
        urls = dl.collect_images(self.image_info())
        if not urls:
            self.error = "В посте не найдено изображений"
            return None
        self.status("downloading")
        # Номер с ведущим нулём — файлы идут в порядке карусели.
        saved = self.save_images([(u, f"{i:02d}") for i, u in enumerate(urls, 1)])
        if saved == 0:
            self.error = self.error or "Не удалось скачать изображения"
            return None
        self.bundle = saved > 1
        files = dl.task_files(self.id)
        return str(files[0]) if files else None

    def save_images(self, items: list[tuple[str, str]], total: int = 0) -> int:
        """Скачать картинки как <id>.<метка>.<ext>. total — уже занятый
        задачей объём (общий предел размера). Возвращает, сколько сохранено."""
        from curl_cffi import requests as cffi
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        saved = 0
        for n, (u, tag) in enumerate(items, 1):
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
            p = config.DOWNLOAD_DIR / f"{self.id}.{tag}.{ext}"
            try:
                p.write_bytes(data)
                saved += 1
            except OSError:
                pass
            if not self.playlist:
                self.emit(ev="progress", percent=round(n / len(items) * 100, 1),
                          speed=None, eta=None, total=total)
        return saved


def _fmt_gb(mb: int) -> str:
    gb = mb / 1024
    return f"{gb:g} ГБ" if gb >= 1 else f"{mb} МБ"


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


def bundle_playlist(tid: str, title: str, titles: dict[int, str]) -> tuple[str | None, int]:
    """Архив плейлиста: «001 - Название.mp4» по номеру в плейлисте.
    Возвращает (путь к ZIP, сколько роликов в нём)."""
    import re
    files = dl.task_files(tid)
    if not files:
        return None, 0
    zpath = config.DOWNLOAD_DIR / f"{tid}.zip"
    used: set[str] = set()
    indices: set[int] = set()
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        for p in files:
            m = re.match(rf"{tid}\.(\d+)\.(.+)$", p.name)
            if m:
                idx = int(m.group(1))
                if idx == 0:                   # файл уровня плейлиста — не ролик
                    continue
                indices.add(idx)
                ext = "." + m.group(2).rsplit(".", 1)[-1]
                name = dl.pretty_filename(titles.get(idx) or f"ролик {idx}", "")
                arc = f"{idx:03d} - {name}{ext}"
            else:
                arc = dl.pretty_filename(title, p.suffix)
            stem, ext_ = os.path.splitext(arc)
            n = 1
            while arc in used:
                n += 1
                arc = f"{stem} ({n}){ext_}"
            used.add(arc)
            z.write(p, arcname=arc)
    for p in files:
        try:
            p.unlink()
        except OSError:
            pass
    return str(zpath), len(indices)


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
    if racefd.STATS["conn_fail"] >= 20:
        # Без ссылки: только домен и виды ошибок — чтобы видеть, что именно
        # мешает (HTTP-отказ, таймаут, обрыв) на прямом пути и через egress.
        logging.warning("racefd %s: удачных %d, неудач %d: %s", dl._host_of(job.url),
                        racefd.STATS["conn_ok"], racefd.STATS["conn_fail"],
                        dict(sorted(racefd.FAIL_KINDS.items(), key=lambda kv: -kv[1])[:6]))
    # Метрики движка для статистики производительности (без ссылок).
    engine = ("images" if job.images_mode
              else "racefd" if racefd.STATS["files"] else "ytdlp")
    result["metrics"] = {"engine": engine, "mirrors": racefd.STATS["mirrors"],
                         "conn_ok": racefd.STATS["conn_ok"],
                         "conn_fail": racefd.STATS["conn_fail"],
                         "egress_bytes": racefd.STATS["egress_bytes"]}
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


def _force_ipv4() -> None:
    """Только IPv4 в процессе загрузки. У хоста нет IPv6-связности, а через
    egress-пул (SOCKS с резолвом у нас) IPv6-адрес уводил узел к Google по его
    медленному IPv6-пути: ~0,2 МБ/с против 3-4 МБ/с по IPv4."""
    import socket
    orig = socket.getaddrinfo

    def v4(host, port, family=0, *args, **kwargs):
        if family in (0, socket.AF_UNSPEC):
            family = socket.AF_INET
        return orig(host, port, family, *args, **kwargs)
    socket.getaddrinfo = v4


def main() -> int:
    # Канал событий — отдельный дескриптор, а stdout процесса уводим в
    # /dev/null: всё, что случайно напечатает yt-dlp или библиотека, не
    # должно ни ломать протокол, ни утекать в журнал вместе со ссылкой.
    out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    logging.basicConfig(level=logging.WARNING, format="[job] %(message)s")
    _force_ipv4()
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
