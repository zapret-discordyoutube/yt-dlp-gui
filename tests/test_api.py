"""HTTP-слой. Сеть не используется: всё, что ходило бы наружу, подменяется."""
import pytest

import config


def test_security_headers_present(client):
    r = client.get("/healthz")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_readyz_reports_checks(client):
    r = client.get("/readyz")
    body = r.get_json()
    assert r.status_code in (200, 503)
    assert set(body["checks"]) >= {"ffmpeg", "stats_db", "disk_free",
                                   "disk_quota", "janitor", "queue"}
    assert "free_disk_mb" in body["metrics"]


def test_readyz_fails_when_disk_full(client, monkeypatch):
    """Признак деградации должен быть виден мониторингу, а не только людям."""
    import app as app_module
    real = app_module.manager.health()
    monkeypatch.setattr(app_module.manager, "health",
                        lambda: {**real, "free_disk_mb": 1})
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.get_json()["checks"]["disk_free"] is False


def test_task_list_endpoint_is_gone(client):
    """Отдавал задачи всех пользователей и позволял забрать чужой файл."""
    assert client.get("/api/tasks").status_code == 404


def test_unknown_task(client):
    assert client.get("/api/tasks/" + "0" * 32).status_code == 404
    assert client.get("/api/tasks/" + "0" * 32 + "/file").status_code == 404
    assert client.post("/api/tasks/" + "0" * 32 + "/cancel").status_code == 404


# --- валидация ввода --------------------------------------------------------

@pytest.mark.parametrize("endpoint", ["/api/info", "/api/downloads"])
def test_private_address_rejected_identically(client, endpoint, monkeypatch):
    """Раньше одна ручка говорила «Этот адрес недоступен», а вторая глотала
    код и отвечала «Плохая ссылка»."""
    r = client.post(endpoint, json={"url": "http://127.0.0.1:8090/x"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "Этот адрес недоступен"


@pytest.mark.parametrize("endpoint", ["/api/info", "/api/downloads"])
def test_bad_scheme_rejected(client, endpoint):
    r = client.post(endpoint, json={"url": "file:///etc/passwd"})
    assert r.status_code == 400
    assert "http" in r.get_json()["error"]


def test_empty_body_does_not_crash(client):
    for ep in ("/api/info", "/api/downloads"):
        assert client.post(ep, json={}).status_code == 400
        assert client.post(ep, data="мусор").status_code == 400


def test_bad_format_rejected(client, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.dl, "validate_url", lambda u: u)
    r = client.post("/api/downloads", json={
        "url": "https://example.com/v", "format_id": "all"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "Недопустимый формат"


def test_unknown_codec_message_is_specific(client, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.dl, "validate_url", lambda u: u)
    r = client.post("/api/downloads", json={
        "url": "https://example.com/v", "kind": "video", "vcodec": "theora"})
    assert r.get_json()["error"] == "Неизвестный видеокодек"


# --- ограничение частоты ----------------------------------------------------

def test_rate_limit_applies(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_MAX_INFO", 3)
    codes = [client.post("/api/info", json={"url": "file:///x"}).status_code
             for _ in range(5)]
    assert codes.count(429) >= 1
    assert codes[0] == 400          # первые проходят валидацию, а не лимит


def test_rate_limit_map_does_not_grow_forever(client, monkeypatch):
    """Ключи словаря не удалялись никогда: перебор адресов выедал память."""
    import app as app_module
    monkeypatch.setattr(config, "RATE_WINDOW_SEC", 0)
    for i in range(50):
        app_module._hits[f"info:10.0.0.{i}"].append(0.0)
    app_module._hits_last_gc = 0.0
    with app_module._hits_lock:
        app_module._gc_hits(10_000.0)
    assert len(app_module._hits) == 0


# --- лента ------------------------------------------------------------------

def test_feed_survives_garbage_params(client):
    for qs in ("?offset=abc", "?offset=-5", "?order=мусор", "?offset=999999"):
        r = client.get("/api/feed" + qs)
        assert r.status_code == 200
        assert "items" in r.get_json()


def test_feed_and_stats_are_cacheable(client):
    """Частая перезагрузка страницы не должна доходить до сервера."""
    for ep in ("/api/feed", "/api/stats"):
        assert "max-age" in client.get(ep).headers.get("Cache-Control", "")


def test_stats_page_renders_with_inlined_data(client):
    """Данные вшиты в страницу — иначе мигают прочерки."""
    html = client.get("/stats").get_data(as_text=True)
    assert "const INITIAL" in html
    assert '"feed"' in html


def test_index_renders(client):
    html = client.get("/").get_data(as_text=True)
    assert "yt-grab" in html
    # шрифт должен быть свой, без обращения к Google
    assert "fonts.googleapis.com" not in html


# --- выдача файла -----------------------------------------------------------

def _make_task(status="finished", filename=None, display=None):
    import app as app_module
    import downloader as dl
    task = dl.Task(id="t" * 32, url="https://example.com/v", title="Ролик",
                   fmt="best", extra={}, label="тест")
    task.status = status
    task.filename = filename
    task.display_name = display
    task.filesize = 5
    with app_module.manager.lock:
        app_module.manager.tasks[task.id] = task
    return task


def test_file_not_ready(client):
    t = _make_task(status="downloading")
    r = client.get(f"/api/tasks/{t.id}/file")
    assert r.status_code == 409


def test_file_already_served(client):
    t = _make_task(status="served")
    r = client.get(f"/api/tasks/{t.id}/file")
    assert r.status_code == 410
    assert "заново" in r.get_json()["error"]


def test_file_vanished_from_disk(client):
    t = _make_task(filename="нет-такого.mp4")
    assert client.get(f"/api/tasks/{t.id}/file").status_code == 410


def test_file_served_with_cyrillic_name(client):
    """Кириллица в имени должна уехать через RFC 5987, иначе браузер
    сохранит файл с мусорным именем."""
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    disk = config.DOWNLOAD_DIR / "aaaa.mp4"
    disk.write_bytes(b"12345")
    t = _make_task(filename="aaaa.mp4", display="Ролик про кота.mp4")
    try:
        r = client.get(f"/api/tasks/{t.id}/file")
        assert r.status_code == 200
        cd = r.headers["Content-Disposition"]
        assert "filename*=UTF-8''" in cd
        assert "%D0%A0" in cd            # 'Р' в процентном кодировании
    finally:
        disk.unlink(missing_ok=True)


def test_playlist_archive_only_for_youtube_lists(client):
    r = client.post("/api/downloads", json={"url": "https://www.youtube.com/watch?v=x",
                                            "playlist": True, "kind": "audio"})
    assert r.status_code == 400
    assert "плейлист" in r.get_json()["error"]
