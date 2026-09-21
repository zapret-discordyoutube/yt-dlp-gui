"""Экстрактор pmvhaven.com для yt-dlp.

Сайт — приложение на Nuxt: в готовом HTML нет тега <video>, и штатный
generic-экстрактор ничего не находит. Но ссылка на HLS-плейлист лежит
прямо в полезной нагрузке страницы, поэтому достаточно её оттуда достать
и отдать yt-dlp — дальше он сам разбирает плейлист на качества.

Кладётся в yt_dlp_plugins/extractor/ рядом с приложением; yt-dlp
подхватывает такие плагины автоматически.
"""
import ipaddress
import re
import socket
from urllib.parse import urlparse

from yt_dlp.extractor.common import InfoExtractor

# Ссылка в нагрузке Nuxt приходит в JSON-экранированном виде
# (`https:\/\/...\/master.m3u8`), поэтому обратный слэш обязан попадать в
# класс символов. Прежний шаблон его исключал, из-за чего экранированная
# форма не находилась вовсе, а строка с последующей заменой `\/` на `/`
# оказывалась недостижимой.
_MEDIA_RE = re.compile(
    r'https?:(?:\\?/|\\u002[fF]){2}[^"\'\s<>,]+')

# Хосты, которым доверяем медиа-ссылку со страницы. Ограничение нужно:
# без него бралась ПЕРВАЯ ссылка на странице, и достаточно было рекламной
# вставки или пользовательского текста, чтобы увести загрузку на чужой
# адрес — включая внутренний.
_ALLOWED_HOST_PARTS = ("pmvhaven",)


def _unescape(url: str) -> str:
    return url.replace("\\/", "/").replace("\\u002F", "/")


def _is_public_host(host: str) -> bool:
    """Все адреса имени должны быть публичными.

    Проверка адреса в приложении охватывает только ссылку ОТ пользователя;
    то, что выковыряно со страницы, ею не покрыто. Без этой проверки
    строка вида http://169.254.169.254/x.m3u8 в содержимом чужой страницы
    уводила бы загрузку во внутреннюю сеть.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return bool(infos)


def _candidates(webpage: str):
    """Пригодные медиа-ссылки со страницы, сначала плейлисты."""
    seen, out = set(), []
    for raw in _MEDIA_RE.findall(webpage):
        url = _unescape(raw).rstrip('\\"\'')
        # Расширение проверяем ПОСЛЕ разэкранирования и по всей ссылке:
        # нежадный шаблон обрывался на первом расширении, а в путях CDN
        # оно встречается в середине — .../x.mp4/master.m3u8.
        if not url.endswith((".m3u8", ".mp4")):
            continue
        if url in seen:
            continue
        seen.add(url)
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            continue
        host = p.hostname.lower()
        trusted = any(part in host for part in _ALLOWED_HOST_PARTS)
        out.append((0 if url.endswith(".m3u8") else 1, 0 if trusted else 1, url, host))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


class PMVHavenIE(InfoExtractor):
    IE_NAME = "pmvhaven"
    _VALID_URL = r"https?://(?:www\.)?pmvhaven\.com/video/(?P<id>[\w\-]+)"

    def _real_extract(self, url):
        video_id = self._match_id(url)
        webpage = self._download_webpage(url, video_id)

        chosen = None
        for _kind, untrusted, candidate, host in _candidates(webpage):
            if untrusted:
                # чужой хост берём только если он хотя бы публичный,
                # и всё равно после доверенных
                if not _is_public_host(host):
                    continue
            elif not _is_public_host(host):
                continue
            chosen = candidate
            break

        if not chosen:
            self.raise_no_formats(
                "Не удалось найти видео на странице", expected=True)

        if chosen.endswith(".m3u8"):
            formats = self._extract_m3u8_formats(
                chosen, video_id, ext="mp4", m3u8_id="hls", fatal=True)
        else:
            formats = [{"url": chosen, "ext": "mp4"}]

        return {
            "id": video_id,
            "title": (self._og_search_title(webpage, default=None)
                      or self._html_extract_title(webpage, default=None)
                      or video_id),
            "description": self._og_search_description(webpage, default=None),
            "thumbnail": self._og_search_thumbnail(webpage, default=None),
            "duration": _int_or_none(self._html_search_meta(
                "og:video:duration", webpage, default=None)),
            "formats": formats,
        }


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
