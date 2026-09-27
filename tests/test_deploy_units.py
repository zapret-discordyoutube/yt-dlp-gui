"""Туннели к ai-egress не раскручивают штраф sshd узла.

27.09.2026: 32 туннеля пула перезапускались одновременно каждые 5 с, sshd на
ai-egress (PerSourcePenalties) штрафовал наш IP за соединения без
аутентификации и отказывал всем туннелям — 296 перезапусков в час.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
TUNNEL_UNITS = ("ytgrab-egress-pool@.service", "ytgrab-egress-tunnel.service")


def _service(name: str) -> configparser.SectionProxy:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(DEPLOY / name, encoding="utf-8")
    return parser["Service"]


@pytest.mark.parametrize("name", TUNNEL_UNITS)
def test_restarts_back_off(name):
    service = _service(name)
    assert service["Restart"] == "always"
    assert int(service["RestartSteps"]) >= 1
    max_delay = int(service["RestartMaxDelaySec"])
    assert max_delay > int(service["RestartSec"])
    assert max_delay >= 60


def test_pool_starts_are_staggered_per_instance():
    pre = _service("ytgrab-egress-pool@.service")["ExecStartPre"]
    # systemd раскрывает одиночный $ сам; литеральный — только как $$.
    assert "$$((" in pre and "%i" in pre


def test_pool_keepalive_tolerates_a_short_stall():
    start = _service("ytgrab-egress-pool@.service")["ExecStart"]
    options = dict(
        part.split("=", 1)
        for part in start.split("-o ")[1:]
        if "=" in part
    )
    interval = int(options["ServerAliveInterval"].split()[0])
    count = int(options["ServerAliveCountMax"].split()[0])
    assert interval * count >= 30


def test_pool_covers_the_configured_port_range():
    target = (DEPLOY / "ytgrab-egress-pool.target").read_text(encoding="utf-8")
    ports = [int(p) for p in range(18110, 18142)]
    assert all(f"ytgrab-egress-pool@{port}.service" in target for port in ports)
