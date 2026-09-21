"""Общая подготовка тестов.

ВАЖНО: переменные окружения выставляются ДО импорта config и app.
`import app` на уровне модуля создаёт менеджер загрузок, который при старте
подметает каталог загрузок. Если не перенаправить пути заранее, прогон
тестов удалит файлы работающего сервиса — так уже случалось.
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ytg-tests-"))
os.environ["YTG_DOWNLOAD_DIR"] = str(_TMP / "downloads")
os.environ["YTG_DATA_DIR"] = str(_TMP / "data")
os.environ["YTG_TRUST_PROXY"] = "0"
os.environ["YTG_JANITOR_INTERVAL_SEC"] = "3600"   # уборщик не мешает тестам
os.environ["YTG_MAX_CONCURRENT"] = "2"
os.environ["YTG_QUEUE_MAX"] = "4"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import config  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _guard_paths():
    """Страховка: тесты не должны работать с боевыми каталогами."""
    assert str(config.DOWNLOAD_DIR).startswith(str(_TMP)), config.DOWNLOAD_DIR
    assert str(config.DATA_DIR).startswith(str(_TMP)), config.DATA_DIR
    yield


@pytest.fixture
def client():
    import app as app_module
    app_module.app.config["TESTING"] = True
    app_module._hits.clear()          # чистый rate limit на каждый тест
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def fake_info():
    """Метаданные в том виде, в каком их отдают экстракторы.

    Специально смешаны три случая: кодеки известны, кодеков нет совсем
    ("none") и кодеки неизвестны (None) — последний ломался.
    """
    return {
        "id": "abc123",
        "title": "Тестовый ролик",
        "uploader": "Автор",
        "duration": 120,
        "thumbnail": "https://example.com/t.jpg",
        "webpage_url": "https://example.com/watch?v=abc123",
        "extractor_key": "Test",
        "formats": [
            {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028",
             "acodec": "none", "height": 1080, "tbr": 4000},
            {"format_id": "140", "ext": "m4a", "vcodec": "none",
             "acodec": "mp4a.40.2", "height": None, "abr": 128},
            {"format_id": "hls-480", "ext": "mp4", "vcodec": None,
             "acodec": None, "height": 480, "tbr": 1500},
            {"format_id": "mp4-low", "ext": "mp4", "vcodec": None,
             "acodec": None, "height": None, "tbr": 500},
            {"format_id": "sb0", "ext": "mhtml", "vcodec": "none",
             "acodec": "none", "format_note": "storyboard"},
        ],
    }
