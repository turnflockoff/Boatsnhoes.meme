"""Which address a request really came from, when the coordinator sits behind a proxy.

Threat model #17 follow-up. Request limits and the failed-login lock-out (app/rate_limit.py)
count per client address. Behind a reverse proxy (nginx, Caddy, a tunnel, a load balancer)
every request arrives from the proxy's address, so all rigs would share one bucket and one
rig with a bad key could lock everyone out. Proxies pass the real address on in the
`X-Forwarded-For` header, but anyone can *send* that header, so it may only be believed when
the connection itself comes from a proxy the operator listed.

`TRUSTED_PROXIES` (comma or space separated addresses or ranges, e.g.
`127.0.0.1, 10.0.0.0/8`; empty by default = the header is ignored):

- If the connecting address isn't listed, it is the client. The header is ignored, so a rig
  can't pick its own address to dodge a lock-out.
- If it is listed, the header is read **from the right**. Each proxy appends the address it
  saw, so the rightmost entries were written by our own proxies and anything further left may
  be made up by the client. The first address (from the right) that isn't a trusted proxy is
  the client.
- No header, or an entry that isn't an address: we stop at the last address we can vouch for
  (usually the proxy). Those requests share that address's limits, and the operator health
  page counts them and warns, because it usually means the proxy isn't set up to send it.

"Trust everyone" (`0.0.0.0/0`, `::/0`) is refused: it would let any client choose its address.

uvicorn has its own proxy-header handling (on by default for 127.0.0.1). Start it with
`--no-proxy-headers` so `TRUSTED_PROXIES` is the one setting that decides this.

Only `X-Forwarded-For` is read, not the newer `Forwarded` header; nginx, Caddy, Cloudflare
and most load balancers send `X-Forwarded-For`.
"""
from __future__ import annotations

import ipaddress
import re
import threading
from typing import Any, Iterable

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address

# Walk at most this many hops from the right. Real chains are 1-3 proxies long.
MAX_HOPS = 20

_IPV4_WITH_PORT = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):\d{1,5}$")
_BRACKETED_V6 = re.compile(r"^\[([0-9A-Fa-f:.]+)\](?::\d{1,5})?$")


def parse_trusted_proxies(raw: str | Iterable[str] | None) -> tuple[Network, ...]:
    """Parse the TRUSTED_PROXIES setting. Raises ValueError with a plain message."""
    if raw is None:
        return ()
    items = re.split(r"[\s,]+", raw) if isinstance(raw, str) else list(raw)
    networks: list[Network] = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise ValueError(f"TRUSTED_PROXIES: {item!r} is not an IP address or range") from exc
        if network.prefixlen == 0:
            raise ValueError(f"TRUSTED_PROXIES: {item!r} would trust every address, so any "
                             "client could choose its own address. List your proxies instead")
        if network not in networks:
            networks.append(network)
    return tuple(networks)


def _address(text: str) -> Address | None:
    """An address from a header entry or a socket peer; None if it isn't one.
    Accepts `1.2.3.4:port` and `[v6]:port`; IPv4-mapped IPv6 becomes plain IPv4."""
    text = text.strip()
    for pattern in (_IPV4_WITH_PORT, _BRACKETED_V6):
        match = pattern.match(text)
        if match:
            text = match.group(1)
            break
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


class ClientResolver:
    def __init__(self, trusted: Iterable[Network]) -> None:
        self.trusted = tuple(trusted)
        self._lock = threading.Lock()
        self.via_proxy = 0
        self.header_missing = 0
        self.header_malformed = 0

    def _is_trusted(self, addr: Address | None) -> bool:
        return addr is not None and any(addr.version == n.version and addr in n
                                        for n in self.trusted)

    def resolve(self, peer: str, forwarded: Iterable[str]) -> str:
        """The client's address. `peer` is the connecting address, `forwarded` every
        X-Forwarded-For header value in the order received. Call once per request: it
        counts what it saw for the operator health page."""
        peer_addr = _address(peer)
        if not self._is_trusted(peer_addr):
            return peer
        hops = [h.strip() for value in forwarded for h in value.split(",")]
        hops = [h for h in hops if h]
        with self._lock:
            self.via_proxy += 1
            if not hops:
                self.header_missing += 1
                return str(peer_addr)
        vouched = str(peer_addr)
        for hop in list(reversed(hops))[:MAX_HOPS]:
            addr = _address(hop)
            if addr is None:
                with self._lock:
                    self.header_malformed += 1
                return vouched
            if not self._is_trusted(addr):
                return str(addr)
            vouched = str(addr)
        # Every hop we looked at is one of our proxies: the furthest is the best we know.
        return vouched

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"trusted_proxies": [str(n) for n in self.trusted],
                    "via_proxy_requests": self.via_proxy,
                    "forwarded_header_missing": self.header_missing,
                    "forwarded_header_malformed": self.header_malformed}

    def warnings(self) -> list[str]:
        snap = self.snapshot()
        out = []
        if snap["forwarded_header_missing"]:
            out.append(f"{snap['forwarded_header_missing']} request(s) came from a trusted proxy "
                       "with no X-Forwarded-For header, so those clients share the proxy's "
                       "request limits and lock-out. Check the proxy is set to add it")
        if snap["forwarded_header_malformed"]:
            out.append(f"{snap['forwarded_header_malformed']} request(s) had an X-Forwarded-For "
                       "entry that isn't an address; they share the last proxy's limits. "
                       "Check TRUSTED_PROXIES lists every proxy in the chain")
        return out
