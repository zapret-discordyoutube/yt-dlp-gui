"""Плагин-экстрактор pmvhaven.

Оба проверяемых дефекта были найдены аудитом и воспроизведены:
экранированная форма ссылки не находилась вовсе, и бралась первая
попавшаяся ссылка со страницы без проверки адреса.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from yt_dlp_plugins.extractor import pmvhaven as plug  # noqa: E402


CDN = "https://cdn.pmvhaven.com/videos/x.mp4/master.m3u8"


def urls(page):
    return [c[2] for c in plug._candidates(page)]


# --- разбор страницы --------------------------------------------------------

def test_finds_plain_url():
    assert CDN in urls(f'<script>var s = "{CDN}";</script>')


def test_finds_json_escaped_url():
    """Nuxt отдаёт ссылку экранированной. Прежний шаблон исключал обратный
    слэш из класса символов, поэтому такую форму не находил вообще, а
    строка с заменой '\\/' на '/' была недостижима."""
    page = '{"src":"https:\\/\\/cdn.pmvhaven.com\\/videos\\/x.mp4\\/master.m3u8"}'
    assert CDN in urls(page)


def test_finds_unicode_escaped_url():
    page = '{"src":"https:\\u002F\\u002Fcdn.pmvhaven.com\\u002Fx\\u002Fmaster.m3u8"}'
    assert any(u.endswith("master.m3u8") for u in urls(page))


def test_playlist_preferred_over_plain_file():
    page = f'"a":"https://cdn.pmvhaven.com/v/x.mp4","b":"{CDN}"'
    assert urls(page)[0] == CDN


def test_no_media_on_page():
    assert urls("<html><body>ничего</body></html>") == []


# --- доверие к адресу -------------------------------------------------------

def test_own_host_ranked_before_foreign():
    """Раньше бралась ПЕРВАЯ ссылка: рекламной вставки перед настоящей
    хватало, чтобы увести загрузку на чужой адрес."""
    page = (f'<script src="https://ads.example.com/track/master.m3u8"></script>'
            f'<script>var s = "{CDN}";</script>')
    assert urls(page)[0] == CDN


@pytest.mark.parametrize("addr", [
    "127.0.0.1", "10.1.2.3", "192.168.0.5", "169.254.169.254", "::1",
])
def test_private_addresses_rejected(monkeypatch, addr):
    """Проверка адреса в приложении покрывает только ссылку ОТ пользователя.
    То, что выковыряно со страницы, ею не защищено."""
    import socket
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    monkeypatch.setattr(plug.socket, "getaddrinfo",
                        lambda *a, **k: [(family, None, None, "", (addr, 0))])
    assert plug._is_public_host("evil.example") is False


def test_public_address_accepted(monkeypatch):
    import socket
    monkeypatch.setattr(plug.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, None, None, "", ("93.184.216.34", 0))])
    assert plug._is_public_host("cdn.pmvhaven.com") is True


def test_unresolvable_host_rejected(monkeypatch):
    import socket
    def boom(*a, **k):
        raise socket.gaierror("нет такого")
    monkeypatch.setattr(plug.socket, "getaddrinfo", boom)
    assert plug._is_public_host("nope.invalid") is False


def test_empty_host_rejected():
    assert plug._is_public_host("") is False


# --- шаблон адреса страницы -------------------------------------------------

@pytest.mark.parametrize("url,ok", [
    ("https://pmvhaven.com/video/abc_123", True),
    ("https://www.pmvhaven.com/video/abc_123", True),
    ("https://pmvhaven.com/profile/someone", False),
    ("https://pmvhaven.com/", False),
    ("https://evil.com/video/x", False),
    ("https://pmvhaven.com.evil.com/video/x", False),
])
def test_valid_url_pattern(url, ok):
    import re
    assert bool(re.match(plug.PMVHavenIE._VALID_URL, url)) is ok
