"""Trusted proxies (threat model #17 follow-up): behind a reverse proxy, request limits and
lock-outs apply to the real client address, and nobody else can pick their own address.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import observability
from app.client_address import ClientResolver, parse_trusted_proxies
from app.config import Settings, get_settings
from app.main import create_app
from tests.auth_helpers import OP, OPERATOR

T0 = 1_800_000_000
PROXY = "10.0.0.5"


class Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(tmp_path: Path, trusted: tuple[str, ...] = (PROXY,), peer: str = PROXY,
         per_minute: int = 240, failure_limit: int = 10):
    settings = Settings(database_path=tmp_path / "db.sqlite3", audit_rate=0.0,
                        audit_rate_new=0.0, audit_rate_suspect=0.0, operator_token=OPERATOR,
                        rate_limit_per_minute=per_minute, auth_failure_limit=failure_limit,
                        trusted_proxies=trusted)
    app = create_app(settings, clock=lambda: T0, limiter_clock=Clock())
    return TestClient(app, client=(peer, 40000)), app


def xff(value: str) -> dict[str, str]:
    return {"X-Forwarded-For": value}


def bad_key(client: TestClient, headers: dict[str, str]) -> int:
    return client.post("/v1/jobs/next",
                       headers=headers | {"Authorization": "Bearer bnhw_guess"}).status_code


# -- reading the header -----------------------------------------------------------------------

def test_no_trusted_proxies_means_the_header_is_ignored() -> None:
    r = ClientResolver(())
    assert r.resolve("203.0.113.9", ["198.51.100.1"]) == "203.0.113.9"
    assert r.snapshot()["via_proxy_requests"] == 0


def test_header_from_an_untrusted_peer_is_ignored() -> None:
    r = ClientResolver(parse_trusted_proxies(PROXY))
    assert r.resolve("203.0.113.9", ["198.51.100.1"]) == "203.0.113.9"


def test_trusted_proxy_passes_on_the_real_client() -> None:
    r = ClientResolver(parse_trusted_proxies(PROXY))
    assert r.resolve(PROXY, ["198.51.100.1"]) == "198.51.100.1"
    assert r.snapshot()["via_proxy_requests"] == 1


def test_a_client_cannot_pick_its_own_address() -> None:
    # The client sends a made-up header; the proxy appends the address it really saw.
    r = ClientResolver(parse_trusted_proxies(PROXY))
    assert r.resolve(PROXY, ["1.1.1.1, 2.2.2.2, 198.51.100.1"]) == "198.51.100.1"
    # Also when the fake arrives as a separate header line.
    assert r.resolve(PROXY, ["1.1.1.1", "198.51.100.1"]) == "198.51.100.1"


def test_chains_of_trusted_proxies_are_walked() -> None:
    r = ClientResolver(parse_trusted_proxies("10.0.0.0/8, 192.168.1.1"))
    assert r.resolve("10.0.0.5", ["9.9.9.9, 198.51.100.1, 192.168.1.1, 10.2.3.4"]) \
        == "198.51.100.1"
    # Every hop trusted: the furthest one is the best we know.
    assert r.resolve("10.0.0.5", ["10.9.9.9, 192.168.1.1"]) == "10.9.9.9"


def test_missing_header_keeps_the_proxy_address_and_is_counted() -> None:
    r = ClientResolver(parse_trusted_proxies(PROXY))
    assert r.resolve(PROXY, []) == PROXY
    assert r.resolve(PROXY, [" , "]) == PROXY
    assert r.snapshot()["forwarded_header_missing"] == 2


def test_garbage_stops_at_the_last_address_we_can_vouch_for() -> None:
    r = ClientResolver(parse_trusted_proxies("10.0.0.0/8"))
    assert r.resolve("10.0.0.5", ["not-an-ip"]) == "10.0.0.5"
    assert r.resolve("10.0.0.5", ["1.1.1.1, unknown, 10.0.0.7"]) == "10.0.0.7"
    assert r.snapshot()["forwarded_header_malformed"] == 2


def test_ports_and_ipv6_forms_are_understood() -> None:
    r = ClientResolver(parse_trusted_proxies("10.0.0.5, ::1"))
    assert r.resolve(PROXY, ["198.51.100.1:5555"]) == "198.51.100.1"
    assert r.resolve(PROXY, ["[2001:db8::7]:443"]) == "2001:db8::7"
    assert r.resolve(PROXY, ["2001:DB8::7"]) == "2001:db8::7"
    # An IPv4 proxy seen over an IPv6 socket is still the same proxy.
    assert r.resolve("::ffff:10.0.0.5", ["198.51.100.1"]) == "198.51.100.1"
    assert r.resolve("::1", ["198.51.100.1"]) == "198.51.100.1"


def test_very_long_chains_are_cut_off() -> None:
    r = ClientResolver(parse_trusted_proxies("10.0.0.0/8"))
    chain = ", ".join(f"10.0.{i // 250}.{i % 250 + 1}" for i in range(500))
    assert r.resolve("10.0.0.5", ["198.51.100.1, " + chain]).startswith("10.")


# -- the setting ------------------------------------------------------------------------------

def test_setting_rejects_bad_entries_and_trust_everyone() -> None:
    assert parse_trusted_proxies(None) == ()
    assert parse_trusted_proxies("") == ()
    assert [str(n) for n in parse_trusted_proxies("127.0.0.1 10.0.0.0/8,::1")] == \
        ["127.0.0.1/32", "10.0.0.0/8", "::1/128"]
    for bad in ("nonsense", "10.0.0.0/33", "0.0.0.0/0", "::/0"):
        with pytest.raises(ValueError):
            parse_trusted_proxies(bad)


def test_settings_read_trusted_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1, 10.0.0.0/8")
    assert get_settings().trusted_proxies == ("127.0.0.1/32", "10.0.0.0/8")
    monkeypatch.setenv("TRUSTED_PROXIES", "0.0.0.0/0")
    with pytest.raises(ValueError, match="TRUSTED_PROXIES"):
        get_settings()
    monkeypatch.delenv("TRUSTED_PROXIES")
    assert get_settings().trusted_proxies == ()


def test_create_app_refuses_a_bad_setting(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make(tmp_path, trusted=("0.0.0.0/0",))


# -- through the coordinator ------------------------------------------------------------------

def test_limits_apply_per_real_client_behind_the_proxy(tmp_path: Path) -> None:
    client, _ = make(tmp_path, per_minute=2)
    for _ in range(2):
        assert client.get("/v1/coordinator/key", headers=xff("198.51.100.1")).status_code != 429
    assert client.get("/v1/coordinator/key", headers=xff("198.51.100.1")).status_code == 429
    # A different client behind the same proxy still has its own bucket.
    assert client.get("/v1/coordinator/key", headers=xff("198.51.100.2")).status_code != 429


def test_one_bad_rig_no_longer_locks_out_its_neighbours(tmp_path: Path) -> None:
    client, _ = make(tmp_path, failure_limit=3)
    for _ in range(3):
        assert bad_key(client, xff("198.51.100.1")) == 401
    assert bad_key(client, xff("198.51.100.1")) == 429
    assert client.get("/v1/coordinator/key", headers=xff("198.51.100.2")).status_code != 429


def test_untrusted_peer_cannot_dodge_a_lock_out_with_the_header(tmp_path: Path) -> None:
    client, _ = make(tmp_path, peer="203.0.113.9", failure_limit=3)
    for i in range(3):
        assert bad_key(client, xff(f"198.51.100.{i}")) == 401
    assert client.get("/v1/coordinator/key", headers=xff("198.51.100.77")).status_code == 429


def test_without_the_setting_everyone_behind_the_proxy_shares(tmp_path: Path) -> None:
    client, _ = make(tmp_path, trusted=(), failure_limit=3)
    for _ in range(3):
        assert bad_key(client, xff("198.51.100.1")) == 401
    assert client.get("/v1/coordinator/key", headers=xff("198.51.100.2")).status_code == 429


def test_health_shows_the_setting_and_warns_about_a_missing_header(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    client, _ = make(tmp_path)
    client.get("/v1/coordinator/key")                      # proxy forgot the header
    client.get("/v1/coordinator/key", headers=xff("bogus"))
    health = client.get("/v1/operator/health", headers=OP | xff("198.51.100.1")).json()
    proxies = health["rate_limits"]["proxies"]
    assert proxies["trusted_proxies"] == ["10.0.0.5/32"]
    assert proxies["forwarded_header_missing"] == 1
    assert proxies["forwarded_header_malformed"] == 1
    assert proxies["via_proxy_requests"] == 3
    warnings = " ".join(health["warnings"])
    assert "X-Forwarded-For" in warnings and "share" in warnings


def test_no_proxy_warning_when_everything_is_fine(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    client.get("/v1/coordinator/key", headers=xff("198.51.100.1"))
    health = client.get("/v1/operator/health", headers=OP | xff("198.51.100.1")).json()
    assert not [w for w in health["warnings"] if "X-Forwarded-For" in w]


def test_real_client_address_never_reaches_the_logs(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    client, _ = make(tmp_path, failure_limit=2)
    with caplog.at_level(logging.INFO, logger="bnh"):
        for _ in range(3):
            bad_key(client, xff("198.51.100.123"))
    fmt = observability.JsonFormatter()
    text = "\n".join(fmt.format(r) for r in caplog.records)
    assert "198.51.100.123" not in text and PROXY not in text
    tags = {json.loads(fmt.format(r)).get("client_tag") for r in caplog.records} - {None}
    assert tags == {client.app.state.limiter.tag("198.51.100.123")}
    # The operator page names the real client, not the proxy, so it can be blocked.
    health = client.get("/v1/operator/health", headers=OP | xff("198.51.100.9")).json()
    assert [c["client"] for c in health["rate_limits"]["locked_out_clients"]] == \
        ["198.51.100.123"]


def test_each_request_is_worked_out_and_counted_once(tmp_path: Path) -> None:
    # A refused key goes through both the limit check and the lock-out counter.
    client, app = make(tmp_path)
    assert bad_key(client, xff("198.51.100.1")) == 401
    assert app.state.client_resolver.snapshot()["via_proxy_requests"] == 1
