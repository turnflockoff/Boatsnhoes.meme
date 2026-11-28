"""Content guards for the public subpages (the owner's homepage, index.html, is not checked).

The project's hard rules say no price, profit or "moon" claims anywhere, and that the site must
not pretend a coin, sale or download exists before it does. These tests keep those rules true
for every subpage, including /stay-safe, and make sure the safety page stays reachable.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app import a11y_check, site_check

SITE = Path(__file__).resolve().parents[2]
OWNER_PAGES = {"index.html"}
HIDDEN = ("docs/", "prototype/", "prompts/", ".github/")


def _subpages() -> list[str]:
    pages = []
    for path in sorted(SITE.rglob("*.html")):
        rel = path.relative_to(SITE).as_posix()
        if rel in OWNER_PAGES or rel.startswith(HIDDEN) or "/." in "/" + rel or "node_modules" in rel:
            continue
        pages.append(rel)
    return pages


class _Text(HTMLParser):
    """Visible text plus link targets, skipping <script> and <style>."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.meta: list[str] = []
        self.hrefs: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "a":
            self.hrefs.extend(v for k, v in attrs if k == "href" and v)
        for k, v in attrs:  # meta descriptions and share cards are public text too
            if tag == "meta" and k == "content" and v:
                self.meta.append(v)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _read(rel: str) -> _Text:
    p = _Text()
    p.feed((SITE / rel).read_text(encoding="utf-8"))
    return p


def _text(rel: str, meta: bool = True) -> str:
    """Visible page text; with meta=True also the description and share-card text."""
    p = _read(rel)
    return re.sub(r"\s+", " ", " ".join(p.parts + (p.meta if meta else [])))


# Hype and money claims the project rules forbid. Each pattern is matched case-insensitively
# against visible text, meta descriptions and share cards.
FORBIDDEN = [
    r"\bto the moon\b", r"\bmoon(?:ing|shot)?\b", r"\blambo\b", r"(?<!Ryzen 5 )\b\d{2,}x\b", r"\b\d+,?000%",
    r"\bguarantee(?:d|s)?\b", r"\brisk[- ]free\b", r"\bget rich\b", r"\bpassive income\b",
    r"\bROI\b", r"\bprice target\b", r"\bearn (?:up to )?\$\d", r"\bbuy now\b", r"\bpresale (?:is )?(?:open|live)\b",
    r"\bairdrop (?:is )?(?:live|open)\b", r"\bclaim your\b",
]


def _forbidden_hits(text: str) -> list[str]:
    return [m.group(0) for pat in FORBIDDEN for m in re.finditer(pat, text, re.I)]


def test_subpages_found():
    pages = _subpages()
    assert "stay-safe.html" in pages and "contribute.html" in pages and "reports/index.html" in pages
    assert "index.html" not in pages


@pytest.mark.parametrize("rel", _subpages())
def test_no_hype_or_money_claims(rel):
    assert _forbidden_hits(_text(rel)) == []


@pytest.mark.parametrize("sample", [
    "Ride $BNH to the moon", "100x soon", "Guaranteed returns", "earn $50 a day", "Presale is live",
    "risk-free", "CLAIM YOUR tokens", "mooning", "passive income from your GPU", "5,000% APY",
])
def test_claim_guard_catches_hype(sample):
    assert _forbidden_hits(sample), sample


@pytest.mark.parametrize("sample", [
    "No price, profit or payout talk.", "Not an investment", "a 10-second browser check",
    "at least 2 signers", "Nothing is for sale.", "There's no presale, private round, whitelist or airdrop.",
    "Pages that ask you to \"connect\", \"sync\", \"validate\" or \"claim\"", "1440 px and 390 px", "Do testers earn $BNH?", "Ryzen 5 5600X",
])
def test_claim_guard_allows_honest_text(sample):
    assert _forbidden_hits(sample) == []


def test_stay_safe_states_the_honest_status():
    text = _text("stay-safe.html", meta=False).lower()  # on the page itself, not only in a share card
    for must in (
        "there is no $bnh coin",
        "recovery phrase",
        "nothing is for sale",
        "no social account has been confirmed as official",
        "nothing is released yet",
        "at least two named people",
        "boatsnhoes.meme",
    ):
        assert must in text, must


def test_stay_safe_only_links_to_official_places():
    """Every outside link on the safety page goes to the project's own GitHub repo."""
    for href in _read("stay-safe.html").hrefs:
        if href.startswith(("http://", "https://")):
            assert href.startswith("https://github.com/turnflockoff/Boatsnhoes.meme"), href
        else:
            assert href.startswith(("/", "#")), href


def test_stay_safe_is_linked_from_subpages():
    for rel in ("contribute.html", "reports/index.html"):
        assert "/stay-safe" in _read(rel).hrefs, rel
    for rel in ("contribute.html", "reports/index.html"):  # in the footer, not only somewhere
        footer = (SITE / rel).read_text(encoding="utf-8").split("<footer", 1)[1]
        assert 'href="/stay-safe"' in footer, rel


def test_stay_safe_menu_script_exists_and_is_small():
    js = (SITE / "assets" / "nav.js").read_text(encoding="utf-8")
    assert "menubtn" in js and "navlinks" in js
    for risky in ("fetch(", "XMLHttpRequest", "innerHTML", "eval(", "localStorage", "document.cookie"):
        assert risky not in js


def test_stay_safe_passes_site_and_accessibility_checks():
    site = site_check.static_check(SITE)
    assert site["ok"]
    mine = [c for c in site["checks"] if c.get("page") == "stay-safe.html"]
    assert {c["name"] for c in mine} >= {"page", "share-card"}
    assert all(c["status"] == "pass" for c in mine)
    a11y = a11y_check.check_site(SITE)
    stay = [c for c in a11y["checks"] if c.get("page") == "stay-safe.html"]
    assert [c["status"] for c in stay] == ["pass"]


def test_stay_safe_in_default_live_check():
    assert "/stay-safe" in site_check.DEFAULT_LIVE_PAGES
