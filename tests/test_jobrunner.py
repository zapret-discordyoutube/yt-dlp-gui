"""Исполнитель загрузок в отдельном процессе: итог, отмена, сторож зависаний.

Сеть наружу не нужна: источник — локальный сокет, который либо закрыт
(ошибка сразу), либо принимает соединение и молчит (зависший сервер).
"""
import shutil
import socket
import threading
import time

import pytest

import config
import downloader as dl


@pytest.fixture
def manager():
    return dl.DownloadManager()


@pytest.fixture
def silent_server():
    """Принимает соединения и ничего не отвечает — как зависший CDN."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    held = []
    stop = threading.Event()

    def loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                held.append(c)
            except OSError:
                continue
    threading.Thread(target=loop, daemon=True).start()
    yield f"http://127.0.0.1:{srv.getsockname()[1]}/video.mp4"
    stop.set()
    for c in held:
        c.close()
    srv.close()


def _closed_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/video.mp4"


def _task(url, tid):
    return dl.Task(id=tid, url=url, title="Ролик", fmt="best", extra={}, label="l")


def _run_async(m, t):
    th = threading.Thread(target=m._run, args=(t,), daemon=True)
    th.start()
    return th


def test_error_is_reported_not_crashed(manager):
    t = _task(_closed_port_url(), "1" * 32)
    manager._run(t)
    assert t.status == "error"
    assert t.error and "Внутренняя" not in t.error
    assert t.completed


def test_cancel_stops_hung_job(manager, silent_server, monkeypatch):
    """Зависший источник не вызывает хук прогресса — мягкая отмена не
    срабатывает, и процесс обязан быть остановлен принудительно."""
    monkeypatch.setattr(config, "CANCEL_GRACE_SEC", 1)
    t = _task(silent_server, "2" * 32)
    th = _run_async(manager, t)
    deadline = time.time() + 20
    while t.status != "preparing" and time.time() < deadline:
        time.sleep(0.05)
    assert t.status == "preparing", "исполнитель не сообщил о подготовке"
    with manager.lock:
        manager.tasks[t.id] = t
    t0 = time.time()
    assert manager.cancel(t.id)
    # Отмена видна сразу, ещё до остановки процесса.
    assert t.status == "cancelled"
    th.join(timeout=15)
    assert not th.is_alive(), "задача не остановилась после отмены"
    assert time.time() - t0 < 3, "отмена зависшей загрузки заняла слишком долго"
    assert t.status == "cancelled"


def test_stall_watchdog_fails_task(manager, silent_server, monkeypatch):
    monkeypatch.setattr(config, "STALL_SEC", 2)
    monkeypatch.setattr(config, "CANCEL_GRACE_SEC", 1)
    t = _task(silent_server, "3" * 32)
    th = _run_async(manager, t)
    th.join(timeout=20)
    assert not th.is_alive(), "сторож не снял зависшую задачу"
    assert t.status == "error"
    assert "перестал отдавать" in t.error
    assert not dl.task_files(t.id), "после сбоя остался мусор"


def test_finished_filename_must_belong_to_task(manager, monkeypatch, tmp_path):
    """Имя файла от исполнителя проверяется: чужой файл не выдаём."""
    other = config.DOWNLOAD_DIR / ("f" * 32 + ".mp4")
    other.write_bytes(b"x")
    monkeypatch.setattr(manager, "_execute",
                        lambda task: {"status": "finished", "filename": other.name})
    t = _task("https://example.com/v", "4" * 32)
    manager._run(t)
    assert t.status == "error"
    other.unlink()


def test_metrics_are_filled(manager):
    t = _task(_closed_port_url(), "5" * 32)
    manager._run(t)
    m = t.metrics
    assert m["engine"] == "ytdlp"
    assert m["total_ms"] is not None and m["queue_ms"] is not None
    assert not any(k.startswith("_") for k in m), "служебные отметки утекли"


def test_youtube_prefers_https_formats():
    """HLS-форматы YouTube висли на заблокированном сервере: при равном
    разрешении берём https (его качает racefd со сменой сервера)."""
    import io
    import jobrunner
    yt = jobrunner.Job({"id": "6" * 32, "url": "https://www.youtube.com/watch?v=x",
                        "fmt": "bv*+ba/b"}, io.StringIO())
    other = jobrunner.Job({"id": "7" * 32, "url": "https://vimeo.com/1",
                           "fmt": "bv*+ba/b"}, io.StringIO())
    fs = yt.ydl_opts()["format_sort"]
    assert fs.index("res") < fs.index("proto:https")
    assert "format_sort" not in other.ydl_opts()


def test_youtube_uses_own_source_ip(monkeypatch):
    """YouTube напрямую — с отдельного IP; прочие сайты и egress — нет."""
    import io
    import jobrunner
    monkeypatch.setattr(config, "YT_SOURCE_IP", "203.0.113.9")
    yt = jobrunner.Job({"id": "8" * 32, "url": "https://www.youtube.com/watch?v=x"}, io.StringIO())
    other = jobrunner.Job({"id": "9" * 32, "url": "https://vimeo.com/1"}, io.StringIO())
    via_egress = jobrunner.Job({"id": "a" * 32, "url": "https://www.youtube.com/watch?v=x",
                                "proxy": "socks5://127.0.0.1:18110"}, io.StringIO())
    assert yt.ydl_opts()["source_address"] == "203.0.113.9"
    assert "source_address" not in other.ydl_opts()
    assert "source_address" not in via_egress.ydl_opts()


def test_racefd_direct_session_binds_source_ip(monkeypatch):
    import racefd
    monkeypatch.setattr(config, "YT_SOURCE_IP", "203.0.113.9")
    s = racefd._direct_session()
    ad = s.get_adapter("https://rr1---sn-x.googlevideo.com/videoplayback")
    assert isinstance(ad, racefd._SourceAdapter) and ad._source_ip == "203.0.113.9"
    monkeypatch.setattr(config, "YT_SOURCE_IP", "")
    assert not isinstance(racefd._direct_session().get_adapter("https://x/"), racefd._SourceAdapter)


def test_bundle_playlist_names_and_skips_playlist_files(tmp_path, monkeypatch):
    """Архив плейлиста: «001 - Название.ext» по номеру, файлы уровня
    плейлиста (номер 0) не берутся и не считаются роликами."""
    import zipfile
    import jobrunner
    tid = "b" * 32
    for name in (f"{tid}.001.opus", f"{tid}.002.opus", f"{tid}.000.jpg"):
        (config.DOWNLOAD_DIR / name).write_bytes(b"x")
    path, n = jobrunner.bundle_playlist(tid, "Плейлист", {1: "Первый: урок", 2: "Второй"})
    names = zipfile.ZipFile(path).namelist()
    assert n == 2
    assert names == ["001 - Первый урок.opus", "002 - Второй.opus"]
    import os
    os.unlink(path)
    (config.DOWNLOAD_DIR / f"{tid}.000.jpg").unlink(missing_ok=True)


def test_second_track_is_downloading_not_processing():
    """Видео докачано — «обработка»; начался звук — снова «скачивание»:
    иначе зависший звук сторож ловил бы по пределу склейки (час)."""
    import io
    import json as _json
    import jobrunner
    out = io.StringIO()
    job = jobrunner.Job({"id": "t", "url": "https://www.youtube.com/watch?v=x"}, out)
    hook = job.make_hook()
    hook({"status": "downloading", "filename": "v", "downloaded_bytes": 1, "total_bytes": 10})
    hook({"status": "finished", "filename": "v", "downloaded_bytes": 10, "total_bytes": 10})
    hook({"status": "downloading", "filename": "a", "downloaded_bytes": 0, "total_bytes": 10,
          "ytg_phase": "connect"})
    hook({"status": "downloading", "filename": "a", "downloaded_bytes": 5, "total_bytes": 10})
    statuses = [e["status"] for e in map(_json.loads, out.getvalue().splitlines())
                if e["ev"] == "status"]
    assert statuses == ["downloading", "processing", "downloading"]


def test_split_tracks_picks_by_cascade_then_downloads_separately():
    """«Не объединять» при каскаде: форматы выбраны по каскаду, дорожки
    качаются раздельно, имена файлов — с номером формата."""
    import io
    import yt_dlp
    import jobrunner
    v = {"format_id": "137", "url": "https://x/v", "ext": "mp4", "protocol": "https",
         "vcodec": "avc1", "acodec": "none", "height": 1080}
    a = {"format_id": "251", "url": "https://x/a", "ext": "webm", "protocol": "https",
         "vcodec": "none", "acodec": "opus", "abr": 130}
    info = {"id": "v", "title": "T", "extractor": "youtube", "extractor_key": "Youtube",
            "webpage_url": "https://www.youtube.com/watch?v=v", "formats": [a, v]}
    job = jobrunner.Job({"id": "t", "url": info["webpage_url"],
                         "fmt": "bestvideo+bestaudio/best",
                         "extra": {dl.SPLIT_KEY: True}}, io.StringIO())
    opts = job.ydl_opts()
    assert job.split and dl.SPLIT_KEY not in opts
    assert "%(format_id)s" in opts["outtmpl"]["default"]
    with yt_dlp.YoutubeDL({"format": job.fmt, "quiet": True, "simulate": True}) as ydl:
        clean = job.split_tracks(ydl, info)
        assert "requested_formats" not in clean
        res = ydl.process_ie_result(clean, download=True)
    assert [d["format_id"] for d in res["requested_downloads"]] == ["137", "251"]


def _yt_clip_job(**spec):
    import io
    import jobrunner
    return jobrunner.Job({"id": "c" * 32, "url": "https://www.youtube.com/watch?v=x",
                          "clip": [60, 300], **spec}, io.StringIO())


def test_youtube_clip_source_has_own_size_cap(monkeypatch):
    """Отрезок с YouTube на откате (HLS) режется из целого ролика: исходник
    больше MAX_FILESIZE_MB — не повод обрывать, обрыв только на своём потолке."""
    import pytest
    import yt_dlp
    monkeypatch.setattr(config, "MAX_FILESIZE_MB", 10)
    monkeypatch.setattr(config, "CLIP_SOURCE_MAX_MB", 40)
    job = _yt_clip_job()
    assert job.ydl_opts()["max_filesize"] == 40 * 1048576
    job.trim_after = True                   # SectionPP откатился на целый ролик
    hook = job.make_hook()
    hook({"status": "downloading", "filename": "v", "downloaded_bytes": 30 * 1048576})
    with pytest.raises(yt_dlp.utils.DownloadCancelled):
        hook({"status": "downloading", "filename": "a", "downloaded_bytes": 11 * 1048576})
    assert "целиком" in job.error
    # Без отрезка — прежний потолок.
    import io
    import jobrunner
    plain = jobrunner.Job({"id": "d" * 32, "url": "https://www.youtube.com/watch?v=x"},
                          io.StringIO())
    with pytest.raises(yt_dlp.utils.DownloadCancelled):
        plain.make_hook()({"status": "downloading", "filename": "v",
                           "downloaded_bytes": 11 * 1048576})


def test_youtube_clip_too_big_source_refused_before_download(monkeypatch, tmp_path):
    """Дорожки не для RaceFD (откат на целый ролик): размер целого известен
    из разбора — отказ сразу, без скачивания."""
    import pytest
    import yt_dlp
    monkeypatch.setattr(config, "CLIP_SOURCE_MAX_MB", 40)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path)
    v = {"format_id": "137", "url": "https://x/v", "ext": "mp4", "protocol": "https",
         "vcodec": "avc1", "acodec": "none", "height": 1080, "filesize": 35 * 1048576}
    a = {"format_id": "140", "url": "https://x/a", "ext": "m4a", "protocol": "https",
         "vcodec": "none", "acodec": "mp4a", "abr": 128, "filesize": 8 * 1048576}
    info = {"id": "x", "title": "T", "extractor": "youtube", "extractor_key": "Youtube",
            "webpage_url": "https://www.youtube.com/watch?v=x", "formats": [a, v]}
    job = _yt_clip_job(fmt="bestvideo+bestaudio/best", info=info)
    with yt_dlp.YoutubeDL({"format": job.fmt, "quiet": True, "simulate": True}) as ydl:
        with pytest.raises(yt_dlp.utils.DownloadCancelled):
            job.check_clip_source(ydl, info)
        assert "целиком" in job.error
        # Влезает в потолок, но не в квоту с чужими файлами — тоже отказ.
        job.error = None
        monkeypatch.setattr(config, "CLIP_SOURCE_MAX_MB", 100)
        monkeypatch.setattr(config, "DISK_QUOTA_MB", 50)
        (tmp_path / ("e" * 32 + ".mp4")).write_bytes(b"\0" * 10 * 1048576)
        with pytest.raises(yt_dlp.utils.DownloadCancelled):
            job.check_clip_source(ydl, info)
        assert "места" in job.error
        # Своё место есть — пропускаем.
        job.error = None
        monkeypatch.setattr(config, "DISK_QUOTA_MB", 100)
        job.check_clip_source(ydl, info)
        assert job.error is None


def test_failed_trim_does_not_serve_oversized_source(monkeypatch, tmp_path):
    """Обрезка не удалась, а целый ролик больше MAX_FILESIZE_MB — ошибка,
    а не отдача исходника в обход лимита."""
    import jobrunner
    monkeypatch.setattr(config, "MAX_FILESIZE_MB", 1)
    src = tmp_path / ("c" * 32 + ".mp4")
    src.write_bytes(b"\0" * 2 * 1048576)
    job = _yt_clip_job()
    job.trim_after = True
    monkeypatch.setattr(job, "download_media", lambda: str(src))
    monkeypatch.setattr(jobrunner, "trim_file", lambda path, clip: None)
    res = job.run()
    assert res["status"] == "error" and "превышает" in res["error"]


def _gv_info(video_mb: int, audio_mb: int, duration: int = 3600) -> dict:
    gv = "https://rr1---sn-x.googlevideo.com/videoplayback?clen="
    v = {"format_id": "137", "url": gv + str(video_mb << 20), "ext": "mp4", "protocol": "https",
         "vcodec": "avc1", "acodec": "none", "height": 1080, "filesize": video_mb << 20}
    a = {"format_id": "140", "url": gv + str(audio_mb << 20), "ext": "m4a", "protocol": "https",
         "vcodec": "none", "acodec": "mp4a", "abr": 128, "filesize": audio_mb << 20}
    return {"id": "x", "title": "T", "extractor": "youtube", "extractor_key": "Youtube",
            "duration": duration, "webpage_url": "https://www.youtube.com/watch?v=x",
            "formats": [a, v]}


def test_youtube_clip_by_demand_checks_clip_not_whole_source(monkeypatch, tmp_path):
    """Отрезок «по запросу»: ролик в 5 ГБ — не помеха, считается доля
    отрезка (4 минуты из часа); слишком длинный отрезок — отказ сразу."""
    import pytest
    import yt_dlp
    monkeypatch.setattr(config, "CLIP_SOURCE_MAX_MB", 4096)
    monkeypatch.setattr(config, "MAX_FILESIZE_MB", 2048)
    monkeypatch.setattr(config, "DISK_QUOTA_MB", 5120)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path)
    info = _gv_info(5000, 100)
    job = _yt_clip_job(fmt="bestvideo+bestaudio/best", info=info)
    assert job.clip == (60.0, 300.0)
    with yt_dlp.YoutubeDL({"format": job.fmt, "quiet": True, "simulate": True}) as ydl:
        job.check_clip_source(ydl, info)
        assert job.error is None
        long = _yt_clip_job(fmt="bestvideo+bestaudio/best", info=info, clip=[0, 3000])
        with pytest.raises(yt_dlp.utils.DownloadCancelled):
            long.check_clip_source(ydl, info)
        assert "Отрезок превышает" in long.error


def test_youtube_clip_uses_ranges_without_reencode():
    """YouTube: отрезок — download_ranges без перекодирования (режет FFmpegFD
    через SectionSession); обрезка после скачивания — только на откате."""
    job = _yt_clip_job()
    opts = job.ydl_opts()
    assert "download_ranges" in opts and opts["force_keyframes_at_cuts"] is False
    assert job.trim_after is False


# --- «Для старого ТВ» -------------------------------------------------------

def test_tv_bitrate_fits_size_limit(monkeypatch):
    import jobrunner
    monkeypatch.setattr(config, "MAX_FILESIZE_MB", 2048)
    assert jobrunner.tv_video_kbps(300) == jobrunner.TV_MAX_VIDEO_KBPS
    assert jobrunner.tv_video_kbps(None) == jobrunner.TV_MAX_VIDEO_KBPS
    for dur in (3600, 4 * 3600, 20 * 3600):
        kbps = jobrunner.tv_video_kbps(dur)
        total_mb = (kbps + jobrunner.TV_AUDIO_KBPS) * dur / 8192
        assert kbps >= jobrunner.TV_MIN_VIDEO_KBPS
        assert total_mb <= 2048 or kbps == jobrunner.TV_MIN_VIDEO_KBPS


def test_tv_cmd_uses_profile_checked_on_the_tv():
    import jobrunner
    cmd = jobrunner.tv_cmd("in.mkv", "out.mkv", 3000)
    joined = " ".join(cmd)
    assert "-profile:v high" in joined and "-level:v 4.0" in joined
    assert "-ar 48000" in joined and "-c:a aac" in joined
    assert "-maxrate 3000k" in joined
    assert cmd[-1] == "out.mkv"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")
def test_transcode_tv_produces_playable_mkv(tmp_path):
    """Живой прогон: 1080p с звуком 44,1 кГц -> 720p High, AAC 48 кГц, MKV."""
    import io
    import json
    import subprocess
    import jobrunner
    src = tmp_path / ("a" * 32 + ".mp4")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=25:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
         "-c:v", "libx264", "-c:a", "aac", str(src)], check=True)
    job = jobrunner.Job({"id": "a" * 32, "url": "https://example.com/v"}, io.StringIO())
    out = job.transcode_tv(str(src))
    assert out and out.endswith(".tv.mkv") and not src.exists()
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_name,profile,height,sample_rate", "-of", "json", out],
        capture_output=True, text=True, check=True).stdout)
    v = next(s for s in info["streams"] if s["codec_name"] == "h264")
    a = next(s for s in info["streams"] if s["codec_name"] == "aac")
    assert v["profile"] == "High" and v["height"] == 720
    assert a["sample_rate"] == "48000"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")
def test_transcode_tv_does_not_upscale_and_skips_audio_only(tmp_path):
    import io
    import subprocess
    import jobrunner
    small = tmp_path / ("b" * 32 + ".mp4")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=1",
         "-c:v", "libx264", str(small)], check=True)
    job = jobrunner.Job({"id": "b" * 32, "url": "https://example.com/v"}, io.StringIO())
    out = job.transcode_tv(str(small))
    assert subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=height", "-of", "csv=p=0", out],
        capture_output=True, text=True).stdout.strip() == "360"
    music = tmp_path / ("c" * 32 + ".m4a")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "aac", str(music)],
        check=True)
    job2 = jobrunner.Job({"id": "c" * 32, "url": "https://example.com/v"}, io.StringIO())
    assert job2.transcode_tv(str(music)) == str(music) and music.exists()


def test_tv_flag_is_taken_out_of_ytdlp_opts():
    import io
    import jobrunner
    job = jobrunner.Job({"id": "d" * 32, "url": "https://example.com/v",
                         "extra": {dl.TV_KEY: True, "merge_output_format": "mkv"}},
                        io.StringIO())
    assert job.tv is True and dl.TV_KEY not in job.extra
