"""The "source code" link goes to the git name zapret-links hands out (owner 2026-11-01: git2 and git.zapret.moe are blocked)."""
import re
import sys

sys.path.insert(0, "/home/codex-pve/zapret-links")
from zapret_links import client as links_client  # noqa: E402


def _source_links(page: str) -> set[str]:
    return set(re.findall(r'href="(https://[^"]+/zapretdiscordyoutube/yt-dlp-gui)"', page))


def test_both_pages_link_to_the_name_zapret_links_hands_out(client, monkeypatch):
    monkeypatch.setattr(links_client, "link", lambda family: "abcdefghij.zapret.moe")
    for path in ("/", "/stats"):
        page = client.get(path).get_data(as_text=True)
        assert _source_links(page) == {"https://abcdefghij.zapret.moe/zapretdiscordyoutube/yt-dlp-gui"}, path


def test_without_zapret_links_the_page_still_renders(client, monkeypatch):
    def broken(family):
        raise FileNotFoundError("links.json")
    monkeypatch.setattr(links_client, "link", broken)
    page = client.get("/").get_data(as_text=True)
    assert _source_links(page) == {"https://git3.zapret.moe/zapretdiscordyoutube/yt-dlp-gui"}
