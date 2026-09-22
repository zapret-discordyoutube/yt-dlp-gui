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

import socket

import requests
from yt_dlp.downloader.common import FileDownloader

import config
import hostban

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
RESOLVE_EVERY = 4        # не чаще, чем раз в столько секунд
RESOLVE_PARALLEL = 3     # параллельных разборов за раунд: YouTube часто
                         # выдаёт тот же сервер, а 3 разом дают разные
MAX_MIRRORS = 6          # сколько серверов держать для одного файла
BAN_WAIT = 3             # сколько ждать запасной сервер, прежде чем всё же
                         # постучаться в заблокированный, с
# Скорость сервера (на одно соединение, МБ/с): медленнее SLOW — опускаем в
# выборе, быстрее FAST — поднимаем. Без замеров считаем сервер средним.
SLOW_MBPS = 1.0
FAST_MBPS = 5.0
NEUTRAL_MBPS = 2.0
# Запасной путь через egress-узел (пул SOCKS-туннелей, config.EGRESS_POOL):
# включается, когда все прямые серверы ролика в бане или долго нет ни байта.
# Провайдер режет каждое соединение до узла (~300 КБ/с), поэтому у каждого
# потока свой туннель. Ссылку для этого пути получаем разбором ЧЕРЕЗ egress:
# YouTube привязывает ссылку к IP, с которого её запросили.
EGRESS_AFTER = 6         # сколько ждать первого байта напрямую, с
EGRESS_NEUTRAL_MBPS = 1.0  # оценка egress-пути до замеров: ниже прямого
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


# Знания о серверах — на весь процесс (= на задачу), общие для всех дорожек:
# счёт удач/неудач по серверу и найденные ссылки на каждый формат на других
# серверах. Звуковая дорожка сразу знает, что сервер видео заблокирован, и
# берёт готовую ссылку, найденную при разборе ради видео.
_HOSTS: dict[str, list[int]] = {}                 # netloc -> [удачи, неудачи]
_ALT: dict[tuple, dict[str, str]] = {}            # (страница, формат, размер) -> {netloc: url}
_mirror_lock = threading.Lock()


_ips: dict[str, str | None] = {}


def _ip_of(url: str) -> str | None:
    """IP сервера из ссылки (кэш: у rr-хостов адрес один)."""
    host = urlparse(url).hostname or ""
    if host not in _ips:
        try:
            _ips[host] = socket.getaddrinfo(host, 443, socket.AF_INET,
                                            socket.SOCK_STREAM)[0][4][0]
        except OSError:
            _ips[host] = None
    return _ips[host]


def _is_banned(url: str) -> bool:
    return hostban.is_banned(_ip_of(url))


def _host_note(url: str, ok: bool) -> None:
    """Учесть исход соединения. Сервер, к которому подряд не прошло ни одно
    соединение, уходит в общий список заблокированных (hostban) — его будут
    обходить все загрузки; удача снимает его оттуда."""
    with _mirror_lock:
        h = _HOSTS.setdefault(urlparse(url).netloc, [0, 0])
        h[0 if ok else 1] += 1
        ok_n, fail_n = h
    ip = _ip_of(url)
    if ok:
        if hostban.is_banned(ip):
            hostban.clear(ip)
    elif ok_n == 0 and fail_n >= config.BAN_FAILS and not hostban.is_banned(ip):
        hostban.ban(ip)
        _count("banned")


_speed: dict[str, float] = {}                     # netloc -> МБ/с (EMA, этот процесс)
_speed_sent: dict[str, float] = {}                # netloc -> когда писали в общий список


def _speed_note(url: str, nbytes: int, sec: float) -> None:
    """Замер скорости одного куска: копим у себя и изредка делимся со всеми."""
    if sec <= 0 or nbytes < CHUNK // 2:
        return
    mbps = nbytes / sec / 1048576
    netloc = urlparse(url).netloc
    with _mirror_lock:
        prev = _speed.get(netloc)
        _speed[netloc] = mbps if prev is None else prev * 0.7 + mbps * 0.3
        cur = _speed[netloc]
        due = time.monotonic() - _speed_sent.get(netloc, 0) > 5
        if due:
            _speed_sent[netloc] = time.monotonic()
    if due:
        hostban.rate_put(_ip_of(url), cur)


def _host_speed(url: str) -> float:
    """Скорость сервера: свой замер, иначе общий, иначе «средний»."""
    with _mirror_lock:
        mine = _speed.get(urlparse(url).netloc)
    if mine is not None:
        return mine
    shared = hostban.rate_get(_ip_of(url))
    return shared if shared is not None else NEUTRAL_MBPS


def _host_score(url: str) -> float:
    """Чем больше, тем лучше: скорость соединения × доля удачных попыток.
    Медленный (<1 МБ/с) сервер уходит вниз, быстрый (>5 МБ/с) — вверх."""
    if _is_banned(url):
        return -1.0                               # заблокирован — в самый конец
    with _mirror_lock:
        ok, fail = _HOSTS.get(urlparse(url).netloc, (0, 0))
    return _host_speed(url) * (ok + 1) / (ok + fail + 1)


def _is_live(url: str) -> bool:
    """Соединения к серверу сейчас проходят (в этом процессе была удача)."""
    with _mirror_lock:
        return _HOSTS.get(urlparse(url).netloc, (0, 0))[0] > 0


def _alt_key(info: dict, size: int, via: str | None = None) -> tuple:
    return (info.get("webpage_url") or info.get("original_url"), info.get("format_id"),
            size, via)


# Зеркало: [url, удачи, неудачи, путь]; путь — None (напрямую) или "egress".
# Egress-путь — не «сервер», а обход: в общий список блокировок и общий
# рейтинг серверов он не пишется, его счёт — свой, в пределах процесса.
_egress = {"ok": 0, "fail": 0, "speed": None}


def _m_banned(mr: list) -> bool:
    return mr[3] is None and _is_banned(mr[0])


def _m_note(mr: list, ok: bool) -> None:
    if mr[3] is None:
        _host_note(mr[0], ok)
    else:
        with _mirror_lock:
            _egress["ok" if ok else "fail"] += 1


def _m_speed_note(mr: list, nbytes: int, sec: float) -> None:
    if mr[3] is None:
        _speed_note(mr[0], nbytes, sec)
    elif sec > 0 and nbytes >= CHUNK // 2:
        mbps = nbytes / sec / 1048576
        with _mirror_lock:
            prev = _egress["speed"]
            _egress["speed"] = mbps if prev is None else prev * 0.7 + mbps * 0.3


def _m_speed(mr: list) -> float:
    if mr[3] is None:
        return _host_speed(mr[0])
    with _mirror_lock:
        return _egress["speed"] or EGRESS_NEUTRAL_MBPS


def _m_score(mr: list) -> float:
    if mr[3] is None:
        return _host_score(mr[0])
    with _mirror_lock:
        ok, fail = _egress["ok"], _egress["fail"]
    return _m_speed(mr) * (ok + 1) / (ok + fail + 1)


def _m_live(mr: list) -> bool:
    if mr[3] is None:
        return _is_live(mr[0])
    with _mirror_lock:
        return _egress["ok"] > 0


# Счётчики за процесс (= за одну задачу): уходят в метрики производительности.
STATS = {"files": 0, "conn_ok": 0, "conn_fail": 0, "mirrors": 1,
         "banned": 0, "avoided": 0, "egress_bytes": 0}
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
        mirrors: list[list] = [[info_dict["url"], 0, 0, None]]
        # Ссылки на этот же формат на других серверах — из прошлых разборов
        # (и через egress, если прошлая дорожка задачи уже туда уходила).
        with _mirror_lock:
            known = dict(_ALT.get(_alt_key(info_dict, size), {}))
            known_egress = dict(_ALT.get(_alt_key(info_dict, size, "egress"), {}))
        for netloc, alt in known.items():
            if all(urlparse(x[0]).netloc != netloc for x in mirrors):
                mirrors.append([alt, 0, 0, None])
        for alt in list(known_egress.values())[:1]:
            mirrors.append([alt, 0, 0, "egress"])
        pool = config.EGRESS_POOL
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
            """Сервер с лучшим счётом за всю задачу; новый — в приоритете."""
            with lock:
                snap = [list(x) for x in mirrors]
            return max(range(len(snap)), key=lambda m: _m_score(snap[m]) + m * 0.01)

        def worker(slot: _Slot, wid: int):
            backoff = 0.0
            m = best_mirror()           # заведомо заблокированный сервер — в обход
            began = time.monotonic()
            # Своя сессия для egress-пути: у каждого потока свой туннель.
            esess = [None]

            def session_for(mr):
                if mr[3] is None or not pool:
                    return slot.session
                if esess[0] is None:
                    px = pool[wid % len(pool)]
                    esess[0] = requests.Session()
                    esess[0].proxies = {"http": px, "https": px}
                return esess[0]

            def renew(mr):
                if mr[3] is None or not pool:
                    slot.renew()
                elif esess[0] is not None:
                    try:
                        esess[0].close()
                    except Exception:            # noqa: BLE001
                        pass
                    esess[0] = None
            try:
                while not stop.is_set():
                    # Не стучимся в заблокированный сервер: есть другой — к
                    # нему; нет — ждём, пока разбор найдёт (но недолго: вдруг
                    # блокировку уже сняли).
                    if _m_banned(mirrors[m]):
                        alt = best_mirror()
                        if not _m_banned(mirrors[alt]):
                            m = alt
                        elif time.monotonic() - began < BAN_WAIT:
                            _count("avoided")
                            stop.wait(0.5)
                            continue
                    i = next_chunk()
                    if i is None:
                        return
                    if i < 0:
                        continue
                    with lock:
                        inflight[i] = inflight.get(i, 0) + 1
                    start = i * CHUNK
                    end = min(size, start + CHUNK) - 1
                    t_req = time.monotonic()
                    mr = mirrors[m]
                    try:
                        r = session_for(mr).get(
                            mr[0], headers={**headers, "Range": f"bytes={start}-{end}"},
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
                            mr[1] += 1
                            alive[wid] = time.monotonic()
                        _count("conn_ok")
                        if mr[3] is not None:
                            _count("egress_bytes", pos - start)
                        _m_note(mr, True)
                        _m_speed_note(mr, pos - start, time.monotonic() - t_req)
                        # Медленный сервер: если есть заметно более быстрый, к
                        # которому соединения ПРЯМО СЕЙЧАС проходят, — уходим
                        # туда. На непроверенный не меняем: живое медленное
                        # соединение лучше нового, которое DPI может не пустить.
                        cur = _m_speed(mr)
                        if cur < SLOW_MBPS:
                            alt = best_mirror()
                            if (alt != m and _m_live(mirrors[alt])
                                    and _m_speed(mirrors[alt]) > cur * 2):
                                renew(mr)
                                m = alt
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
                            mr[2] += 1
                        _count("conn_fail")
                        _m_note(mr, False)
                        with lock:
                            if fails[0] >= MAX_FAILS:
                                stop.set()
                            if i not in finished:
                                todo.put(i)
                        renew(mr)
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
                for new in _fresh_urls(info_dict, size):
                    host = urlparse(new).netloc
                    with lock:
                        if (len(mirrors) < MAX_MIRRORS
                                and all(x[3] is not None or urlparse(x[0]).netloc != host
                                        for x in mirrors)):
                            mirrors.append([new, 0, 0, None])
                            with _stats_lock:
                                STATS["mirrors"] = max(STATS["mirrors"], len(mirrors))
            finally:
                last_resolve[0] = time.monotonic()
                resolving.clear()
        egress_state = {"started": any(x[3] for x in mirrors), "running": False}

        def resolve_egress():
            """Ссылка на тот же формат, полученная разбором ЧЕРЕЗ egress."""
            try:
                new = _fresh_url(info_dict, size, proxy=pool[0])
                if new:
                    with lock:
                        mirrors.append([new, 0, 0, "egress"])
                        with _stats_lock:
                            STATS["mirrors"] = max(STATS["mirrors"], len(mirrors))
            finally:
                egress_state["running"] = False

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
                # Запасной путь через egress: все прямые серверы в бане или
                # долго нет ни одного живого соединения. Прямые попытки при
                # этом продолжаются — кто первый отдаст, тот и качает.
                if pool and not egress_state["started"] and not egress_state["running"]:
                    with lock:
                        direct = [x for x in mirrors if x[3] is None]
                    all_banned = bool(direct) and all(_m_banned(x) for x in direct)
                    starving = n_alive == 0 and time.time() - started > EGRESS_AFTER
                    if all_banned or starving:
                        egress_state.update(started=True, running=True)
                        threading.Thread(target=resolve_egress, daemon=True).start()
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


def _fresh_url(info: dict, size: int, proxy: str | None = None) -> str | None:
    """Новая ссылка на тот же формат: повторный разбор страницы ролика.
    Принимаем только файл того же размера — иначе это другой файл.
    proxy — разбор через egress: ссылка будет привязана к его IP и годится
    только для загрузки через тот же egress."""
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
                               "socket_timeout": 15,
                               # Через egress — по IPv4: IPv6-путь узла до
                               # Google медленный (см. jobrunner._force_ipv4).
                               **({"proxy": proxy, "source_address": "0.0.0.0"}
                                  if proxy else {})}) as ydl:
            data = ydl.extract_info(page, download=False, process=False)
            data = ydl.sanitize_info(data)
    except Exception:                                   # noqa: BLE001
        return None
    # Разбор приносит ссылки на ВСЕ форматы — запоминаем их: следующей
    # дорожке задачи новый разбор уже не понадобится.
    found = None
    with _mirror_lock:
        for f in data.get("formats") or []:
            fsize = _size_of(f)
            if not (f.get("url") and f.get("format_id") and fsize):
                continue
            key = (page, f["format_id"], fsize, "egress" if proxy else None)
            _ALT.setdefault(key, {})[urlparse(f["url"]).netloc] = f["url"]
            if f["format_id"] == fid and fsize == size:
                found = f["url"]
    return found


def _fresh_urls(info: dict, size: int) -> list[str]:
    """Несколько разборов разом: YouTube часто отдаёт тот же сервер, а
    параллельные запросы чаще получают разные."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(RESOLVE_PARALLEL) as ex:
        urls = list(ex.map(lambda _: _fresh_url(info, size), range(RESOLVE_PARALLEL)))
    out, seen = [], set()
    for u in urls:
        if u and urlparse(u).netloc not in seen:
            seen.add(urlparse(u).netloc)
            out.append(u)
    return out


def install() -> None:
    """Подставить RaceFD в выбор загрузчика yt-dlp (в этом процессе)."""
    ydl_mod = sys.modules["yt_dlp.YoutubeDL"]
    original = ydl_mod.get_suitable_downloader

    def pick(info_dict, params=None, *args, **kwargs):
        if params is not None and suitable(info_dict, params):
            return RaceFD
        return original(info_dict, params, *args, **kwargs)

    ydl_mod.get_suitable_downloader = pick
