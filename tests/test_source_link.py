"""The "source code" link goes to the git name zapret-links hands out (owner 2026-11-01: git2 and git.zapret.moe are blocked).
Since 2026-11-08 (move to CT 209) the name is read from the published snapshot links.json over HTTP, not from a local package."""
import re
import time

import pytest


def _source_links(page: str) -> set[str]:
    return set(re.findall(r'href="(https://[^"]+/zapretdiscordyoutube/yt-dlp-gui)"', page))


@pytest.fixture(autouse=True)
def _fresh_cache():
    import app as app_module
    app_module._git_cache.update(host=app_module._GIT_FALLBACK, until=0.0)
    yield
    app_module._git_cache.update(host=app_module._GIT_FALLBACK, until=0.0)


def test_both_pages_link_to_the_name_zapret_links_hands_out(client, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "_fetch_links_json",
                        lambda: {"families": {"git": {"link": "abcdefghij.zapret.moe"}}})
    for path in ("/", "/stats"):
        page = client.get(path).get_data(as_text=True)
        assert _source_links(page) == {"https://abcdefghij.zapret.moe/zapretdiscordyoutube/yt-dlp-gui"}, path


def test_without_the_snapshot_the_page_still_renders(client, monkeypatch):
    import app as app_module

    def broken():
        raise OSError("links.json unreachable")
    monkeypatch.setattr(app_module, "_fetch_links_json", broken)
    page = client.get("/").get_data(as_text=True)
    assert _source_links(page) == {"https://git3.zapret.moe/zapretdiscordyoutube/yt-dlp-gui"}


@pytest.mark.parametrize("bad", ["evil.example.com", "a.zapret.moe/x", "x\"><script>.zapret.moe", ""])
def test_a_foreign_or_malformed_name_in_the_snapshot_is_ignored(client, monkeypatch, bad):
    import app as app_module
    monkeypatch.setattr(app_module, "_fetch_links_json", lambda: {"families": {"git": {"link": bad}}})
    page = client.get("/").get_data(as_text=True)
    assert _source_links(page) == {"https://git3.zapret.moe/zapretdiscordyoutube/yt-dlp-gui"}


def test_the_name_is_cached_for_ten_minutes(client, monkeypatch):
    import app as app_module
    calls = []

    def fetch():
        calls.append(1)
        return {"families": {"git": {"link": "abcdefghij.zapret.moe"}}}
    monkeypatch.setattr(app_module, "_fetch_links_json", fetch)
    client.get("/"); client.get("/stats"); client.get("/")
    assert len(calls) == 1
