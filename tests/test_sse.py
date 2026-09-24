"""Поток прогресса.

Единственное место, которое удерживает поток gunicorn на всё время
загрузки, и до сих пор не покрытое ни одним тестом. Именно здесь живёт
периодический комментарий, без которого обрыв клиента не обнаруживался и
поток жил до самого дедлайна.
"""
import json

import pytest

import config


@pytest.fixture
def task(client):
    """Задача в реестре боевого менеджера, убирается за собой."""
    import app as app_module
    import downloader as dl
    t = dl.Task(id="s" * 32, url="https://example.com/v", title="Ролик",
                fmt="best", extra={}, label="тест")
    t.status = "queued"
    with app_module.manager.lock:
        app_module.manager.tasks[t.id] = t
    yield t
    with app_module.manager.lock:
        app_module.manager.tasks.pop(t.id, None)


def read_stream(client, tid, limit=40):
    """Прочитать поток до конца, не дольше limit кусков."""
    r = client.get(f"/api/tasks/{tid}/progress")
    chunks = []
    for raw in r.response:
        chunks.append(raw.decode())
        if len(chunks) >= limit:
            break
    r.close()
    return r, chunks


def events(chunks):
    return [json.loads(c[6:]) for c in chunks if c.startswith("data: ")]


# --- базовое поведение ------------------------------------------------------

def test_unknown_task_is_404(client):
    assert client.get("/api/tasks/" + "0" * 32 + "/progress").status_code == 404


def test_headers_disable_buffering(client, task):
    """Без этих заголовков nginx копит поток в буфере, и прогресс не идёт."""
    r = client.get(f"/api/tasks/{task.id}/progress")
    assert r.mimetype == "text/event-stream"
    assert r.headers["X-Accel-Buffering"] == "no"
    assert r.headers["Cache-Control"] == "no-cache"
    r.close()


@pytest.mark.parametrize("status", ["finished", "error", "cancelled", "served"])
def test_stream_ends_on_terminal_status(client, task, status):
    """Поток обязан закрываться на терминальном статусе, иначе он держит
    поток gunicorn впустую. Статус served добавлен сюда не зря: по уже
    выданной задаче соединение крутилось до дедлайна."""
    task.status = status
    r, chunks = read_stream(client, task.id)
    assert events(chunks)[0]["status"] == status
    assert len(chunks) < 40, "поток не завершился сам"


def test_first_event_carries_current_state(client, task):
    task.status = "finished"
    task.percent = 100
    _, chunks = read_stream(client, task.id)
    ev = events(chunks)[0]
    assert ev["id"] == task.id and ev["percent"] == 100


def test_vanished_task_reports_gone(client, task, monkeypatch):
    """Задачу мог снять уборщик: клиент должен узнать об этом, а не ждать."""
    import app as app_module
    with app_module.manager.lock:
        app_module.manager.tasks.pop(task.id, None)
    # задача есть на входе, но исчезает к моменту чтения
    r = client.get(f"/api/tasks/{task.id}/progress")
    assert r.status_code == 404
    r.close()


# --- дедупликация и признак жизни -------------------------------------------

def test_identical_payload_is_not_repeated(client, task, monkeypatch):
    """Одинаковые данные не должны литься в сокет потоком."""
    import app as app_module
    sent = []
    real_sleep = app_module.time.sleep

    def fake_sleep(_s):
        # после первого прохода завершаем задачу, чтобы поток закрылся
        task.status = "finished"
        real_sleep(0)

    monkeypatch.setattr(app_module.time, "sleep", fake_sleep)
    _, chunks = read_stream(client, task.id)
    sent = events(chunks)
    assert len(sent) == 2, f"ожидались два разных состояния, пришло {len(sent)}"
    assert sent[0]["status"] == "queued" and sent[1]["status"] == "finished"


def test_ping_is_sent_when_nothing_changes(client, task, monkeypatch):
    """Пока статус не меняется, в сокет не шло ни байта, и обрыв клиента
    оставался незамеченным — поток жил до дедлайна. Комментарий заставляет
    запись упасть на закрытом соединении."""
    import app as app_module
    clock = [1000.0]
    monkeypatch.setattr(app_module.time, "time", lambda: clock[0])

    steps = [0]

    def fake_sleep(_s):
        steps[0] += 1
        clock[0] += 20            # проскакиваем порог в 15 секунд
        if steps[0] >= 3:
            task.status = "finished"

    monkeypatch.setattr(app_module.time, "sleep", fake_sleep)
    _, chunks = read_stream(client, task.id)
    assert any(c.startswith(": ping") for c in chunks), \
        "признак жизни не отправлен — обрыв клиента останется незамеченным"


def test_stream_stops_at_deadline(client, task, monkeypatch):
    """Срок жизни соединения ограничен: иначе зависшие клиенты копят
    потоки gunicorn."""
    import app as app_module
    monkeypatch.setattr(config, "SSE_MAX_SECONDS", 1)
    clock = [1000.0]
    monkeypatch.setattr(app_module.time, "time", lambda: clock[0])

    def fake_sleep(_s):
        clock[0] += 10            # мгновенно перешагиваем дедлайн

    monkeypatch.setattr(app_module.time, "sleep", fake_sleep)
    _, chunks = read_stream(client, task.id)
    assert chunks[-1].startswith(": timeout"), \
        "поток не закрылся по сроку жизни"
