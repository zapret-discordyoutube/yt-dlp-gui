"""Параллельный загрузчик для видеосерверов YouTube (googlevideo.com).

Что мешает. Провайдерский DPI пропускает к googlevideo пачку новых
TLS-соединений после паузы, а следующие какое-то время молча вешает на
рукопожатии (обход по SNI не помогает — перебор стратегий zapret дал тот
же результат). Уже установленное соединение при этом работает в полную
скорость. Штатный загрузчик yt-dlp ждёт по 20 с на попытку одного
соединения; aria2c сначала одним соединением узнаёт размер — оба «висели»
минутами.

Как обходим. Размер файла YouTube сообщает заранее (filesize / clen),
поэтому сразу, одной пачкой, открываем WORKERS соединений и дальше БЕРЕЖЁМ
их: каждое keep-alive забирает кусок за куском по Range, между дорожками
одной задачи соединения переиспользуются. Медленный кусок не рвём (новое
соединение может и не открыться) — в конце свободные соединения дублируют
недокачанные куски, выигрывает первое. Прогресс, отмена и сторож размера
работают через штатные хуки yt-dlp.
"""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
from urllib.parse import urlparse

import requests
from yt_dlp.downloader.common import FileDownloader

CHUNK = 1 << 20          # 1 МиБ на запрос
WORKERS = 12             # соединений на файл
WORKERS_BIG = 16         # на большой файл (4K и т.п.)
BIG_FILE = 64 << 20      # от какого размера файл «большой»
CONNECT_TIMEOUT = 3      # TCP+TLS: повисшее рукопожатие бросаем
MAX_BACKOFF = 2.0        # пауза перед новой попыткой после неудачи
# Смена сервера. DPI режет по IP конкретного видеосервера: к одному новые
# соединения не пускает, к другому в тот же момент пускает все. Повторный
# разбор ролика обычно выдаёт ссылку на тот же файл на другом сервере.
ALIVE_WINDOW = 10        # соединение «живое», если отдало кусок за это время, с
RESOLVE_EVERY = 8        # не чаще, чем раз в столько секунд
MAX_MIRRORS = 6          # сколько серверов держать для одного файла
READ_TIMEOUT = 15        # полная тишина внутри ответа
MAX_FAILS = 400          # подряд неудач у всех соединений -> ошибка (обычно раньше снимет сторож менеджера)


class _Slot:
    """Соединение-«слот». Живёт весь процесс: хорошее соединение, раз
    пробившись, обслуживает и видео, и звук задачи."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.busy = False          # занят потоком (в т.ч. брошенным)

    def renew(self) -> None:
        try:
            self.session.close()
        except Exception:          # noqa: BLE001
            pass
        self.session = requests.Session()


# Счётчики за процесс (= за одну задачу): уходят в метрики производительности.
STATS = {"files": 0, "conn_ok": 0, "conn_fail": 0, "mirrors": 1}
_stats_lock = threading.Lock()


def _count(key: str, n: int = 1) -> None:
    with _stats_lock:
        STATS[key] += n


_slots_lock = threading.Lock()
_slots: list[_Slot] = []


def _take_slots(n: int) -> list[_Slot]:
    """Свободные слоты. Слот, чей поток ещё дочитывает прошлый ответ,
    не отдаём — вместо него заводим новый."""
    with _slots_lock:
        free = [s for s in _slots if not s.busy][:n]
        while len(free) < n:
            s = _Slot()
            _slots.append(s)
            free.append(s)
        for s in free:
            s.busy = True
        return free


def _size_of(info: dict) -> int | None:
    size = info.get("filesize")
    if size:
        return int(size)
    m = re.search(r"[?&]clen=(\d+)", info.get("url") or "")
    return int(m.group(1)) if m else None


def suitable(info: dict, params: dict) -> bool:
    """Берёмся только за прямой https-файл с googlevideo известного размера
    и без прокси (через прокси DPI не мешает, а SOCKS мы не поддерживаем)."""
    if params.get("proxy") or info.get("protocol") not in ("https", "http"):
        return False
    host = urlparse(info.get("url") or "").hostname or ""
    if not host.endswith(".googlevideo.com"):
        return False
    # Порога по размеру нет: маленький файл (звук короткого ролика) штатный
    # загрузчик так же вешает на DPI, а у нас за единственный кусок сразу
    # гоняются все соединения.
    return bool(_size_of(info))


class RaceFD(FileDownloader):
    def real_download(self, filename, info_dict):
        size = _size_of(info_dict)
        _count("files")
        # Зеркала: ссылки на ЭТОТ ЖЕ файл (тот же формат и размер) на разных
        # серверах. [url, удачи, неудачи]. Куски — байтовые диапазоны одного
        # файла, поэтому их можно брать с любого зеркала вперемешку.
        mirrors: list[list] = [[info_dict["url"], 0, 0]]
        alive: dict[int, float] = {}             # поток -> время последней удачи
        headers = dict(info_dict.get("http_headers") or {})
        tmp = self.temp_name(filename)
        self.report_destination(filename)

        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.ftruncate(fd, size)
        n_chunks = (size + CHUNK - 1) // CHUNK
        todo: queue.Queue = queue.Queue()
        for i in range(n_chunks):
            todo.put(i)
        finished: set[int] = set()
        inflight: dict[int, int] = {}
        got = [0]
        fails = [0]                 # неудач подряд (сбрасывается успехом)
        lock = threading.Lock()
        # Запись и закрытие файла — под одним замком: поток, который ещё
        # дочитывает брошенный ответ, не должен писать в чужой файл, если
        # номер дескриптора уже переиспользован.
        wlock = threading.Lock()
        stop = threading.Event()

        def next_chunk() -> int | None:
            try:
                return todo.get(timeout=0.2)
            except queue.Empty:
                pass
            with lock:
                if len(finished) == n_chunks:
                    return None
                # «Добивание»: самый мало дублируемый недокачанный кусок.
                left = [c for c in inflight if c not in finished]
                return min(left, key=lambda c: inflight[c]) if left else -1

        def best_mirror() -> int:
            """Зеркало с лучшим счётом; новое (без истории) — в приоритете."""
            with lock:
                return max(range(len(mirrors)),
                           key=lambda m: (mirrors[m][1] + 1) / (mirrors[m][2] + 1) + m * 0.01)

        def worker(slot: _Slot, wid: int):
            backoff = 0.0
            m = 0
            try:
                while not stop.is_set():
                    i = next_chunk()
                    if i is None:
                        return
                    if i < 0:
                        continue
                    with lock:
                        inflight[i] = inflight.get(i, 0) + 1
                    start = i * CHUNK
                    end = min(size, start + CHUNK) - 1
                    try:
                        r = slot.session.get(
                            mirrors[m][0], headers={**headers, "Range": f"bytes={start}-{end}"},
                            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True)
                        if r.status_code not in (200, 206):
                            r.close()
                            raise OSError(f"HTTP {r.status_code}")
                        pos = start
                        # Ответ читаем до конца, даже если кусок уже добил
                        # другой: недочитанный ответ губит соединение, а новое
                        # может не открыться.
                        for block in r.iter_content(64 << 10):
                            if i not in finished:
                                with wlock:
                                    if stop.is_set():
                                        return
                                    os.pwrite(fd, block, pos)
                            pos += len(block)
                        with lock:
                            fails[0] = 0
                            mirrors[m][1] += 1
                            alive[wid] = time.monotonic()
                        _count("conn_ok")
                        with lock:
                            if i not in finished and pos == end + 1:
                                finished.add(i)
                                got[0] += end - start + 1
                            elif i not in finished:
                                todo.put(i)
                        backoff = 0.0
                    except Exception:                         # noqa: BLE001
                        with lock:
                            fails[0] += 1
                            mirrors[m][2] += 1
                        _count("conn_fail")
                        with lock:
                            if fails[0] >= MAX_FAILS:
                                stop.set()
                            if i not in finished:
                                todo.put(i)
                        slot.renew()
                        m = best_mirror()                 # новое соединение — к лучшему серверу
                        # Короткая пауза: DPI то пускает новые соединения,
                        # то нет, и долгое ожидание (раньше до 8 с) оставляло
                        # большинство потоков простаивать, когда он снова
                        # начинал пускать.
                        backoff = min(MAX_BACKOFF, backoff * 2 or 0.5)
                        stop.wait(backoff)
                    finally:
                        with lock:
                            inflight[i] -= 1
                            if inflight[i] <= 0:
                                inflight.pop(i, None)
            finally:
                slot.busy = False

        slots = _take_slots(WORKERS_BIG if size >= BIG_FILE else WORKERS)
        threads = [threading.Thread(target=worker, args=(s, n), daemon=True)
                   for n, s in enumerate(slots)]
        resolving = threading.Event()
        # Первый поиск запасного сервера — сразу, если соединения не поднялись.
        last_resolve = [time.monotonic() - RESOLVE_EVERY]

        def resolve():
            """Повторно разобрать ролик и добавить зеркало на другом сервере."""
            try:
                new = _fresh_url(info_dict, size)
                if new:
                    host = urlparse(new).netloc
                    with lock:
                        if all(urlparse(x[0]).netloc != host for x in mirrors):
                            mirrors.append([new, 0, 0])
                            with _stats_lock:
                                STATS["mirrors"] = max(STATS["mirrors"], len(mirrors))
            finally:
                last_resolve[0] = time.monotonic()
                resolving.clear()
        started = time.time()
        for t in threads:
            t.start()
        last_t, last_b, speed = started, 0, None
        try:
            while True:
                time.sleep(0.5)
                with lock:
                    done_now, all_done = got[0], len(finished) == n_chunks
                if all_done or stop.is_set():
                    break
                # Живых соединений мало — сервер, скорее всего, под DPI:
                # в фоне ищем тот же файл на другом сервере (то же, что даёт
                # ручной перезапуск загрузки).
                mono = time.monotonic()
                with lock:
                    n_alive = sum(1 for t in alive.values() if mono - t < ALIVE_WINDOW)
                if (n_alive < len(slots) // 2 and not resolving.is_set()
                        and mono - last_resolve[0] > RESOLVE_EVERY
                        and len(mirrors) < MAX_MIRRORS):
                    resolving.set()
                    threading.Thread(target=resolve, daemon=True).start()
                now = time.time()
                if done_now == last_b:
                    continue       # без новых байт хук не зовём: менеджер видит простой
                inst = (done_now - last_b) / max(now - last_t, 1e-3)
                speed = inst if speed is None else speed * 0.7 + inst * 0.3
                last_t, last_b = now, done_now
                self._hook_progress({
                    "status": "downloading", "filename": filename, "tmpfilename": tmp,
                    "downloaded_bytes": done_now, "total_bytes": size,
                    "speed": speed, "elapsed": now - started,
                    "eta": int((size - done_now) / speed) if speed else None,
                }, info_dict)
        finally:
            with wlock:
                stop.set()
                os.close(fd)
            # Потоки, дочитывающие медленный ответ, не ждём: файлу они уже
            # не пишут (stop под wlock), а слот остаётся занят до их конца.
            for t in threads:
                t.join(timeout=0.5)

        if got[0] != size:
            self.report_error("не удалось скачать файл целиком: источник не отвечает")
            return False
        self.try_rename(tmp, filename)
        self._hook_progress({
            "status": "finished", "filename": filename,
            "downloaded_bytes": size, "total_bytes": size,
            "elapsed": time.time() - started,
        }, info_dict)
        return True


def _fresh_url(info: dict, size: int) -> str | None:
    """Новая ссылка на тот же формат: повторный разбор страницы ролика.
    Принимаем только файл того же размера — иначе это другой файл."""
    import yt_dlp
    page = info.get("webpage_url") or info.get("original_url")
    fid = info.get("format_id")
    if not page or not fid:
        return None

    class _Quiet:
        def debug(self, msg): pass
        info = warning = error = debug

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": _Quiet(),
                               "socket_timeout": 15}) as ydl:
            data = ydl.extract_info(page, download=False, process=False)
            data = ydl.sanitize_info(data)
    except Exception:                                   # noqa: BLE001
        return None
    for f in data.get("formats") or []:
        if f.get("format_id") == fid and f.get("url") and _size_of(f) == size:
            return f["url"]
    return None


def install() -> None:
    """Подставить RaceFD в выбор загрузчика yt-dlp (в этом процессе)."""
    ydl_mod = sys.modules["yt_dlp.YoutubeDL"]
    original = ydl_mod.get_suitable_downloader

    def pick(info_dict, params=None, *args, **kwargs):
        if params is not None and suitable(info_dict, params):
            return RaceFD
        return original(info_dict, params, *args, **kwargs)

    ydl_mod.get_suitable_downloader = pick
