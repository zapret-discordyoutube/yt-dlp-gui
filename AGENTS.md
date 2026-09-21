# AGENTS.md — карта развёртывания и инфраструктуры

Заметки для будущих агентов: что где стоит на боевом хосте. Код живёт в этом
репозитории, но прод и обход блокировок настроены снаружи — здесь их адреса.

## Боевой сервис

- **Домен:** `yt.zapret.moe` → nginx → `http://127.0.0.1:8090`.
- **systemd-юнит:** `/etc/systemd/system/ytdlp-gui.service` (`ytdlp-gui.service`).
  - `WorkingDirectory=/home/codex-pve/ytdlp-gui` — прод крутится **из этого
    репозитория**. Любой `git`-коммит в файлы приложения попадёт в прод при
    следующем `systemctl restart ytdlp-gui`.
  - gunicorn: `--workers 1 --threads 64` (один процесс: **состояние задач
    держится в памяти**, поэтому рестарт стирает очередь и текущие загрузки).
  - Перезапуск: `sudo systemctl restart ytdlp-gui` (запускается от `codex-pve`,
    `sudo` без пароля доступен).
- **nginx:** `/etc/nginx/sites-available/yt.zapret.moe`. Лимиты: `limit_conn 24`
  на IP, `limit_req` 30r/m (heavy) и 240r/m (read).

### Ключевые параметры (в юните, через `Environment=`)

| Переменная | Значение | Смысл |
|---|---|---|
| `YTG_MAX_CONCURRENT` | **32** | одновременных загрузок (воркер-потоков) |
| `YTG_QUEUE_MAX` | 50 (дефолт) | сверх 32 ждут в очереди, потом 503 |
| `YTG_TASKS_MAX` | 500 (дефолт) | потолок записей задач |
| `YTG_MAX_FILESIZE_MB` | 2048 | максимум на файл |
| `YTG_DISK_QUOTA_MB` | 5120 | квота на папку загрузок |
| `MemoryMax` / `TasksMax` | 4G / 1024 | лимиты cgroup (юнит) |

> Хост — **гипервизор Proxmox с ВМ**. Юнит намеренно ограничивает ресурсы
> (`CPUWeight=20`, `IOWeight=20`, `Nice=10`) и запрещает исходящие во внутреннюю
> сеть (`IPAddressDeny` на 10/8, 172.16/12, 192.168/16 и т.д.) — не снимать.

### Как это работает внутри

- **yt-dlp вызывается в процессе** (Python API `YoutubeDL.extract_info`), а не
  подпроцессом. Отдельных `yt-dlp`-процессов в `ps` не будет; `ffmpeg` —
  подпроцесс (склейка/перекодирование).
- Один файл на задачу: имя на диске `<task_id>.<ext>`, отдача через `/api/file`.
- Здоровье и метрики (только с localhost): `curl -s http://127.0.0.1:8090/readyz`
  → `queue_pending`, `tasks_active`, `downloads_mb`, `free_disk_mb`.
  `/healthz` — грубый «жив».

## Обход блокировок (DPI по SNI)

Провайдерский DPI (ТСПУ) рвёт TLS-хендшейк по имени хоста в SNI — из-за этого
`soundcloud.com`, `x.com` и др. не открывались (`_ssl.c:1012 handshake timed
out`). Диагностика: `openssl s_client -connect <ip>:443 -servername <host>` —
если конкретный SNI даёт `Terminated`, а другой на том же IP `CONNECTED`, это
SNI-DPI, не блокировка IP.

- **Инструмент:** zapret2 (`nfqws2`), сервис `zapret-egress.service`,
  каталог `/opt/zapret-egress`. Документация: github.com/bol-van/zapret2.
- **Механизм:** nftables (`nft-apply.sh`, таблица `zapret_egress`) заворачивает
  первые пакеты исходящих TCP/443-потоков в nfqueue 200; `nfqws2` применяет
  десинхронизацию TLS ClientHello (`multidisorder`) **только для хостов из
  списка**.
- **Список хостов:** `/opt/zapret-egress/egress-hosts.txt` (root). Матчатся хост
  и все поддомены. Чтобы разблокировать новый сайт — **дописать его домен сюда**
  и `sudo systemctl restart zapret-egress`.
  - Уже добавлены: `soundcloud.com`, `sndcdn.com`, `x.com`, `twitter.com`,
    `twimg.com`, `instagram.com`, `cdninstagram.com`, `fbcdn.net`,
    `pornhub.com`, `phncdn.com` (плюс исходные `t.me`, `vkvideo.ru`,
    `xvideos.com` и т.д.).
- Проверка после добавления: `openssl s_client -connect <ip>:443 -servername
  <host> </dev/null` должен дать `CONNECTED`; затем `yt-dlp --simulate <url>`.

## Движок yt-dlp: обязательные спутники

- **curl_cffi** (в requirements.txt) — браузерный impersonate. Без него сайты
  за Cloudflare/анти-ботом (kick.com, pornhub) отдают 403/404. yt-dlp
  подхватывает автоматически.
- **deno** (в `/usr/local/bin`, есть в дефолтном PATH systemd) — JS-runtime для
  nsig-челленджа YouTube. Без него в логах предупреждение «No supported
  JavaScript runtime», часть форматов пропадает, скорость рвётся. Ставится
  бинарём с github релизов; кэш пишет в `$HOME/.cache/deno` (HOME=/tmp,
  PrivateTmp — писать можно).
- Диагностика «сайт не открывается»: `openssl s_client -connect <ip>:443
  -servername <host>` → `Terminated` = SNI-DPI (добавить в egress-hosts.txt);
  403/404 = нужен curl_cffi; 429/логин = ограничение сайта (напр. Instagram
  stories требуют авторизации — не чинится).

## Ограничения

- **DRM не поддерживается** (Widevine/PlayReady/FairPlay): Netflix, Spotify,
  Apple Music и пр. yt-dlp не расшифровывает — это не обходится настройкой.
- **Instagram stories/highlights** требуют авторизации (429 без кук); DPI
  обойдён, но приватный контент без логина не берётся.
- Рестарт прода теряет незавершённые задачи (состояние в памяти одного процесса).
