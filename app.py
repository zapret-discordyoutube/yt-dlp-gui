"""yt-dlp GUI — веб-обёртка. Сервер не хранит историю и настройки пользователя."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque

from flask import (Flask, Response, jsonify, render_template, request,
                   send_file, stream_with_context)
from werkzeug.middleware.proxy_fix import ProxyFix

import config
import downloader as dl
import stats

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

if config.TRUST_PROXY:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

if config.QUIET_ACCESS_LOG:
    # не пишем URL/IP пользователей в лог
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

manager = dl.DownloadManager()
stats.init()


def _task_finished(task) -> None:
    """Считаем исход задачи. Пишем только числа, без URL и заголовков."""
    if task.status == "finished":
        stats.bump("downloads_done")
        stats.record_download(task.url)
    elif task.status == "error":
        stats.bump("downloads_failed")


manager.on_complete = _task_finished

# --- простой in-memory rate limit по IP ---
_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


def _client_ip() -> str:
    return request.remote_addr or "unknown"


def rate_ok(bucket: str, limit: int) -> bool:
    key = f"{bucket}:{_client_ip()}"
    now = time.time()
    with _hits_lock:
        q = _hits[key]
        while q and q[0] < now - config.RATE_WINDOW_SEC:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


@app.get("/")
def index():
    return render_template("index.html", limits={
        "max_filesize_mb": config.MAX_FILESIZE_MB,
        "max_duration_sec": config.MAX_DURATION_SEC,
        "file_ttl_minutes": config.FILE_TTL_MINUTES,
    })


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "version": dl.yt_dlp.version.__version__})


@app.post("/api/info")
def api_info():
    if not rate_ok("info", config.RATE_MAX_INFO):
        return err("Слишком много запросов, подождите немного", 429)
    data = request.get_json(silent=True) or {}
    try:
        url = dl.validate_url(data.get("url", ""))
    except ValueError as e:
        return err({"empty_or_too_long": "Пустая или слишком длинная ссылка",
                    "private_host": "Этот адрес недоступен",
                    "bad_scheme": "Нужна ссылка http:// или https://"}.get(str(e), "Плохая ссылка"))
    try:
        info = dl.probe(url)
        stats.bump("api_info")
        return jsonify(info)
    except ValueError as e:
        if str(e) == "too_long":
            return err(f"Слишком длинное видео (лимит {config.MAX_DURATION_SEC // 3600} ч)")
        return err("Не удалось разобрать ссылку")
    except dl.yt_dlp.utils.DownloadError as e:
        return err(dl._clean_err(str(e)))
    except Exception:
        return err("Не удалось получить информацию о видео", 502)


@app.post("/api/downloads")
def api_download():
    if not rate_ok("download", config.RATE_MAX_DOWNLOAD):
        return err("Слишком много загрузок, подождите немного", 429)
    if manager.quota_exceeded():
        return err("На сервере временно нет места, попробуйте позже", 503)

    data = request.get_json(silent=True) or {}
    try:
        url = dl.validate_url(data.get("url", ""))
    except ValueError:
        return err("Плохая ссылка")

    try:
        if data.get("format_id"):
            fmt, extra, label = dl.build_from_format_id(
                str(data["format_id"]), str(data.get("container", "auto")))
        else:
            fmt, extra, label = dl.build_format(
                kind=str(data.get("kind", "video")),
                height=str(data.get("height", "auto")),
                acodec=str(data.get("acodec", "best")),
                vcodec=str(data.get("vcodec", "auto")),
                vaudio=str(data.get("vaudio", "auto")),
            )
    except ValueError as e:
        return err({"bad_format_id": "Недопустимый формат",
                    "unknown_acodec": "Неизвестный аудиокодек",
                    "unknown_vcodec": "Неизвестный видеокодек",
                    "unknown_vaudio": "Неизвестный формат звука",
                    "unknown_height": "Неизвестное разрешение",
                    "unknown_kind": "Неизвестный тип"}.get(str(e), "Неверный выбор формата"))

    title = str(data.get("title") or "Видео")[:200]
    thumb = data.get("thumbnail")
    if thumb and not str(thumb).startswith(("http://", "https://")):
        thumb = None

    task = manager.create(url=url, fmt=fmt, extra=extra, label=label,
                          title=title, thumbnail=thumb)
    stats.bump("downloads_started")
    return jsonify(task.public()), 201


@app.get("/api/tasks")
def api_tasks():
    return jsonify(manager.list())


@app.get("/api/tasks/<tid>")
def api_task(tid):
    t = manager.get(tid)
    return jsonify(t.public()) if t else err("Задача не найдена", 404)


@app.post("/api/tasks/<tid>/cancel")
def api_cancel(tid):
    return (jsonify({"cancelled": True}) if manager.cancel(tid)
            else err("Нечего отменять", 409))


@app.get("/api/tasks/<tid>/progress")
def api_progress(tid):
    if not manager.get(tid):
        return err("Задача не найдена", 404)

    def gen():
        last = None
        # Поток держится всё время загрузки, поэтому ограничиваем его срок:
        # иначе несколько зависших соединений исчерпают пул gunicorn.
        # Браузер переподключит EventSource автоматически.
        deadline = time.time() + config.SSE_MAX_SECONDS
        while time.time() < deadline:
            t = manager.get(tid)
            if not t:
                yield f"data: {json.dumps({'status': 'gone'})}\n\n"
                return
            payload = json.dumps(t.public(), ensure_ascii=False)
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
            if t.status in ("finished", "error", "cancelled"):
                return
            time.sleep(0.5)
        # мягкий разрыв: клиент переподключится и продолжит следить
        yield ": timeout\n\n"

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.get("/api/tasks/<tid>/file")
def api_file(tid):
    t = manager.get(tid)
    if not t:
        return err("Задача не найдена", 404)
    if t.status == "served":
        return err("Файл уже удалён с сервера — запустите загрузку заново", 410)
    if t.status != "finished" or not t.filename:
        return err("Файл ещё не готов", 409)
    path = dl.safe_download_path(t.filename)
    if not path:
        return err("Файл удалён с сервера — запустите загрузку заново", 410)

    # Отмечаем момент выдачи; уборщик удалит файл через grace-период.
    # Не используем resp.call_on_close: send_file включает direct_passthrough,
    # и WSGI закрывает файловую обёртку, а не Response — колбэк не сработает.
    resp = send_file(path, as_attachment=True,
                     download_name=t.display_name or t.filename)
    if t.served_at is None:
        # считаем только первую выдачу, чтобы докачка не удваивала цифры
        stats.bump("files_served")
        stats.bump("bytes_served", t.filesize or 0)
    if config.DELETE_AFTER_SERVE and t.served_at is None:
        t.served_at = time.time()
    return resp


@app.get("/api/stats")
def api_stats():
    return jsonify(stats.summary())


@app.get("/api/feed")
def api_feed():
    order = "popular" if request.args.get("order") == "popular" else "recent"
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0
    return jsonify({
        "items": stats.feed(order=order, limit=config.FEED_PAGE_SIZE, offset=offset),
        "totals": stats.feed_totals(),
    })


@app.get("/stats")
def stats_page():
    return render_template("stats.html")


if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT, threaded=True)
