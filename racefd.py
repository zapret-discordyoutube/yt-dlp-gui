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
# Большие файлы — кусками по 4 МиБ. YouTube ограничивает частоту запросов к
# одной ссылке: куски по 1 МиБ на быстром канале — десятки запросов в секунду,
# и сервер отвечает 401. Замер на 4K: 1 МиБ × 32 — 2 МБ/с и 1408 отказов из
# 1509; 4 МиБ × 16 — 99 МБ/с без единого отказа.
CHUNK_BIG = 4 << 20
# Отказы «слишком часто» — пауза подольше, а не новая попытка сразу.
RATE_LIMIT_CODES = ("HTTP 401", "HTTP 429")
RATE_LIMIT_PAUSE = 2.0
# 403 — не DPI (тот вешает рукопожатие), а мёртвая ссылка: сервер ответил,
# но этот файл по ней больше не отдаст. Столько 403 подряд — ссылку бросаем;
# все ссылки мёртвые и новых не будет — загрузку прекращаем сразу (исполнитель
# разберёт ролик заново), а не долбим 403 тысячами до MAX_FAILS.
DEAD_LINK_CODE = "HTTP 403"
DEAD_LINK_FAILS = 6
WORKERS = 12             # соединений на файл
WORKERS_BIG = 16         # на большой файл (4K и т.п.) напрямую: больше — упор в лимит запросов
WORKERS_EGRESS_BIG = 32  # на большой файл целиком через egress-пул — по туннелю на поток
BIG_FILE = 64 << 20      # от какого размера файл «большой»
CONNECT_TIMEOUT = 3      # TCP+TLS: повисшее рукопожатие бросаем
MAX_BACKOFF = 2.0        # пауза перед новой попыткой после неудачи
# Смена сервера. DPI режет по IP конкретного видеосервера: к одному новые
# соединения не пускает, к другому в тот же момент пускает все. Повторный
# разбор ролика обычно выдаёт ссылку на тот же файл на другом сервере.
ALIVE_WINDOW = 10        # соединение «живое», если отдало кусок за это время, с
RESOLVE_EVERY = 4        # не чаще, чем раз в столько секунд
RESOLVE_PARALLEL = 2     # параллельных разборов за раунд: YouTube часто
                         # выдаёт тот же сервер, а несколько разом — разные
MAX_RESOLVE_ROUNDS = 3   # раундов на файл: частые разборы с одного IP YouTube
                         # принимает за бота («Sign in to confirm you're not a bot»)
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
EGRESS_AFTER = 3         # сколько ждать первого байта напрямую, с
EGRESS_NEUTRAL_MBPS = 1.0  # оценка egress-пути до замеров: ниже прямого
READ_TIMEOUT = 15        # полная тишина внутри ответа
EGRESS_READ_TIMEOUT = 8  # через egress: подвисший туннель бросаем быстрее
DEMAND_STALL = 2         # по запросу: нужный кусок не идёт столько, с — ищем другой сервер
DEMAND_AHEAD = 24 << 20  # по запросу: упреждение, байт — всё сверх него на конце отрезка впустую
DEMAND_DUP_AFTER = 1.0   # по запросу: кусок, которого ждёт ffmpeg, дублируем не сразу, с
MAX_FAILS = 5000         # подряд неудач у всех соединений -> ошибка; на деле решает сторож
                         # простоя менеджера (STALL_SEC): минутный провал пути до egress
                         # при 16 потоках набирал старые 400 неудач меньше чем за минуту


class _SourceAdapter(requests.adapters.HTTPAdapter):
    """Соединения с заданного адреса-источника (свой IP для YouTube)."""

    def __init__(self, source_ip: str, **kwargs) -> None:
        self._source_ip = source_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["source_address"] = (self._source_ip, 0)
        return super().init_poolmanager(*args, **kwargs)


# Маршрут у сессий racefd всегда явный: напрямую со своего IP или в свой
# туннель пула. Переменные HTTP(S)_PROXY окружения им не указ: requests
# ставит их ВЫШЕ Session.proxies — и туннели пула подменялись бы чужим
# прокси (так висели тесты egress-пула в оболочке с HTTP_PROXY).
def _direct_session() -> requests.Session:
    """Сессия для прямых соединений с видеосерверами — с IP для YouTube,
    если он задан (ссылки на файлы привязаны к IP, с которого их получили).
    Egress-сессии сюда не относятся: они ходят в локальный SOCKS."""
    s = requests.Session()
    s.trust_env = False
    if config.YT_SOURCE_IP:
        ad = _SourceAdapter(config.YT_SOURCE_IP)
        s.mount("https://", ad)
        s.mount("http://", ad)
    return s


class _Slot:
    """Соединение-«слот». Живёт весь процесс: хорошее соединение, раз
    пробившись, обслуживает и видео, и звук задачи."""

    def __init__(self) -> None:
        self.session = _direct_session()
        self.busy = False          # занят потоком (в т.ч. брошенным)

    def renew(self) -> None:
        try:
            self.session.close()
        except Exception:          # noqa: BLE001
            pass
        self.session = _direct_session()


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
# Причины неудач по видам (HTTP-код, таймаут, обрыв…) — для диагностики.
FAIL_KINDS: dict[str, int] = {}


def _fail_kind(e: Exception, via: str | None) -> None:
    name = str(e) if str(e).startswith("HTTP ") else type(e).__name__
    key = f"{'egress' if via else 'direct'}:{name}"
    with _stats_lock:
        FAIL_KINDS[key] = FAIL_KINDS.get(key, 0) + 1
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
    """Берёмся за прямой https-файл с googlevideo известного размера — либо
    без прокси, либо через egress-пул (ролик разобран через egress: YouTube
    не пускает наш IP). Через прочие прокси — штатный загрузчик."""
    proxy = params.get("proxy")
    if proxy and proxy not in config.EGRESS_POOL:
        return False
    if info.get("protocol") not in ("https", "http"):
        return False
    host = urlparse(info.get("url") or "").hostname or ""
    if not host.endswith(".googlevideo.com"):
        return False
    # Порога по размеру нет: маленький файл (звук короткого ролика) штатный
    # загрузчик так же вешает на DPI, а у нас за единственный кусок сразу
    # гоняются все соединения.
    return bool(_size_of(info))


def _chunk_of(size: int) -> int:
    return CHUNK_BIG if size >= BIG_FILE else CHUNK


class RaceFD(FileDownloader):
    def real_download(self, filename, info_dict, demand: _Demand | None = None):
        """demand — режим «по запросу» (отрезок, см. SectionSession): файл не
        качается целиком, а только куски, которые сейчас читает ffmpeg."""
        size = _size_of(info_dict)
        _count("files")
        # Зеркала: ссылки на ЭТОТ ЖЕ файл (тот же формат и размер) на разных
        # серверах. [url, удачи, неудачи]. Куски — байтовые диапазоны одного
        # файла, поэтому их можно брать с любого зеркала вперемешку.
        pool = config.EGRESS_POOL
        # Задача целиком через egress (YouTube не пускает наш IP): ссылка
        # привязана к IP узла, качаем только через пул, прямой путь не трогаем.
        egress_only = bool(self.params.get("proxy")) and bool(pool)
        mirrors: list[list] = [[info_dict["url"], 0, 0, "egress" if egress_only else None]]
        # Ссылки на этот же формат на других серверах — из прошлых разборов
        # (и через egress, если прошлая дорожка задачи уже туда уходила).
        with _mirror_lock:
            known = dict(_ALT.get(_alt_key(info_dict, size), {}))
            known_egress = dict(_ALT.get(_alt_key(info_dict, size, "egress"), {}))
        if not egress_only:
            for netloc, alt in known.items():
                if all(urlparse(x[0]).netloc != netloc for x in mirrors):
                    mirrors.append([alt, 0, 0, None])
            for alt in list(known_egress.values())[:1]:
                mirrors.append([alt, 0, 0, "egress"])
        alive: dict[int, float] = {}             # поток -> время последней удачи
        forbidden: dict[int, int] = {}           # зеркало -> 403 подряд
        dead: set[int] = set()                   # зеркала с мёртвой ссылкой
        headers = dict(info_dict.get("http_headers") or {})
        if demand is None:
            tmp = self.temp_name(filename)
            self.report_destination(filename)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            os.ftruncate(fd, size)
        else:
            tmp, fd = None, demand.fd           # файл-подложка — у сессии
        chunk = _chunk_of(size)
        n_chunks = (size + chunk - 1) // chunk
        todo: queue.Queue = queue.Queue()
        if demand is None:
            for i in range(n_chunks):
                todo.put(i)
        finished: set[int] = demand.finished if demand is not None else set()
        inflight: dict[int, int] = {}
        got = [0]
        # Байты недокачанных кусков — для прогресса: на медленном соединении
        # кусок в 1 МиБ идёт десятки секунд, и без этого полоса стояла на нуле.
        partial: dict[int, int] = {}
        fails = [0]                 # неудач подряд (сбрасывается успехом)
        lock = threading.Lock()
        # Запись и закрытие файла — под одним замком: поток, который ещё
        # дочитывает брошенный ответ, не должен писать в чужой файл, если
        # номер дескриптора уже переиспользован.
        wlock = threading.Lock()
        stop = threading.Event()

        def next_chunk() -> int | None:
            if demand is not None:
                return next_wanted()
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

        def next_wanted() -> int | None:
            """По запросу: самый ранний нужный кусок, за которым ещё никто
            не пошёл; все в работе — дублируем тот, что ждут дольше всех
            (читатель стоит именно на нём), но не больше чем вдвоём."""
            with lock:
                if demand.closed:
                    return None
                left = [c for c in demand.wanted() if c not in finished]
                now = time.monotonic()
                for c in left:
                    if c not in inflight:
                        demand.taken[c] = now
                        return c
                if (left and inflight.get(left[0], 0) < 2
                        and now - demand.taken.get(left[0], now) > DEMAND_DUP_AFTER):
                    return left[0]
            demand.wait(0.2)
            return -1

        def best_mirror() -> int:
            """Сервер с лучшим счётом за всю задачу; новый — в приоритете."""
            with lock:
                snap = [list(x) for x in mirrors]
                live = [m for m in range(len(snap)) if m not in dead] or list(range(len(snap)))
            return max(live, key=lambda m: _m_score(snap[m]) + m * 0.01)

        def worker(slot: _Slot, wid: int):
            backoff = 0.0
            m = best_mirror()           # заведомо заблокированный сервер — в обход
            began = time.monotonic()
            # Своя сессия для egress-пути: у каждого потока свой туннель.
            # После неудачи поток переходит на СЛЕДУЮЩИЙ туннель: подвисший
            # SSH-туннель держит все свои соединения, и переподключение через
            # него же ничего не даёт.
            esess = [None]
            tunnel = [wid]

            def session_for(mr):
                if mr[3] is None or not pool:
                    return slot.session
                if esess[0] is None:
                    px = pool[tunnel[0] % len(pool)]
                    esess[0] = requests.Session()
                    esess[0].trust_env = False
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
                    tunnel[0] += 1
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
                    start = i * chunk
                    end = min(size, start + chunk) - 1
                    t_req = time.monotonic()
                    mi, mr = m, mirrors[m]
                    try:
                        r = session_for(mr).get(
                            mr[0], headers={**headers, "Range": f"bytes={start}-{end}"},
                            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT if mr[3] is None
                                     else EGRESS_READ_TIMEOUT), stream=True)
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
                                if i not in finished:
                                    partial[i] = max(partial.get(i, 0), pos - start)
                        with lock:
                            fails[0] = 0
                            mr[1] += 1
                            forbidden.pop(mi, None)
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
                                partial.pop(i, None)
                                got[0] += end - start + 1
                            elif i not in finished:
                                todo.put(i)
                        if demand is not None:
                            demand.notify()
                        backoff = 0.0
                    except Exception as e:                    # noqa: BLE001
                        _fail_kind(e, mr[3])
                        with lock:
                            fails[0] += 1
                            mr[2] += 1
                        _count("conn_fail")
                        if str(e) == DEAD_LINK_CODE:
                            # Сервер ответил — DPI его не режет, в бан не
                            # заносим (иначе его обходили бы все загрузки).
                            with lock:
                                forbidden[mi] = forbidden.get(mi, 0) + 1
                                if forbidden[mi] >= DEAD_LINK_FAILS:
                                    dead.add(mi)
                        else:
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
                        # начинал пускать. На отказ «слишком часто» — дольше.
                        backoff = min(MAX_BACKOFF, backoff * 2 or 0.5)
                        stop.wait(RATE_LIMIT_PAUSE if str(e) in RATE_LIMIT_CODES else backoff)
                    finally:
                        with lock:
                            inflight[i] -= 1
                            if inflight[i] <= 0:
                                inflight.pop(i, None)
            finally:
                slot.busy = False

        slots = _take_slots(WORKERS if size < BIG_FILE
                            else WORKERS_EGRESS_BIG if egress_only else WORKERS_BIG)
        threads = [threading.Thread(target=worker, args=(s, n), daemon=True)
                   for n, s in enumerate(slots)]
        resolving = threading.Event()
        rounds = [0]                    # раундов повторного разбора за файл
        # Первый поиск запасного сервера — сразу, если соединения не поднялись.
        last_resolve = [time.monotonic() - RESOLVE_EVERY]

        def resolve():
            """Повторно разобрать ролик и добавить зеркало на другом сервере."""
            try:
                for new in _fresh_urls(info_dict, size):
                    host = urlparse(new).netloc
                    with lock:
                        # Тот же сервер, но у старой ссылки 403 — свежая
                        # ссылка её заменяет.
                        same = [i for i in dead if mirrors[i][3] is None
                                and urlparse(mirrors[i][0]).netloc == host]
                        if same and new != mirrors[same[0]][0]:
                            mirrors[same[0]][0] = new
                            dead.discard(same[0])
                            forbidden.pop(same[0], None)
                        elif (len(mirrors) < MAX_MIRRORS
                                and all(x[3] is not None or urlparse(x[0]).netloc != host
                                        for x in mirrors)):
                            mirrors.append([new, 0, 0, None])
                            with _stats_lock:
                                STATS["mirrors"] = max(STATS["mirrors"], len(mirrors))
            finally:
                last_resolve[0] = time.monotonic()
                resolving.clear()
        egress_state = {"started": any(x[3] for x in mirrors), "running": False,
                        "prefetched": None}

        def prefetch_egress():
            """Ссылку для egress готовим заранее, в фоне: если прямой путь не
            пойдёт, переключение мгновенное, без разбора ролика в этот момент."""
            try:
                egress_state["prefetched"] = _fresh_url(info_dict, size, proxy=pool[0])
            except Exception:                    # noqa: BLE001
                pass

        def resolve_egress():
            """Ссылка на тот же формат, полученная разбором ЧЕРЕЗ egress."""
            try:
                new = egress_state["prefetched"] or _fresh_url(info_dict, size, proxy=pool[0])
                if new:
                    with lock:
                        mirrors.append([new, 0, 0, "egress"])
                        with _stats_lock:
                            STATS["mirrors"] = max(STATS["mirrors"], len(mirrors))
            finally:
                egress_state["running"] = False

        def maybe_prefetch(now_bytes: int) -> None:
            """Готовим egress-ссылку заранее — но не на каждый файл (лишние
            запросы к YouTube с IP узла): сразу, если сервер уже в бане, или
            если за секунду не пришло ни байта."""
            if (not pool or egress_state["started"] or egress_state.get("pf_started")):
                return
            if _m_banned(mirrors[0]) or (now_bytes == 0 and time.time() - started > 1):
                egress_state["pf_started"] = True
                threading.Thread(target=prefetch_egress, daemon=True).start()

        started = time.time()
        phase = [None]
        links_dead = [False]

        def more_links_possible() -> bool:
            """Может ли ещё появиться ссылка: идёт разбор или раунды не кончились."""
            if resolving.is_set() or egress_state["running"]:
                return True
            if (not egress_only and rounds[0] < MAX_RESOLVE_ROUNDS
                    and len(mirrors) < MAX_MIRRORS):
                return True
            return bool(pool) and not egress_state["started"]

        def set_phase(name: str) -> None:
            """Сообщить, что происходит до первых байт: «подключаемся» или
            «сервер заблокирован — качаем в обход». Только при смене фазы."""
            if phase[0] != name:
                phase[0] = name
                if demand is not None:
                    demand.phase = name         # сообщит репортёр сессии
                    return
                self._hook_progress({"status": "downloading", "filename": filename,
                                     "downloaded_bytes": 0, "total_bytes": size,
                                     "ytg_phase": name}, info_dict)

        set_phase("connect")
        for t in threads:
            t.start()
        last_t, last_b, speed = started, 0, None
        # По запросу простаивать — норма (ffmpeg не читает): повторные разборы
        # и egress — только когда нужный кусок не идёт (иначе лишние разборы
        # с нашего IP — прямой путь к антиботу YouTube).
        moved = [time.monotonic(), -1]          # когда в последний раз шли байты
        try:
            while True:
                time.sleep(0.5)
                with lock:
                    done_now = min(size, got[0] + sum(partial.values()))
                    all_done = (len(finished) == n_chunks if demand is None
                                else demand.closed)
                    waiting = demand is not None and any(
                        c not in finished for c in demand.wanted())
                if all_done or stop.is_set():
                    break
                with lock:
                    all_dead = len(dead) == len(mirrors)
                if all_dead and not more_links_possible():
                    links_dead[0] = True
                    break
                # Живых соединений мало — сервер, скорее всего, под DPI:
                # в фоне ищем тот же файл на другом сервере (то же, что даёт
                # ручной перезапуск загрузки).
                mono = time.monotonic()
                with lock:
                    n_alive = sum(1 for t in alive.values() if mono - t < ALIVE_WINDOW)
                if demand is not None:
                    if not waiting or done_now != moved[1]:
                        moved[:] = [mono, done_now]
                    stalled = mono - moved[0]
                    # «Мало живых» по запросу: нужное не идёт DEMAND_STALL с.
                    few_alive = stalled > DEMAND_STALL
                else:
                    stalled = None
                    few_alive = n_alive < len(slots) // 2
                if (few_alive and not resolving.is_set()
                        and not egress_only and rounds[0] < MAX_RESOLVE_ROUNDS
                        and mono - last_resolve[0] > RESOLVE_EVERY
                        and len(mirrors) < MAX_MIRRORS):
                    rounds[0] += 1
                    resolving.set()
                    threading.Thread(target=resolve, daemon=True).start()
                if demand is None or stalled > 1:
                    maybe_prefetch(done_now if demand is None else 0)
                # Запасной путь через egress: все прямые серверы в бане или
                # долго нет ни одного живого соединения. Прямые попытки при
                # этом продолжаются — кто первый отдаст, тот и качает.
                if (pool and not egress_state["started"] and not egress_state["running"]
                        and (demand is None or waiting)):
                    with lock:
                        direct = [x for x in mirrors if x[3] is None]
                    all_banned = bool(direct) and all(_m_banned(x) for x in direct)
                    starving = (n_alive == 0 and time.time() - started > EGRESS_AFTER
                                if demand is None else stalled > EGRESS_AFTER)
                    if all_banned or starving:
                        egress_state.update(started=True, running=True)
                        threading.Thread(target=resolve_egress, daemon=True).start()
                        if done_now == 0:
                            set_phase("bypass")
                now = time.time()
                if demand is not None:
                    continue       # прогресс по запросу сообщает сессия (что отдано ffmpeg)
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
                if demand is None:
                    os.close(fd)
            # Потоки, дочитывающие медленный ответ, не ждём: файлу они уже
            # не пишут (stop под wlock), а слот остаётся занят до их конца.
            for t in threads:
                t.join(timeout=0.5)
            if demand is not None:
                # Движок встал — ждущие куски не придут: сервер рвёт ответы,
                # ffmpeg сразу получает обрыв, а не висит до сторожа.
                demand.stopped()

        if demand is not None:
            # Ошибку увидит ffmpeg (оборванный ответ); report_error из
            # потока движка поднял бы исключение не там, где его ждут.
            return demand.closed and not links_dead[0]

        if got[0] != size:
            self.report_error("ссылка на файл больше не действует (HTTP 403)" if links_dead[0]
                              else "не удалось скачать файл целиком: источник не отвечает")
            return False
        self.try_rename(tmp, filename)
        self._hook_progress({
            "status": "finished", "filename": filename,
            "downloaded_bytes": size, "total_bytes": size,
            "elapsed": time.time() - started,
        }, info_dict)
        return True


class _Demand:
    """Режим «по запросу»: какие куски файла сейчас нужны читателю (ffmpeg
    через локальный HTTP-сервер сессии) и что уже скачано в файл-подложку."""

    def __init__(self, fd: int, size: int) -> None:
        self.fd = fd                        # разреженный файл во всю длину, уже удалён с диска
        self.size = size
        self.chunk = _chunk_of(size)
        self.finished: set[int] = set()     # пишет движок под своим замком
        self.cv = threading.Condition()
        self._want: dict[int, int] = {}     # кусок -> сколько ответов его ждут
        self.closed = False                 # сессия закрыта: движку — выход
        self.dead = False                   # движок встал: новых кусков не будет
        self.phase: str | None = None
        self.served = 0                     # отдано ffmpeg, байт
        self.taken: dict[int, float] = {}   # кусок -> когда за ним пошли (под замком движка)

    def wanted(self) -> list[int]:
        with self.cv:
            return sorted(self._want)

    def want(self, chunks) -> None:
        with self.cv:
            for c in chunks:
                self._want[c] = self._want.get(c, 0) + 1
            self.cv.notify_all()

    def unwant(self, chunks) -> None:
        with self.cv:
            for c in chunks:
                n = self._want.get(c, 0) - 1
                if n > 0:
                    self._want[c] = n
                else:
                    self._want.pop(c, None)

    def notify(self) -> None:
        with self.cv:
            self.cv.notify_all()

    def wait(self, timeout: float) -> None:
        with self.cv:
            self.cv.wait(timeout)

    def stopped(self) -> None:
        with self.cv:
            self.dead = True
            self.cv.notify_all()

    def wait_chunk(self, c: int) -> bool:
        """Дождаться куска. False — не придёт (движок встал / сессия закрыта)."""
        with self.cv:
            while c not in self.finished:
                if self.dead or self.closed:
                    return False
                self.cv.wait(1.0)
            return True


def _workers_for(size: int, egress_only: bool) -> int:
    return (WORKERS if size < BIG_FILE
            else WORKERS_EGRESS_BIG if egress_only else WORKERS_BIG)


def partial_ok(formats: list[dict], params: dict) -> bool:
    """Отрезок можно качать «по запросу»: все выбранные дорожки — файлы
    с googlevideo, которые берёт RaceFD. Иначе (HLS и т.п.) — целиком и
    обрезка потом."""
    return bool(formats) and all(suitable(f, params) for f in formats)


class SectionSession:
    """Отрезок ролика без скачивания целого файла.

    yt-dlp режет отрезок одним ffmpeg (-ss/-t на каждый вход) — тот по
    индексу файла (sidx/Cues) читает только голову и нужный кусок. Но
    напрямую к googlevideo ffmpeg не годится: ни нашего IP для YouTube, ни
    обхода DPI, ни зеркал. Поэтому ffmpeg получает ссылки на локальный
    HTTP-сервер, а тот отдаёт байты, которые по запросу качает RaceFD со
    всеми своими обходами, в разреженный файл-подложку."""

    def __init__(self, ydl, formats: list[dict], est_total: int | None,
                 should_stop=lambda: False) -> None:
        import secrets
        import tempfile
        from http.server import ThreadingHTTPServer
        self._ydl = ydl
        self._should_stop = should_stop
        self.est_total = est_total
        self.token = secrets.token_hex(12)
        self.demands: list[_Demand] = []
        self.engines: list[threading.Thread] = []
        egress_only = bool(ydl.params.get("proxy")) and bool(config.EGRESS_POOL)
        self.ahead_max: list[int] = []
        # FFmpegFD передаёт ffmpeg прокси задачи через HTTP_PROXY; к нашему
        # серверу — только напрямую.
        np = os.environ.get("no_proxy", "")
        if "127.0.0.1" not in np:
            os.environ["no_proxy"] = ",".join(x for x in (np, "127.0.0.1", "localhost") if x)
        for f in formats:
            size = _size_of(f)
            # Файл-подложка: вне папки загрузок (квота считает st_size, а он
            # во всю длину ролика) и сразу удалён — живёт, пока открыт, и не
            # останется мусором, даже если процесс убьют.
            fd, path = tempfile.mkstemp(prefix="ytg-part-")
            os.unlink(path)
            os.ftruncate(fd, size)
            d = _Demand(fd, size)
            self.demands.append(d)
            self.ahead_max.append(max(2, min(_workers_for(size, egress_only),
                                             DEMAND_AHEAD // d.chunk)))
            eng = RaceFD(ydl, ydl.params)
            t = threading.Thread(target=self._run_engine, args=(eng, f, d), daemon=True)
            self.engines.append(t)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        for t in self.engines:
            t.start()
        self._closed = False
        # ffmpeg закончил (FFmpegFD сообщил finished) — репортёр замолкает:
        # иначе его запоздалое «скачивание» перебило бы «обработку».
        self._hlock = threading.Lock()
        self._done = False
        ydl.add_progress_hook(self._on_progress)
        self._reporter = threading.Thread(target=self._report, daemon=True)
        self._reporter.start()

    @staticmethod
    def _run_engine(eng: RaceFD, f: dict, d: _Demand) -> None:
        try:
            eng.real_download(None, f, demand=d)
        except Exception:                       # noqa: BLE001
            pass
        finally:
            d.stopped()

    def url(self, n: int) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{self.token}/{n}"

    def _handler(self):
        sess = self
        from http.server import BaseHTTPRequestHandler

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):          # noqa: D401
                pass

            def _target(self):
                parts = self.path.strip("/").split("/")
                if len(parts) != 2 or parts[0] != sess.token or not parts[1].isdigit():
                    return None
                n = int(parts[1])
                return (n, sess.demands[n]) if n < len(sess.demands) else None

            def _range(self, size: int):
                m = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "bytes=0-")
                if not m:
                    return 0, size - 1
                a = int(m.group(1))
                b = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                return a, b

            def do_HEAD(self):
                self._serve(body=False)

            def do_GET(self):
                self._serve(body=True)

            def _serve(self, body: bool) -> None:
                t = self._target()
                if t is None:
                    self.send_error(404)
                    return
                n, d = t
                a, b = self._range(d.size)
                if a > b or a >= d.size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{d.size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {a}-{b}/{d.size}")
                self.send_header("Content-Length", str(b - a + 1))
                self.end_headers()
                # Соединение — на один ответ: оборванный ответ (движок встал)
                # иначе оставил бы ffmpeg ждать недостающие байты без конца.
                self.close_connection = True
                if body:
                    sess._stream(n, d, a, b, self.wfile)

        return H

    def _stream(self, n: int, d: _Demand, a: int, b: int, out) -> None:
        """Отдать [a, b] по мере скачивания. Упреждение растёт вдвое с
        каждым отданным куском (до DEMAND_AHEAD): голову файла ffmpeg читает
        по чуть-чуть и уходит — лишнего не качаем, а сам отрезок — подряд,
        на много соединений. Читатель ушёл — его куски больше не нужны."""
        last_chunk = b // d.chunk
        pos, ahead = a, 1
        mine: set[int] = set()
        try:
            while pos <= b:
                c = pos // d.chunk
                need = set(range(c, min(last_chunk, c + ahead - 1) + 1)) - mine
                if need:
                    d.want(need)
                    mine |= need
                if not d.wait_chunk(c):
                    return                      # оборвать: ffmpeg получит ошибку сразу
                stop = min(b + 1, (c + 1) * d.chunk)
                while pos < stop:
                    # Порциями: прогресс и счёт отданного — по мере того,
                    # как ffmpeg читает, а не мегабайтами.
                    data = os.pread(d.fd, min(stop - pos, 64 << 10), pos)
                    if not data:
                        return
                    out.write(data)
                    pos += len(data)
                    with d.cv:
                        d.served += len(data)
                d.unwant([c])
                mine.discard(c)
                ahead = min(self.ahead_max[n], ahead * 2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                # ffmpeg дочитал и закрыл — это норма
        finally:
            d.unwant(mine)

    def _report(self) -> None:
        """Прогресс для интерфейса и сторожей: сколько отдано ffmpeg. FFmpegFD
        сам молчит до конца, а без событий сторож простоя снял бы задачу.
        Хук может оборвать задачу (размер, место, отмена) — тогда сессия
        закрывается, и ffmpeg получает обрыв."""
        started, last_b, last_t, speed = time.time(), -1, time.time(), None
        phase_sent = None
        while not self._closed:
            time.sleep(0.5)
            try:
                if self._should_stop():
                    raise RuntimeError("stop")
                served = sum(d.served for d in self.demands)
                phase = next((d.phase for d in self.demands if d.phase == "bypass"),
                             "connect")
                if served == 0 and phase != phase_sent:
                    phase_sent = phase
                    self._hook({"status": "downloading", "filename": "-",
                                "downloaded_bytes": 0, "total_bytes": self.est_total,
                                "ytg_phase": phase})
                if served == last_b:
                    continue
                now = time.time()
                if last_b >= 0:
                    inst = (served - last_b) / max(now - last_t, 1e-3)
                    speed = inst if speed is None else speed * 0.7 + inst * 0.3
                last_b, last_t = served, now
                total = max(self.est_total or 0, served) or None
                self._hook({"status": "downloading", "filename": "-",
                            "downloaded_bytes": served, "total_bytes": total,
                            "speed": speed, "elapsed": now - started,
                            "eta": int((total - served) / speed) if speed and total else None})
            except Exception:                   # noqa: BLE001
                self.abort()
                return

    def _on_progress(self, d: dict) -> None:
        if d.get("status") == "finished" and d.get("filename") != "-":
            with self._hlock:
                self._done = True

    def _hook(self, d: dict) -> None:
        with self._hlock:
            if self._done:
                return
            for ph in list(getattr(self._ydl, "_progress_hooks", [])):
                if ph != self._on_progress:
                    ph(d)

    def abort(self) -> None:
        """Оборвать отдачу: ffmpeg получит обрыв и завершится с ошибкой."""
        for d in self.demands:
            d.stopped()
        try:
            # И слушающий сокет: новое соединение (ffmpeg перематывает)
            # получит отказ сразу, а не повиснет в очереди без ответа.
            self.server.shutdown()
            self.server.server_close()
        except Exception:                       # noqa: BLE001
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ydl._progress_hooks.remove(self._on_progress)
        except (AttributeError, ValueError):
            pass
        for d in self.demands:
            with d.cv:
                d.closed = True
                d.cv.notify_all()
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:                       # noqa: BLE001
            pass
        alive = False
        for t in self.engines:
            t.join(timeout=3)
            alive = alive or t.is_alive()
        # Движок, не успевший выйти, ещё может писать в дескриптор — тогда
        # не закрываем (номер переиспользовался бы), его закроет конец процесса.
        if not alive:
            for d in self.demands:
                try:
                    os.close(d.fd)
                except OSError:
                    pass


def _section_formats(info: dict) -> list[dict]:
    """Дорожки задачи с полями ролика (как их видит загрузчик одной дорожки)."""
    reqs = info.get("requested_formats")
    if not reqs:
        return [info]
    base = {k: v for k, v in info.items() if k != "requested_formats"}
    return [{**base, **f} for f in reqs]


class SectionPP:
    """Постпроцессор before_dl: отрезок YouTube — через SectionSession.

    Ссылки выбранных дорожек подменяются на локальный сервер, и штатный
    FFmpegFD yt-dlp режет отрезок, читая только нужное. Если дорожки не
    годятся для RaceFD (HLS и т.п.), отрезок снимается: ролик качается
    целиком, а режет его исполнитель (on_fallback)."""

    def __init__(self, ydl, full_duration: float | None, on_fallback,
                 should_stop=lambda: False) -> None:
        from yt_dlp.postprocessor.common import PostProcessor

        outer = self

        class _PP(PostProcessor):
            def run(self, info):
                return outer.run(info)

        self.pp = _PP(ydl)
        self._ydl = ydl
        self.full_duration = full_duration
        self.on_fallback = on_fallback
        self.should_stop = should_stop
        self.session: SectionSession | None = None

    def run(self, info: dict):
        start, end = info.get("section_start"), info.get("section_end")
        if start is None and end is None:
            return [], info
        fmts = _section_formats(info)
        if not partial_ok(fmts, self._ydl.params):
            for k in ("section_start", "section_end", "section_title", "section_number"):
                info.pop(k, None)
            self.on_fallback()
            return [], info
        self.close()                        # прошлая попытка (повторный разбор)
        clip_len = (end if end is not None else (self.full_duration or 0)) - (start or 0)
        est = 0
        for f in fmts:
            size = _size_of(f) or 0
            if self.full_duration and clip_len > 0:
                est += int(size * min(1.0, clip_len / self.full_duration))
            elif f.get("tbr") and clip_len > 0:
                est += int(f["tbr"] * 125 * clip_len)
        self.session = SectionSession(self._ydl, fmts, est or None, self.should_stop)
        reqs = info.get("requested_formats")
        if reqs:
            info["requested_formats"] = [
                {**f, "url": self.session.url(n), "protocol": "http", "http_headers": {}}
                for n, f in enumerate(reqs)]
        else:
            info.update(url=self.session.url(0), protocol="http", http_headers={})
        return [], info

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None


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
                               # Напрямую — со своего IP для YouTube.
                               **({"proxy": proxy, "source_address": "0.0.0.0"}
                                  if proxy else
                                  {"source_address": config.YT_SOURCE_IP}
                                  if config.YT_SOURCE_IP else {})}) as ydl:
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
