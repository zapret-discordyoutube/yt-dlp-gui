"""Разбор ссылок, защита от SSRF и path traversal, имена файлов."""
import pytest

import config
import downloader as dl


# --- validate_url -----------------------------------------------------------

@pytest.mark.parametrize("url", [
    "ftp://example.com/x",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "",
    "   ",
    "https://",
])
def test_validate_url_rejects_bad_schemes(url):
    with pytest.raises(ValueError):
        dl.validate_url(url)


def test_validate_url_rejects_too_long():
    with pytest.raises(ValueError) as e:
        dl.validate_url("https://example.com/" + "a" * 2100)
    assert str(e.value) == "empty_or_too_long"


@pytest.mark.parametrize("addr", [
    "127.0.0.1", "10.1.2.3", "192.168.0.5", "172.16.9.9",
    "169.254.169.254",          # метаданные облака
    "::1", "fe80::1", "fc00::1",
])
def test_validate_url_rejects_private_addresses(monkeypatch, addr):
    import socket
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    monkeypatch.setattr(dl.socket, "getaddrinfo",
                        lambda *a, **k: [(family, None, None, "", (addr, 0))])
    with pytest.raises(ValueError) as e:
        dl.validate_url("https://evil.example/x")
    assert str(e.value) == "private_host"


def test_validate_url_rejects_host_with_any_private_address(monkeypatch):
    """Имя может резолвиться в несколько адресов. Если хотя бы один
    приватный — отказываем: иначе обход тривиален."""
    import socket
    monkeypatch.setattr(dl.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, None, None, "", ("93.184.216.34", 0)),
        (socket.AF_INET, None, None, "", ("127.0.0.1", 0)),
    ])
    with pytest.raises(ValueError):
        dl.validate_url("https://mixed.example/x")


def test_validate_url_accepts_public(monkeypatch):
    import socket
    monkeypatch.setattr(dl.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, None, None, "", ("93.184.216.34", 0))])
    assert dl.validate_url("https://example.com/v") == "https://example.com/v"


def test_validate_url_rejects_unresolvable(monkeypatch):
    import socket
    def boom(*a, **k):
        raise socket.gaierror("nope")
    monkeypatch.setattr(dl.socket, "getaddrinfo", boom)
    with pytest.raises(ValueError):
        dl.validate_url("https://nonexistent.invalid/x")


# --- canonical_url ----------------------------------------------------------

@pytest.mark.parametrize("given,expected", [
    ("https://www.youtube.com/watch?v=aqz-KE-bpKQ",
     "https://www.youtube.com/watch?v=aqz-KE-bpKQ"),
    ("https://youtu.be/aqz-KE-bpKQ?t=30",
     "https://www.youtube.com/watch?v=aqz-KE-bpKQ"),
    ("https://m.youtube.com/watch?v=aqz-KE-bpKQ&list=PL1&index=4",
     "https://www.youtube.com/watch?v=aqz-KE-bpKQ"),
    ("https://www.youtube.com/shorts/abc123DEF45",
     "https://www.youtube.com/watch?v=abc123DEF45"),
    ("https://vk.ru/video-20225241_456251102",
     "https://vkvideo.ru/video-20225241_456251102"),
    ("https://vkvideo.ru/video-20225241_456251102?t=5",
     "https://vkvideo.ru/video-20225241_456251102"),
])
def test_canonical_url_collapses_same_video(given, expected):
    assert dl.canonical_url(given) == expected


def test_canonical_url_strips_secrets():
    """Лента публичная: токены из ссылки публиковать нельзя."""
    out = dl.canonical_url(
        "https://example.com/v?token=SECRET&api_key=K&id=7&utm_source=tg")
    assert "SECRET" not in out and "api_key" not in out
    assert "id=7" in out


def test_canonical_url_is_stable():
    """Порядок параметров и регистр хоста не должны плодить дубликаты."""
    a = dl.canonical_url("https://Example.com/p?b=2&a=1#frag")
    b = dl.canonical_url("https://example.com/p/?a=1&b=2")
    assert a == b
    assert "#" not in a


def test_canonical_url_survives_garbage():
    for bad in ["", "не ссылка", "https://"]:
        dl.canonical_url(bad)      # не должно бросать


# --- safe_download_path -----------------------------------------------------

@pytest.mark.parametrize("name", [
    "../../etc/passwd", "sub/dir/file.mp4", "/etc/passwd",
    "a\x00b", "", "..",
])
def test_safe_download_path_rejects_traversal(name):
    assert dl.safe_download_path(name) is None


def test_safe_download_path_accepts_real_file():
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    f = config.DOWNLOAD_DIR / "deadbeef.mp4"
    f.write_bytes(b"x")
    try:
        assert dl.safe_download_path("deadbeef.mp4") == f.resolve()
    finally:
        f.unlink()


def test_safe_download_path_rejects_symlink_outside(tmp_path):
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("секрет")
    link = config.DOWNLOAD_DIR / "link.mp4"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(outside)
    try:
        assert dl.safe_download_path("link.mp4") is None
    finally:
        link.unlink()


# --- pretty_filename --------------------------------------------------------

def test_pretty_filename_keeps_cyrillic():
    assert dl.pretty_filename("Ролик про кота", ".mp4") == "Ролик про кота.mp4"


def test_pretty_filename_handles_trailing_dots():
    """Заголовок, кончающийся точками, когда-то вырождался в '..' и ломал
    выдачу файла — регрессия на этот случай."""
    out = dl.pretty_filename("стало еще хуже..", ".mp4")
    assert out == "стало еще хуже.mp4"
    assert not out.startswith(".")


def test_pretty_filename_strips_dangerous_chars():
    out = dl.pretty_filename('a/b\\c:d*e?f"g<h>i|j\r\n', ".mp3")
    for ch in '/\\:*?"<>|\r\n':
        assert ch not in out


def test_pretty_filename_never_empty():
    assert dl.pretty_filename("", ".mp4").endswith(".mp4")
    assert len(dl.pretty_filename("", ".mp4")) > 4
    assert dl.pretty_filename("///", ".mp4").endswith(".mp4")


def test_pretty_filename_truncates():
    out = dl.pretty_filename("я" * 500, ".mp4")
    assert len(out) <= 124
