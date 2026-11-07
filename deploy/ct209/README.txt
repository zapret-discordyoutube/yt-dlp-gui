Образцы для CT 209 `ytdlp` (US2-7), с 2026-11-08. Ставятся копированием:
  ytdlp-gui.service            -> /etc/systemd/system/ytdlp-gui.service   (вместо <CT_ADDRESS> — адрес контейнера из MAP.md)
  nftables-ytg-isolate.conf    -> /etc/nftables.conf, затем `systemctl restart nftables`
                                  (вместо <FRONT_NGINX_ADDRESS> — адрес входного nginx CT 204, <CT_ADDRESS> не нужен)
Адреса — заглушки: репозиторий публичный, настоящие значения только в закрытых MAP.md / servers.md.
Старые юниты pve (deploy/ytdlp-gui.service, deploy/ytgrab-*) — архив: туннелей и второго IP на новом месте нет.
