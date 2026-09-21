"""Экстрактор pmvhaven.com для yt-dlp.

Сайт — приложение на Nuxt: в готовом HTML нет тега <video>, и штатный
generic-экстрактор ничего не находит. Но ссылка на HLS-плейлист лежит
прямо в полезной нагрузке страницы, поэтому достаточно её оттуда достать
и отдать yt-dlp — дальше он сам разбирает плейлист на качества.

Кладётся в yt_dlp_plugins/extractor/ рядом с приложением; yt-dlp
подхватывает такие плагины автоматически.
"""
from yt_dlp.extractor.common import InfoExtractor


class PMVHavenIE(InfoExtractor):
    IE_NAME = "pmvhaven"
    _VALID_URL = r"https?://(?:www\.)?pmvhaven\.com/video/(?P<id>[\w\-]+)"

    def _real_extract(self, url):
        video_id = self._match_id(url)
        webpage = self._download_webpage(url, video_id)

        # Ссылка приходит внутри JSON-нагрузки Nuxt, поэтому ищем её по
        # тексту страницы, а не в разметке плеера.
        m3u8_url = self._search_regex(
            r'(https?://[^"\'\\\s]+?/master\.m3u8)', webpage,
            "ссылка на плейлист", default=None)
        if not m3u8_url:
            # запасной вариант: прямой файл, если плейлиста нет
            direct = self._search_regex(
                r'(https?://[^"\'\\\s]+?\.mp4)(?![\w./])', webpage,
                "ссылка на видео", default=None)
            if not direct:
                self.raise_no_formats("Не удалось найти видео на странице",
                                      expected=True)
            formats = [{"url": direct, "ext": "mp4"}]
        else:
            m3u8_url = m3u8_url.replace("\\/", "/")
            formats = self._extract_m3u8_formats(
                m3u8_url, video_id, ext="mp4", m3u8_id="hls", fatal=True)

        return {
            "id": video_id,
            "title": (self._og_search_title(webpage, default=None)
                      or self._html_extract_title(webpage, default=None)
                      or video_id),
            "description": self._og_search_description(webpage, default=None),
            "thumbnail": self._og_search_thumbnail(webpage, default=None),
            "duration": int_or_none_safe(self._html_search_meta(
                "og:video:duration", webpage, default=None)),
            "formats": formats,
        }


def int_or_none_safe(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
