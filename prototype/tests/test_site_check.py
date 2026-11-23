"""Tests for the website checker (`site-check-v1`): offline checks of the repo's pages and
`_headers`, and live checks of a deployed site (run here against a local server and a fake
fetcher, since this sandbox can't reach boatsnhoes.meme)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app import site_check as sc

REPO = Path(__file__).resolve().parents[2]

HEADERS = """/*
  Strict-Transport-Security: max-age=31536000; includeSubDomains
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: camera=()
  Content-Security-Policy: default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'
"""

NETLIFY = """[build]
  publish = "."

[[redirects]]
  from = "/docs/*"
  to = "/404.html"
  status = 404
  force = true
"""

CARD = """<meta property="og:title" content="T"><meta property="og:description" content="D">
<meta property="og:image" content="https://example.test/assets/og.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="canonical" href="https://example.test/">"""


def png(width: int, height: int) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
            + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00")


def make_site(tmp_path: Path, index_body: str = "", extra: dict[str, str] | None = None,
              headers: str = HEADERS) -> Path:
    root = tmp_path / "site"
    (root / "assets").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "_headers").write_text(headers)
    (root / "netlify.toml").write_text(NETLIFY)
    (root / "assets" / "site.css").write_text("body{color:#000}")
    (root / "assets" / "app.js").write_text("console.log('hi')")
    (root / "assets" / "og.png").write_bytes(png(1200, 630))
    (root / "docs" / "secret.md").write_text("internal")
    (root / "about.html").write_text(f"<!doctype html><html><head>{CARD}</head><body>about</body></html>")
    (root / "404.html").write_text("<!doctype html><html><head></head><body>lost</body></html>")
    (root / "index.html").write_text(
        "<!doctype html><html><head>" + CARD
        + '<link rel="stylesheet" href="/assets/site.css"><script src="/assets/app.js"></script>'
        + "</head><body><a href=\"/about\">about</a>" + index_body + "</body></html>")
    for name, text in (extra or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def problems(result: dict) -> list[str]:
    return [c["name"] + ": " + c["detail"] for c in result["checks"] if c["status"] == "fail"]


# ---------------------------------------------------------------- offline (static) checks

def test_real_repo_passes_static_checks():
    result = sc.static_check(REPO, origin="https://boatsnhoes.meme")
    assert result["format"] == sc.FORMAT and result["rule"] == sc.RULE
    assert problems(result) == []
    pages = {c["page"] for c in result["checks"] if c.get("page")}
    assert {"index.html", "contribute.html", "reports/index.html"} <= pages


def test_clean_fixture_passes(tmp_path):
    result = sc.static_check(make_site(tmp_path), origin="https://example.test")
    assert problems(result) == [], problems(result)
    assert result["ok"] is True


def test_csp_parsing_and_fallbacks():
    csp = sc.parse_csp("default-src 'self'; img-src 'self' data:; ; frame-ancestors 'none'")
    assert csp["img-src"] == ["'self'", "data:"]
    origin = "https://example.test"
    assert sc.csp_allows(csp, "script-src", "https://example.test/a.js", origin)
    assert not sc.csp_allows(csp, "script-src", "https://cdn.other/a.js", origin)
    assert sc.csp_allows(csp, "img-src", "data:image/png;base64,AA", origin)
    assert not sc.csp_allows(csp, "font-src", "data:font/woff2;base64,AA", origin)  # falls back to default-src
    # form-action never falls back to default-src: no directive means anything goes
    assert sc.csp_allows(csp, "form-action", "https://evil.test/x", origin)
    wild = sc.parse_csp("default-src 'none'; style-src https://*.googleapis.com")
    assert sc.csp_allows(wild, "style-src", "https://fonts.googleapis.com/css", origin)
    assert not sc.csp_allows(wild, "style-src", "https://googleapis.com.evil.test/css", origin)
    assert not sc.csp_allows(wild, "style-src", "https://evilgoogleapis.com/css", origin)
    assert not sc.csp_allows(wild, "style-src", "https://googleapis.com/css", origin)  # *. needs a subdomain
    assert not sc.csp_allows(wild, "img-src", "https://example.test/a.png", origin)


@pytest.mark.parametrize("body, needle", [
    ('<script src="https://cdn.evil.test/x.js"></script>', "script-src"),
    ('<img src="https://images.other.test/a.png">', "img-src"),
    ('<link rel="stylesheet" href="https://fonts.other.test/a.css">', "style-src"),
    ('<form action="https://forms.other.test/post"></form>', "form-action"),
    ('<iframe src="https://video.other.test/e"></iframe>', "frame-src"),
    ("<script>alert(1)</script>", "inline script"),
])
def test_resources_the_csp_would_block_are_problems(tmp_path, body, needle):
    result = sc.static_check(make_site(tmp_path, body), origin="https://example.test")
    found = problems(result)
    assert any(needle in p for p in found), found


def test_inline_style_needs_unsafe_inline(tmp_path):
    strict = HEADERS.replace("style-src 'self' 'unsafe-inline'", "style-src 'self'")
    site = make_site(tmp_path, '<p style="color:red">x</p>', headers=strict)
    assert any("inline style" in p for p in problems(sc.static_check(site, origin="https://example.test")))


def test_fetch_in_a_script_is_checked_against_connect_src(tmp_path):
    site = make_site(tmp_path)
    (site / "assets" / "app.js").write_text("fetch('https://api.other.test/v1');fetch('/ok.json')")
    (site / "ok.json").write_text("{}")
    found = problems(sc.static_check(site, origin="https://example.test"))
    assert any("connect-src" in p and "api.other.test" in p for p in found), found
    assert not any("ok.json" in p for p in found)


def test_broken_and_hidden_local_links(tmp_path):
    site = make_site(tmp_path, '<a href="/missing">x</a><a href="/docs/secret.md">d</a>'
                               '<img src="/assets/nope.png">')
    found = problems(sc.static_check(site, origin="https://example.test"))
    assert any("/missing" in p for p in found), found
    assert any("/assets/nope.png" in p for p in found), found
    assert any("/docs/secret.md" in p and "netlify.toml" in p for p in found), found


def test_pretty_urls_and_folders_resolve(tmp_path):
    site = make_site(tmp_path, '<a href="/about">a</a><a href="/sub/">s</a><a href="#top">t</a>'
                               '<a href="/about?x=1#y">q</a><a href="mailto:a@b.c">m</a>',
                     extra={"sub/index.html": f"<html><head>{CARD}</head></html>"})
    assert problems(sc.static_check(site, origin="https://example.test")) == []


def test_share_card_problems(tmp_path):
    site = make_site(tmp_path)
    (site / "about.html").write_text(
        '<html><head><meta property="og:title" content="T">'
        '<meta property="og:image" content="https://example.test/assets/gone.png"></head></html>')
    found = problems(sc.static_check(site, origin="https://example.test"))
    assert any("about.html" in p and "og:description" in p for p in found), found
    assert any("about.html" in p and "twitter:card" in p for p in found), found
    assert any("gone.png" in p for p in found), found


def test_share_image_on_another_site_or_wrong_size(tmp_path):
    site = make_site(tmp_path)
    page = (site / "about.html").read_text()
    (site / "about.html").write_text(page.replace(
        "https://example.test/assets/og.png", "https://elsewhere.test/og.png"))
    (site / "index.html").write_text((site / "index.html").read_text().replace(
        CARD, CARD + '<meta property="og:image:width" content="1000">'))
    found = problems(sc.static_check(site, origin="https://example.test"))
    assert any("elsewhere.test" in p for p in found), found
    assert any("og:image:width" in p and "1200" in p for p in found), found


def test_pages_without_a_card_and_missing_canonical_are_notes_only(tmp_path):
    site = make_site(tmp_path)
    (site / "about.html").write_text((site / "about.html").read_text().replace(
        '<link rel="canonical" href="https://example.test/">', ""))
    result = sc.static_check(site, origin="https://example.test")
    assert problems(result) == []
    notes = [c for c in result["checks"] if c["status"] == "note"]
    assert any(c.get("page") == "about.html" and "canonical" in c["detail"] for c in notes)
    assert any(c.get("page") == "404.html" for c in notes)  # no share card: fine, just noted


@pytest.mark.parametrize("drop, needle", [
    ("Strict-Transport-Security", "Strict-Transport-Security"),
    ("X-Content-Type-Options", "X-Content-Type-Options"),
    ("Content-Security-Policy", "Content-Security-Policy"),
    ("Referrer-Policy", "Referrer-Policy"),
])
def test_missing_security_headers_are_problems(tmp_path, drop, needle):
    headers = "\n".join(l for l in HEADERS.splitlines() if drop + ":" not in l) + "\n"
    found = problems(sc.static_check(make_site(tmp_path, headers=headers), origin="https://example.test"))
    assert any(needle in p for p in found), found


def test_weak_header_values_are_problems(tmp_path):
    weak = (HEADERS.replace("max-age=31536000", "max-age=600")
            .replace("frame-ancestors 'none'; ", "").replace("X-Frame-Options: DENY", "X-Frame-Options: ALLOWALL")
            .replace("nosniff", "sniff"))
    found = problems(sc.static_check(make_site(tmp_path, headers=weak), origin="https://example.test"))
    assert any("max-age" in p for p in found), found
    assert any("framing" in p for p in found), found
    assert any("nosniff" in p for p in found), found


def test_parse_headers_file_blocks():
    rules = sc.parse_headers_file("# c\n/*\n  A: 1\n  B: two: three\n\n/x/*\n  C: 3\n")
    assert rules == {"/*": {"a": ("A", "1"), "b": ("B", "two: three")}, "/x/*": {"c": ("C", "3")}}


def test_static_cli_writes_report_and_exit_codes(tmp_path, capsys):
    site = make_site(tmp_path)
    out = tmp_path / "static.json"
    assert sc.main(["static", "--root", str(site), "--origin", "https://example.test", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["ok"] is True
    (site / "index.html").write_text((site / "index.html").read_text() + '<img src="/x.png">')
    assert sc.main(["static", "--root", str(site), "--origin", "https://example.test"]) == 1
    assert "FAIL" in capsys.readouterr().out


# ---------------------------------------------------------------- live checks (fake fetcher)

GOOD_HEADERS = {name: value for name, value in sc.parse_headers_file(HEADERS)["/*"].values()}


class FakeWeb:
    """Answers like a well-configured Netlify site at https://example.test; tests break it."""

    def __init__(self):
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        page = ('<html><head><meta property="og:image" content="https://example.test/assets/og.png">'
                "</head></html>").encode()
        for path in ("/", "/about", "/assets/site.css"):
            self.routes["https://example.test" + path] = (200, dict(GOOD_HEADERS), page)
        self.routes["https://example.test/assets/og.png"] = (
            200, {**GOOD_HEADERS, "Content-Type": "image/png"}, png(1200, 630))
        self.routes["http://example.test/"] = (301, {"Location": "https://example.test/"}, b"")
        self.routes["https://www.example.test/"] = (301, {"Location": "https://example.test/"}, b"")
        self.seen: list[str] = []

    def __call__(self, url: str) -> sc.Response:
        self.seen.append(url)
        if url in self.routes:
            status, headers, body = self.routes[url]
        else:
            status, headers, body = 404, dict(GOOD_HEADERS), b"lost"
        return sc.Response(status, {k.lower(): v for k, v in headers.items()}, body)


def live(fake, root, **kw):
    return sc.live_check("https://example.test", root=root, fetch=fake,
                         pages=["/", "/about", "/assets/site.css"], **kw)


def test_live_good_site_passes(tmp_path):
    fake = FakeWeb()
    result = live(fake, make_site(tmp_path))
    assert problems(result) == [], problems(result)
    assert result["ok"] is True and result["base_url"] == "https://example.test"
    names = {c["name"] for c in result["checks"]}
    assert {"https-redirect", "www-redirect", "headers", "hidden-path", "share-image"} <= names
    assert "https://example.test/docs/secret.md" in fake.seen  # a real file, not a made-up path


def test_live_http_not_redirecting_fails(tmp_path):
    fake = FakeWeb()
    root = make_site(tmp_path)
    fake.routes["http://example.test/"] = (200, {}, b"plain")
    assert any("https-redirect" in p for p in problems(live(fake, root)))
    fake.routes["http://example.test/"] = (301, {"Location": "http://example.test/again"}, b"")
    assert any("https-redirect" in p for p in problems(live(fake, root)))
    # a look-alike host that merely starts with the same letters is not the same host
    fake.routes["http://example.test/"] = (301, {"Location": "https://example.test.evil/"}, b"")
    assert any("https-redirect" in p for p in problems(live(fake, root)))


def test_live_www_not_redirecting_fails_and_can_be_skipped(tmp_path):
    fake = FakeWeb()
    fake.routes["https://www.example.test/"] = (200, dict(GOOD_HEADERS), b"copy")
    root = make_site(tmp_path)
    assert any("www-redirect" in p for p in problems(live(fake, root)))
    fake.routes["https://www.example.test/"] = (301, {"Location": "https://elsewhere.test/"}, b"")
    assert any("www-redirect" in p for p in problems(live(fake, root)))
    result = live(fake, root, check_www=False)
    assert problems(result) == []
    assert any(c["name"] == "www-redirect" and c["status"] == "skip" for c in result["checks"])


def test_live_missing_or_changed_header_fails(tmp_path):
    fake = FakeWeb()
    status, headers, body = fake.routes["https://example.test/about"]
    headers = dict(headers)
    del headers["Content-Security-Policy"]
    headers["Referrer-Policy"] = "unsafe-url"
    fake.routes["https://example.test/about"] = (status, headers, body)
    found = problems(live(fake, make_site(tmp_path)))
    assert any("/about" in p and "Content-Security-Policy" in p and "missing" in p for p in found), found
    assert any("/about" in p and "Referrer-Policy" in p and "unsafe-url" in p for p in found), found


def test_live_header_whitespace_and_case_differences_are_fine(tmp_path):
    fake = FakeWeb()
    status, headers, body = fake.routes["https://example.test/"]
    headers = {k.upper(): v.replace("; ", ";   ") for k, v in headers.items()}
    fake.routes["https://example.test/"] = (status, headers, body)
    assert problems(live(fake, make_site(tmp_path))) == []


def test_live_page_not_200_fails(tmp_path):
    fake = FakeWeb()
    del fake.routes["https://example.test/about"]
    assert any("/about" in p and "404" in p for p in problems(live(fake, make_site(tmp_path))))


def test_live_hidden_folder_served_fails(tmp_path):
    fake = FakeWeb()
    fake.routes["https://example.test/docs/secret.md"] = (200, dict(GOOD_HEADERS), b"internal")
    found = problems(live(fake, make_site(tmp_path)))
    assert any("hidden-path" in p and "/docs/" in p for p in found), found


def test_live_share_image_broken_fails(tmp_path):
    fake = FakeWeb()
    fake.routes["https://example.test/assets/og.png"] = (200, {"Content-Type": "text/html"}, b"<html>")
    found = problems(live(fake, make_site(tmp_path)))
    assert any("share-image" in p for p in found), found


def test_live_unreachable_is_a_failure_not_a_crash(tmp_path):
    def down(url):
        raise sc.FetchError("connection refused")
    result = sc.live_check("https://example.test", root=make_site(tmp_path), fetch=down, pages=["/"])
    assert result["ok"] is False
    assert any("connection refused" in p for p in problems(result))


def test_live_http_base_skips_https_and_www_checks(tmp_path):
    fake = FakeWeb()
    for url in list(fake.routes):
        fake.routes[url.replace("https://example.test", "http://127.0.0.1:9")] = fake.routes[url]
    result = sc.live_check("http://127.0.0.1:9", root=make_site(tmp_path), fetch=fake,
                           pages=["/", "/about"], share_image=False)
    skipped = {c["name"] for c in result["checks"] if c["status"] == "skip"}
    assert {"https-redirect", "www-redirect", "share-image"} <= skipped
    assert problems(result) == []


def test_bad_base_url_refused():
    with pytest.raises(ValueError):
        sc.live_check("ftp://example.test", root=REPO, fetch=FakeWeb())
    with pytest.raises(ValueError):
        sc.live_check("https://example.test/sub?x", root=REPO, fetch=FakeWeb())


# ---------------------------------------------------------------- live check over real HTTP

class _Handler(BaseHTTPRequestHandler):
    root: Path
    rules: dict
    blocked = ("/docs/",)

    def log_message(self, *a):  # keep test output quiet
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        target = None
        if not path.startswith(self.blocked):
            target = sc.resolve_local(self.root, path)
        if target is None:
            status, body = 404, (self.root / "404.html").read_bytes()
        else:
            status, body = 200, target.read_bytes()
        self.send_response(status)
        for name, value in self.rules["/*"].values():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_live_check_over_real_http(tmp_path):
    site = make_site(tmp_path)
    handler = type("H", (_Handler,), {"root": site, "rules": sc.parse_headers_file(HEADERS)})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        out = tmp_path / "live.json"
        code = sc.main(["live", "--url", base, "--root", str(site), "--out", str(out),
                        "--page", "/", "--page", "/about", "--no-share-image"])
        report = json.loads(out.read_text())
        assert code == 0, problems(report)
        assert report["ok"] is True
        assert any(c["name"] == "hidden-path" and c["status"] == "pass" for c in report["checks"])
    finally:
        server.shutdown()
        server.server_close()


def test_real_fetcher_does_not_follow_redirects(tmp_path):
    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(301)
            self.send_header("Location", "https://example.test/")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        r = sc.fetch_url(f"http://127.0.0.1:{server.server_address[1]}/")
        assert r.status == 301 and r.headers["location"] == "https://example.test/"
    finally:
        server.shutdown()
        server.server_close()


def test_resolve_local_never_leaves_the_site_folder(tmp_path):
    site = make_site(tmp_path)
    (tmp_path / "outside.html").write_text("secret")
    assert sc.resolve_local(site, "/../outside.html") is None
    assert sc.resolve_local(site, "/%2e%2e/outside") is None
    assert sc.resolve_local(site, "/about") == (site / "about.html").resolve()
    assert sc.resolve_local(site, "about") is None  # must be a path from the site root
