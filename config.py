"""Конфигурация сервиса. Значения переопределяются переменными окружения."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.environ.get("YTG_DOWNLOAD_DIR", BASE_DIR / "downloads"))
# Каталог для служебных данных (обезличенные счётчики). Вынесен отдельно,
# потому что systemd монтирует остальной проект только на чтение.
DATA_DIR = Path(os.environ.get("YTG_DATA_DIR", BASE_DIR / "data"))

# Сеть
HOST = os.environ.get("YTG_HOST", "127.0.0.1")
PORT = int(os.environ.get("YTG_PORT", "8090"))

# Публичный сервис -> защита от абуза
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("YTG_MAX_CONCURRENT", "3"))
MAX_FILESIZE_MB = int(os.environ.get("YTG_MAX_FILESIZE_MB", "2048"))   # потолок на один файл
MAX_DURATION_SEC = int(os.environ.get("YTG_MAX_DURATION_SEC", str(4 * 3600)))  # 4 часа
DISK_QUOTA_MB = int(os.environ.get("YTG_DISK_QUOTA_MB", "5120"))       # 5 ГБ на всю папку
# Сервис живёт на Proxmox-хосте: мало оставить место себе, нужно не съесть
# его у гипервизора. Если на разделе свободно меньше — новые загрузки не
# принимаются, независимо от квоты выше.
MIN_FREE_DISK_MB = int(os.environ.get("YTG_MIN_FREE_DISK_MB", "10240"))  # 10 ГБ

# --- Приватность: сервер не хранит ничего ---
# Файл удаляется после того, как пользователь его забрал.
DELETE_AFTER_SERVE = os.environ.get("YTG_DELETE_AFTER_SERVE", "1") == "1"
# Отсрочка после начала выдачи. Отсчёт идёт от момента ЗАПРОСА, а сама
# отдача многогигабайтного файла на медленном канале может идти долго, плюс
# пользователю свойственно качать повторно — поэтому запас щедрый.
SERVED_GRACE_SEC = int(os.environ.get("YTG_SERVED_GRACE_SEC", "1800"))
# Как часто работает уборщик.
JANITOR_INTERVAL_SEC = int(os.environ.get("YTG_JANITOR_INTERVAL_SEC", "30"))
# Страховка: даже неотданные файлы живут не дольше этого.
FILE_TTL_MINUTES = int(os.environ.get("YTG_FILE_TTL_MINUTES", "60"))
# Карточки задач держатся в памяти недолго и нигде не персистятся.
TASK_TTL_MINUTES = int(os.environ.get("YTG_TASK_TTL_MINUTES", "120"))
# Не писать URL/IP пользователей в логи.
QUIET_ACCESS_LOG = os.environ.get("YTG_QUIET_ACCESS_LOG", "1") == "1"

# Rate limiting (простое, in-memory, на IP)
RATE_WINDOW_SEC = int(os.environ.get("YTG_RATE_WINDOW_SEC", "60"))
RATE_MAX_INFO = int(os.environ.get("YTG_RATE_MAX_INFO", "20"))         # запросов /api/info за окно
RATE_MAX_DOWNLOAD = int(os.environ.get("YTG_RATE_MAX_DOWNLOAD", "6"))  # запусков загрузки за окно

# Заголовки, которым доверяем реальный IP (за reverse-proxy)
TRUST_PROXY = os.environ.get("YTG_TRUST_PROXY", "1") == "1"

# Максимальный срок одного SSE-соединения. Каждое занимает поток gunicorn,
# поэтому рвём его принудительно — EventSource переподключится сам.
SSE_MAX_SECONDS = int(os.environ.get("YTG_SSE_MAX_SECONDS", "900"))

# --- Публичная лента «что скачивают» ---
# Храним ТОЛЬКО адрес ролика, дату и счётчик. Ни названия, ни обложки:
# их подтягивает браузер напрямую с источника.
FEED_ENABLED = os.environ.get("YTG_FEED_ENABLED", "1") == "1"
FEED_PAGE_SIZE = int(os.environ.get("YTG_FEED_PAGE_SIZE", "50"))
