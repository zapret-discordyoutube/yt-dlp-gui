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
# Глубина очереди ожидания и потолок на число карточек задач в памяти.
# Оба — защита от переполнения: переполнить память или число потоков
# не должно получаться в принципе.
QUEUE_MAX = int(os.environ.get("YTG_QUEUE_MAX", "50"))
TASKS_MAX = int(os.environ.get("YTG_TASKS_MAX", "500"))

# Параллельная загрузка фрагментов (DASH/HLS — то, чем отдаёт YouTube).
# Без этого фрагменты тянутся строго по одному, и скорость упирается в
# задержку до сервера, а не в канал. Держим умеренным: значение умножается
# на число одновременных загрузок, а хост — гипервизор.
CONCURRENT_FRAGMENTS = int(os.environ.get("YTG_CONCURRENT_FRAGMENTS", "5"))

# Прокси для исходящих запросов yt-dlp (socks5://host:port или http://...).
# Задаётся ТОЛЬКО администратором через окружение и никогда не принимается
# от пользователя: произвольный прокси — это готовый SSRF.
# Нужен там, где сайт недоступен напрямую из сети хоста.
PROXY = os.environ.get("YTG_PROXY", "").strip()
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
# Отсрочка после начала выдачи: запас на докачку по Range и повторный клик.
SERVED_GRACE_SEC = int(os.environ.get("YTG_SERVED_GRACE_SEC", "1800"))
# Как часто работает уборщик.
JANITOR_INTERVAL_SEC = int(os.environ.get("YTG_JANITOR_INTERVAL_SEC", "30"))
# Жёсткий потолок: ни один файл не живёт на сервере дольше этого,
# независимо от того, забрали его или нет. Отсчёт — от готовности файла.
# 2 часа: за это время файл в 2 ГБ (наш потолок) успевает скачаться даже
# на 2,3 Мбит/с. Общий объём ограничивает не время, а квота DISK_QUOTA_MB.
FILE_TTL_MINUTES = int(os.environ.get("YTG_FILE_TTL_MINUTES", "120"))
# Карточки задач держатся в памяти недолго и нигде не персистятся.
# Срок заведомо больше FILE_TTL_MINUTES: карточка отсчитывается от создания
# задачи, а файл — от окончания скачивания, поэтому при равных значениях
# карточка истекала раньше файла ровно на длительность загрузки, и файл
# оставался на диске недостижимым, занимая квоту.
TASK_TTL_MINUTES = int(os.environ.get("YTG_TASK_TTL_MINUTES", "240"))
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
