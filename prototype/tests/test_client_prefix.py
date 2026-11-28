"""`client-prefix-v1`: one IPv6 network counts as one client, and the limiter's table of
clients stays small and cheap however many addresses send requests.

Before this, limits were counted per full address. One IPv6 connection is handed a whole
/64 (about 18 billion billion addresses) and can use a new one for every request, so every
request got a fresh bucket and fresh guesses at the operator token. And once 10,000 clients
were known, every new one scanned the whole table under the lock all requests wait for.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import observability
from app.config import Settings, get_settings
from app.main import create_app
from app.rate_limit import OVERFLOW, RateLimiter, client_key
from tests.auth_helpers import OP, OPERATOR

T0 = 1_800_000_000
PROXY = "10.0.0.5"
HOME = ipaddress.ip_network("2001:db8:1:2::/64")
NEIGHBOUR = ipaddress.ip_network("2001:db8:1:3::/64")


class Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def fresh(net: ipaddress.IPv6Network) -> str:
    """A new random address inside `net`, like a privacy-address rotation per request."""
    return str(net[secrets.randbits(128 - net.prefixlen)])


def make(tmp_path: Path, **overrides):
    values = dict(database_path=tmp_path / "db.sqlite3", audit_rate=0.0, audit_rate_new=0.0,
                  audit_rate_suspect=0.0, operator_token=OPERATOR, rate_limit_per_minute=240,
                  auth_failure_limit=10, trusted_proxies=(PROXY,))
    values.update(overrides)
    app = create_app(Settings(**values), clock=lambda: T0, limiter_clock=Clock())
    return TestClient(app, client=(PROXY, 40000)), app


def wrong_token(client: TestClient, address: str) -> int:
    return client.get("/v1/operator/health",
                      headers={"X-Forwarded-For": address,
                               "X-Operator-Token": "wrong-" + secrets.token_hex(12)}
                      ).status_code


# -- the grouping rule -------------------------------------------------------------------------

def test_ipv6_addresses_are_counted_per_64_block() -> None:
    assert client_key("2001:db8:1:2::abcd") == "2001:db8:1:2::/64"
    assert client_key("2001:db8:1:2:ffff:ffff:ffff:ffff") == "2001:db8:1:2::/64"
    assert client_key("2001:db8:1:3::1") == "2001:db8:1:3::/64"
    # Different ways of writing the same address land in the same place.
    assert client_key("2001:0DB8:0001:0002:0000:0000:0000:0001") == "2001:db8:1:2::/64"
    assert client_key("fe80::1%eth0") == "fe80::/64"


def test_ipv4_stays_per_address_and_non_addresses_are_kept() -> None:
    assert client_key("203.0.113.7") == "203.0.113.7"
    assert client_key("::ffff:203.0.113.7") == "203.0.113.7"
    assert client_key("testclient") == "testclient"
    assert client_key("unknown") == "unknown"


def test_other_prefix_lengths() -> None:
    assert client_key("2001:db8:1:2::abcd", 128) == "2001:db8:1:2::abcd"
    assert client_key("2001:db8:1:2::abcd", 56) == "2001:db8:1::/56"
    assert client_key("2001:db8:1:2ff::1", 48) == "2001:db8:1::/48"
    with pytest.raises(ValueError):
        RateLimiter(0, 10, 600, Clock(), ipv6_prefix=16)
    with pytest.raises(ValueError):
        RateLimiter(0, 10, 600, Clock(), ipv6_prefix=129)
    with pytest.raises(ValueError):
        RateLimiter(0, 10, 600, Clock(), max_clients=0)


# -- the attack, end to end --------------------------------------------------------------------

def test_new_addresses_from_one_network_get_no_extra_token_guesses(tmp_path: Path) -> None:
    """The attack shown before the fix: a new address per wrong operator token."""
    client, _ = make(tmp_path)
    codes = [wrong_token(client, fresh(HOME)) for _ in range(60)]
    assert codes.count(403) == 10  # AUTH_FAILURE_LIMIT, then refused unchecked
    assert codes.count(429) == 50
    assert codes[:10] == [403] * 10


def test_a_direct_ipv6_connection_is_grouped_too(tmp_path: Path) -> None:
    """No proxy: the socket's own address is grouped the same way."""
    _, app = make(tmp_path, trusted_proxies=())
    codes = []
    for _ in range(25):
        rig = TestClient(app, client=(fresh(HOME), 40000))
        codes.append(rig.get("/v1/operator/health",
                             headers={"X-Operator-Token": "wrong-token-guess"}).status_code)
    assert codes.count(403) == 10 and codes.count(429) == 15


def test_new_addresses_from_one_network_share_one_request_bucket(tmp_path: Path) -> None:
    client, _ = make(tmp_path, rate_limit_per_minute=20)
    codes = [client.get("/v1/coordinator/key",
                        headers={"X-Forwarded-For": fresh(HOME)}).status_code
             for _ in range(30)]
    assert codes.count(200) == 20 and codes.count(429) == 10


def test_the_neighbouring_network_is_not_locked_out(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    for _ in range(12):
        wrong_token(client, fresh(HOME))
    assert wrong_token(client, fresh(HOME)) == 429
    assert wrong_token(client, fresh(NEIGHBOUR)) == 403  # checked, its own guesses
    r = client.get("/v1/operator/health", headers={"X-Forwarded-For": fresh(NEIGHBOUR)} | OP)
    assert r.status_code == 200


def test_prefix_128_counts_each_address_as_before(tmp_path: Path) -> None:
    client, _ = make(tmp_path, ipv6_client_prefix=128)
    codes = [wrong_token(client, fresh(HOME)) for _ in range(20)]
    assert codes == [403] * 20


def test_operator_health_names_the_network_and_logs_one_tag(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    client, _ = make(tmp_path, auth_failure_limit=3)
    addresses = [fresh(HOME) for _ in range(4)]
    caplog.set_level(logging.INFO, logger="bnh")
    for address in addresses:
        wrong_token(client, address)
    health = client.get("/v1/operator/health",
                        headers={"X-Forwarded-For": "198.51.100.1"} | OP).json()
    limits = health["rate_limits"]
    [locked] = limits["locked_out_clients"]
    assert locked["client"] == "2001:db8:1:2::/64"  # what goes in a firewall rule
    assert limits["ipv6_client_prefix"] == 64 and limits["max_tracked_clients"] == 50_000
    fmt = observability.JsonFormatter()
    lines = [fmt.format(r) for r in caplog.records if r.name.startswith("bnh")]
    tags = {json.loads(line).get("client_tag") for line in lines} - {None}
    assert tags == {locked["client_tag"]}  # one network, one tag: lock-out and refusals
    assert not any(a in line for a in addresses for line in lines)
    assert not any("2001:db8" in line for line in lines)


def test_settings_read_the_new_values(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    assert s.ipv6_client_prefix == 64 and s.rate_limit_max_clients == 50_000
    monkeypatch.setenv("IPV6_CLIENT_PREFIX", "56")
    monkeypatch.setenv("RATE_LIMIT_MAX_CLIENTS", "2000")
    s = get_settings()
    assert s.ipv6_client_prefix == 56 and s.rate_limit_max_clients == 2000
    for name, bad in (("IPV6_CLIENT_PREFIX", "16"), ("IPV6_CLIENT_PREFIX", "129"),
                      ("RATE_LIMIT_MAX_CLIENTS", "10")):
        monkeypatch.setenv(name, bad)
        with pytest.raises(ValueError, match=name):
            get_settings()
        monkeypatch.delenv(name)


# -- a table that stays small and cheap ----------------------------------------------------------

def test_many_new_addresses_stay_cheap_and_the_table_stays_small() -> None:
    """Before: once 10,000 clients were known, each new one scanned them all under the lock
    (the second 10,000 took about 31 s). Here 30,000 new clients, 1 ms apart."""
    clock = Clock()
    lim = RateLimiter(240, 10, 600, clock)
    many = ipaddress.ip_network("10.0.0.0/8")
    addresses = [str(many[n]) for n in range(1, 30_001)]  # 30,000 different IPv4 addresses
    started = time.perf_counter()
    for n, address in enumerate(addresses):
        clock.t = n * 0.001
        assert lim.check(address, "/v1/jobs/next") is None
    assert time.perf_counter() - started < 10.0  # about 0.3 s; the old code took minutes
    # A bucket with one request taken is full again after 0.25 s, so only the last few
    # hundred milliseconds of clients are worth remembering.
    assert len(lim._clients) < 600


def test_forgetting_never_gives_back_a_used_bucket() -> None:
    clock = Clock()
    lim = RateLimiter(6, 0, 600, clock)  # 6 a minute: one comes back every 10 s
    for _ in range(6):
        assert lim.check("203.0.113.7", "/v1/jobs/next") is None
    assert lim.check("203.0.113.7", "/v1/jobs/next") == ("rate_limited", 10)
    for n in range(2_000):  # lots of strangers; each sweep looks at the oldest entries
        clock.t = 5 + n * 0.001
        lim.check(f"198.51.{n // 250}.{n % 250}", "/v1/jobs/next")
    assert lim.check("203.0.113.7", "/v1/jobs/next") == ("rate_limited", 4)  # 7 s in


def test_forgetting_never_drops_failures_or_guesses_in_progress() -> None:
    clock = Clock()
    lim = RateLimiter(240, 3, 600, clock)
    for _ in range(3):
        lim.record_auth_failure("203.0.113.7")
    assert lim.start_guess("203.0.113.8") is None  # still being checked
    for n in range(2_000):
        clock.t = 1 + n * 0.1
        lim.check(f"198.51.{n // 250}.{n % 250}", "/v1/jobs/next")
    assert lim.check("203.0.113.7", "/v1/jobs/next")[0] == "locked_out"
    assert lim._clients["203.0.113.8"].guessing == 1
    clock.t = 700  # the failures have left the window: now it may be forgotten
    for n in range(10):
        lim.check(f"192.0.2.{n}", "/v1/jobs/next")
    assert "203.0.113.7" not in lim._clients


def test_spent_entries_are_forgotten_with_everything_off() -> None:
    lim = RateLimiter(0, 0, 600, Clock())
    for n in range(1_000):
        lim.check(f"198.51.{n // 250}.{n % 250}", "/v1/jobs/next")
    assert len(lim._clients) <= 2


# -- when the table is full ---------------------------------------------------------------------

def test_a_full_table_puts_newcomers_in_one_shared_overflow_bucket() -> None:
    clock = Clock()
    lim = RateLimiter(240, 2, 600, clock, max_clients=3)
    for n in range(3):  # three clients that can't be forgotten (a failure each)
        lim.record_auth_failure(f"203.0.113.{n}")
    assert len(lim._clients) == 3
    # Newcomers share the overflow entry: two failures between them lock all of them out.
    lim.record_auth_failure("198.51.100.1")
    lim.record_auth_failure("198.51.100.2")
    assert lim.check("198.51.100.3", "/v1/jobs/next")[0] == "locked_out"
    assert lim.check("203.0.113.0", "/v1/jobs/next") is None  # known clients carry on
    assert len(lim._clients) == 3  # never more than the cap
    snap = lim.snapshot()
    assert snap["tracked_clients"] == 3 and snap["max_tracked_clients"] == 3
    assert snap["since_start"]["overflow_requests"] == 1
    assert OVERFLOW in [c["client"] for c in snap["locked_out_clients"]]
    # Once old failures leave the window there is room again, and newcomers get their own.
    clock.t = 601
    assert lim.check("198.51.100.9", "/v1/jobs/next") is None
    assert "198.51.100.9" in lim._clients


def test_overflow_guess_slots_are_given_back_to_the_overflow_entry() -> None:
    clock = Clock()
    lim = RateLimiter(0, 3, 600, clock, max_clients=1)
    lim.record_auth_failure("203.0.113.1")  # fills the table
    assert lim.start_guess("198.51.100.1") is None  # slot on the overflow entry
    assert lim._overflow is not None and lim._overflow.guessing == 1
    clock.t = 601  # room again; the same client would now get its own entry
    assert lim.check("192.0.2.1", "/v1/jobs/next") is None
    lim.finish_guess("198.51.100.1")
    assert lim._overflow.guessing == 0
    assert all(c.guessing == 0 for c in lim._clients.values())


def test_overflow_slots_are_thread_safe() -> None:
    lim = RateLimiter(0, 5, 600, Clock(), max_clients=1)
    lim.record_auth_failure("203.0.113.1")
    start = threading.Barrier(40)

    def guess(n: int) -> None:
        start.wait()
        client = f"198.51.100.{n}"
        if lim.start_guess(client) is None:
            lim.finish_guess(client)

    threads = [threading.Thread(target=guess, args=(n,)) for n in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert lim._overflow.guessing == 0 and lim._overflow_guesses == {}


def test_operator_health_warns_when_newcomers_shared_the_overflow_bucket(
        tmp_path: Path) -> None:
    client, _ = make(tmp_path, rate_limit_max_clients=2)
    for _ in range(2):  # two networks with a wrong token each fill the table
        wrong_token(client, fresh(HOME))
        wrong_token(client, fresh(NEIGHBOUR))
    other = ipaddress.ip_network("2001:db8:9:9::/64")
    health = client.get("/v1/operator/health", headers={"X-Forwarded-For": fresh(other)} | OP)
    assert health.status_code == 200
    body = health.json()
    assert body["rate_limits"]["since_start"]["overflow_requests"] >= 1
    assert any("overflow bucket" in w for w in body["warnings"])
