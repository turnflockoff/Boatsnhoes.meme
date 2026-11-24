"""Check the website's security headers, links and share cards (`site-check-v1`).

The site is static files at the repo root, served by Netlify with the headers in `_headers`
and the rules in `netlify.toml`. This tool checks two things:

    python -m app.site_check static [--root ..] [--origin https://boatsnhoes.meme] [--out f.json]
    python -m app.site_check live --url https://boatsnhoes.meme [--out site-check.json]

`static` reads files only (CI runs it on every site change):
  * `_headers` has the security headers for every page (`/*`): HSTS for at least 180 days,
    `nosniff`, a referrer policy, a Content-Security-Policy, and framing blocked
    (`frame-ancestors` or `X-Frame-Options`);
  * every page's scripts, styles, images, frames, form targets, and the URLs its scripts
    `fetch()` are allowed by that CSP (otherwise the browser silently blocks them), and
    inline scripts/styles are only used if the CSP allows inline code;
  * every local link and file a page uses exists (Netlify's pretty URLs: `/contribute` is
    `contribute.html`), and none points into a folder `netlify.toml` hides (`/docs/` ...);
  * pages with a share card have og:title, og:description, og:image and twitter:card, and
    the image is on this site, exists, and matches any width/height the page states.
    Pages without a card, or without a canonical URL, are only noted.

`live` asks the deployed site, without following redirects:
  * each page answers 200 with exactly the headers `_headers` says (names and spacing may
    differ); a value Netlify changed or dropped shows up here;
  * `http://` redirects to `https://` on the same host, and `www.` redirects to the bare
    domain (HTTPS certificates are checked by the request itself);
  * a real file in each folder `netlify.toml` hides answers 404 (not 200);
  * the homepage's share image loads as an image.
It sends only plain GET requests, like a browser; it changes nothing anywhere.

Both write a `bnh-site-check/1` JSON report with `--out` and exit 0 when nothing failed,
1 when something did, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from app.screen import say_lines

RULE = "site-check-v1"
FORMAT = "bnh-site-check/1"
DEFAULT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ORIGIN = "https://boatsnhoes.meme"
DEFAULT_LIVE_PAGES = ["/", "/contribute", "/stay-safe", "/reports/", "/assets/site.css", "/reports/index.json"]
MIN_HSTS_SECONDS = 15_552_000  # 180 days
MAX_BODY = 2_000_000
REDIRECT_CODES = {301, 302, 307, 308}

# Which CSP directive applies when one is missing (CSP Level 3). Directives not listed here
# (form-action, frame-ancestors, base-uri) never fall back: if missing, anything is allowed.
FALLBACKS = {
    "script-src": ["default-src"], "style-src": ["default-src"], "img-src": ["default-src"],
    "font-src": ["default-src"], "connect-src": ["default-src"], "media-src": ["default-src"],
    "manifest-src": ["default-src"], "frame-src": ["child-src", "default-src"],
}
FONT_EXT = (".woff", ".woff2", ".ttf", ".otf", ".eot")


# ------------------------------------------------------------------ small parsers

def parse_headers_file(text: str) -> dict[str, dict[str, tuple[str, str]]]:
    """Netlify `_headers`: a path line, then indented `Name: value` lines.
    Returns {path: {lowercase name: (Name, value)}}; repeated names are joined with ", "."""
    rules: dict[str, dict[str, tuple[str, str]]] = {}
    current: dict[str, tuple[str, str]] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not raw[:1].isspace():
            current = rules.setdefault(line.strip(), {})
            continue
        if current is None or ":" not in line:
            continue
        name, value = line.strip().split(":", 1)
        key = name.strip().lower()
        value = value.strip()
        if key in current:
            value = current[key][1] + ", " + value
        current[key] = (name.strip(), value)
    return rules


def headers_for(rules: dict[str, dict[str, tuple[str, str]]], path: str) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for pattern, block in rules.items():
        if pattern == path or (pattern.endswith("*") and path.startswith(pattern[:-1])):
            out.update(block)
    return out


def parse_csp(value: str) -> dict[str, list[str]]:
    csp: dict[str, list[str]] = {}
    for part in value.split(";"):
        bits = part.split()
        if bits and bits[0].lower() not in csp:  # browsers ignore a repeated directive
            csp[bits[0].lower()] = bits[1:]
    return csp


def _source_matches(source: str, url: str, origin: str) -> bool:
    s = source.strip()
    low = s.lower()
    parsed = urllib.parse.urlsplit(url)
    if low == "'self'":
        return f"{parsed.scheme}://{parsed.netloc}".lower() == origin.lower()
    if low.startswith("'"):
        return False  # 'none', 'unsafe-inline', nonces, hashes: not a URL match
    if low.endswith(":") and "/" not in low:  # scheme source, e.g. data: or https:
        return parsed.scheme.lower() == low[:-1]
    m = re.fullmatch(r"(?:(?P<scheme>[a-z][a-z0-9+.-]*)://)?(?P<host>\*|\*\.[^/:]+|[^/:*]+)"
                     r"(?::(?P<port>\d+|\*))?(?P<path>/.*)?", low)
    if not m or parsed.scheme.lower() not in ("http", "https"):
        return False
    if m["scheme"] and m["scheme"] != parsed.scheme.lower():
        return False
    host = (parsed.hostname or "").lower()
    want = m["host"]
    if want == "*":
        pass
    elif want.startswith("*."):
        if not host.endswith(want[1:]):
            return False
    elif host != want:
        return False
    if m["port"] and m["port"] != "*" and str(parsed.port or "") != m["port"]:
        return False
    if m["path"]:
        path = parsed.path or "/"
        if m["path"].endswith("/"):
            return path.startswith(m["path"])
        return path == m["path"]
    return True


def directive_for(csp: dict[str, list[str]], directive: str) -> tuple[str, list[str]] | None:
    for name in [directive] + FALLBACKS.get(directive, []):
        if name in csp:
            return name, csp[name]
    return None


def csp_allows(csp: dict[str, list[str]], directive: str, url: str, origin: str) -> bool:
    found = directive_for(csp, directive)
    if found is None:
        return True
    return any(_source_matches(src, url, origin) for src in found[1])


def csp_allows_inline(csp: dict[str, list[str]], directive: str) -> bool:
    found = directive_for(csp, directive)
    if found is None:
        return True
    sources = [s.lower() for s in found[1]]
    # With a nonce or hash present, browsers ignore 'unsafe-inline'.
    if any(s.startswith(("'nonce-", "'sha256-", "'sha384-", "'sha512-")) for s in sources):
        return False
    return "'unsafe-inline'" in sources


def hidden_prefixes(root: Path) -> list[str]:
    """Folders `netlify.toml` answers with 404 (e.g. /docs/*)."""
    path = root / "netlify.toml"
    if not path.is_file():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = []
    for rule in data.get("redirects", []):
        src = str(rule.get("from", ""))
        if int(rule.get("status", 301)) == 404 and src.endswith("/*"):
            out.append(src[:-1])
    return out


def resolve_local(root: Path, url_path: str) -> Path | None:
    """The file Netlify would serve for a path (pretty URLs: /x -> x.html, /x/ -> x/index.html)."""
    path = urllib.parse.unquote(url_path.split("?", 1)[0].split("#", 1)[0])
    if not path.startswith("/"):
        return None
    base = root.resolve()
    rel = path.lstrip("/")
    candidates = [base / rel / "index.html"] if path.endswith("/") else [
        base / rel, base / (rel + ".html"), base / rel / "index.html"]
    for c in candidates:
        try:
            c = c.resolve()
        except OSError:
            continue
        if c.is_file() and (c == base or base in c.parents):
            return c
    return None


def png_size(path: Path) -> tuple[int, int] | None:
    head = path.read_bytes()[:24]
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


# ------------------------------------------------------------------ report helpers

class Report:
    def __init__(self, kind: str, **meta):
        self.data = {"format": FORMAT, "rule": RULE, "kind": kind,
                     "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **meta,
                     "ok": True, "checks": []}

    def add(self, status: str, name: str, detail: str, **extra) -> None:
        self.data["checks"].append({"name": name, "status": status, "detail": detail, **extra})
        if status == "fail":
            self.data["ok"] = False

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for c in self.data["checks"]:
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        self.data["counts"] = counts
        return self.data


# ------------------------------------------------------------------ static checks

class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[str, str, str]] = []  # (directive or "link", url, tag)
        self.meta: dict[str, str] = {}
        self.canonical = False
        self.inline_scripts = 0
        self.inline_styles = 0
        self._in_inline_script = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if "style" in a:
            self.inline_styles += 1
        if tag == "script":
            kind = a.get("type", "").lower()
            if a.get("src"):
                self.refs.append(("script-src", a["src"], tag))
            elif kind in ("", "text/javascript", "module", "application/javascript"):
                self._in_inline_script = True
        elif tag == "style":
            self.inline_styles += 1
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            href = a.get("href", "")
            if not href:
                return
            if "stylesheet" in rel:
                self.refs.append(("style-src", href, tag))
            elif {"icon", "apple-touch-icon", "mask-icon"} & set(rel):
                self.refs.append(("img-src", href, tag))
            elif "manifest" in rel:
                self.refs.append(("manifest-src", href, tag))
            elif "canonical" in rel:
                self.canonical = True
        elif tag in ("img", "source", "input") and a.get("src"):
            directive = "img-src" if tag != "source" else "media-src"
            self.refs.append((directive, a["src"], tag))
        elif tag in ("video", "audio") and a.get("src"):
            self.refs.append(("media-src", a["src"], tag))
        elif tag in ("iframe", "frame") and a.get("src"):
            self.refs.append(("frame-src", a["src"], tag))
        elif tag == "form":
            self.refs.append(("form-action", a.get("action", ""), tag))
        elif tag == "a" and a.get("href"):
            self.refs.append(("link", a["href"], tag))
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key.startswith(("og:", "twitter:")) and key not in self.meta:
                self.meta[key] = a.get("content", "")

    def handle_data(self, data):
        if self._in_inline_script and data.strip():
            self.inline_scripts += 1
            self._in_inline_script = False

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_inline_script = False


FETCH_RE = re.compile(r"""\bfetch\(\s*(['"`])([^'"`$]+)\1""")
CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")\s]+)['"]?\s*\)""")
CSS_IMPORT_RE = re.compile(r"""@import\s+(?:url\()?\s*['"]([^'"]+)['"]""")


def _page_url(page: str) -> str:
    if page == "index.html":
        return "/"
    if page.endswith("/index.html"):
        return "/" + page[: -len("index.html")]
    return "/" + page[: -len(".html")] if page.endswith(".html") else "/" + page


def _list_pages(root: Path, hidden: list[str]) -> list[str]:
    pages = []
    for path in sorted(root.rglob("*.html")):
        rel = path.relative_to(root).as_posix()
        parts = rel.split("/")
        if any(p.startswith(".") or p == "node_modules" for p in parts):
            continue
        if any(("/" + rel).startswith(h) for h in hidden):
            continue
        pages.append(rel)
    return pages


def _check_header_file(report: Report, rules: dict, csp_value: str | None) -> dict[str, list[str]]:
    block = rules.get("/*")
    if block is None:
        report.add("fail", "headers", "_headers has no `/*` block, so pages get no security headers")
        return {}
    hsts = block.get("strict-transport-security", ("", ""))[1]
    m = re.search(r"max-age\s*=\s*(\d+)", hsts, re.I)
    if not hsts:
        report.add("fail", "headers", "Strict-Transport-Security is missing")
    elif not m or int(m.group(1)) < MIN_HSTS_SECONDS:
        report.add("fail", "headers", f"Strict-Transport-Security max-age is below {MIN_HSTS_SECONDS} "
                   f"(180 days): {hsts!r}")
    else:
        report.add("pass", "headers", f"Strict-Transport-Security: {hsts}")
    xcto = block.get("x-content-type-options", ("", ""))[1]
    if xcto.lower() != "nosniff":
        report.add("fail", "headers", f"X-Content-Type-Options should be nosniff, is {xcto or 'missing'!r}")
    else:
        report.add("pass", "headers", "X-Content-Type-Options: nosniff")
    ref = block.get("referrer-policy", ("", ""))[1]
    if not ref or ref.lower() in ("unsafe-url", "no-referrer-when-downgrade"):
        report.add("fail", "headers", f"Referrer-Policy should be set and not leak full URLs, is {ref or 'missing'!r}")
    else:
        report.add("pass", "headers", f"Referrer-Policy: {ref}")
    if not csp_value:
        report.add("fail", "headers", "Content-Security-Policy is missing")
        csp: dict[str, list[str]] = {}
    else:
        csp = parse_csp(csp_value)
        report.add("pass", "headers", "Content-Security-Policy is set")
    xfo = block.get("x-frame-options", ("", ""))[1].upper()
    if "frame-ancestors" in csp or xfo in ("DENY", "SAMEORIGIN"):
        report.add("pass", "headers", "framing by other sites is blocked")
    else:
        report.add("fail", "headers", "framing by other sites is not blocked (no CSP frame-ancestors, "
                   f"X-Frame-Options is {xfo or 'missing'!r})")
    if "permissions-policy" not in block:
        report.add("note", "headers", "no Permissions-Policy (optional)")
    return csp


def _check_ref(report: Report, root: Path, origin: str, csp: dict, hidden: list[str],
               page: str, base_url: str, directive: str, ref: str, where: str) -> None:
    ref = ref.strip()
    if directive != "form-action" and (not ref or ref.startswith("#")):
        return
    if ref.lower().startswith(("mailto:", "tel:", "javascript:", "sms:")):
        return
    url = urllib.parse.urljoin(base_url, ref) if ref else base_url
    parsed = urllib.parse.urlsplit(url)
    local = f"{parsed.scheme}://{parsed.netloc}".lower() == origin.lower()
    if directive != "link" and csp and not csp_allows(csp, directive, url, origin):
        found = directive_for(csp, directive)
        via = f" (via {found[0]})" if found and found[0] != directive else ""
        report.add("fail", "csp", f"{page}: {where} {ref} is blocked by {directive}{via}", page=page)
        return
    if not local or parsed.scheme not in ("http", "https"):
        return
    path = parsed.path or "/"
    for prefix in hidden:
        if path.startswith(prefix):
            report.add("fail", "link", f"{page}: {where} {path} is hidden by netlify.toml (404 on the site)",
                       page=page)
            return
    if resolve_local(root, path) is None:
        report.add("fail", "link", f"{page}: {where} {path} does not exist", page=page)


def _check_card(report: Report, root: Path, origin: str, page: str, parser: _PageParser) -> None:
    meta = parser.meta
    if not meta:
        report.add("note", "share-card", f"{page}: no share card (fine for pages nobody shares)", page=page)
        return
    for key in ("og:title", "og:description", "og:image", "twitter:card"):
        if not meta.get(key, "").strip():
            report.add("fail", "share-card", f"{page}: missing {key}", page=page)
    if not (parser.canonical or meta.get("og:url")):
        report.add("note", "share-card", f"{page}: no canonical link or og:url "
                   "(shares may show the address the visitor used)", page=page)
    image = meta.get("og:image", "").strip()
    if not image:
        return
    parsed = urllib.parse.urlsplit(image)
    if parsed.scheme != "https":
        report.add("fail", "share-card", f"{page}: og:image must be a full https:// URL, is {image!r}", page=page)
        return
    if f"{parsed.scheme}://{parsed.netloc}".lower() != origin.lower():
        report.add("fail", "share-card", f"{page}: og:image {image} is on another site", page=page)
        return
    file = resolve_local(root, parsed.path)
    if file is None:
        report.add("fail", "share-card", f"{page}: og:image {parsed.path} does not exist", page=page)
        return
    size = png_size(file)
    for key, idx, word in (("og:image:width", 0, "wide"), ("og:image:height", 1, "tall")):
        stated = meta.get(key, "").strip()
        if stated and size and stated != str(size[idx]):
            report.add("fail", "share-card", f"{page}: {key} says {stated} but the image is "
                       f"{size[idx]} {word}", page=page)
    report.add("pass", "share-card", f"{page}: share card complete ({parsed.path})", page=page)


def static_check(root: Path = DEFAULT_ROOT, origin: str = DEFAULT_ORIGIN) -> dict:
    root = Path(root)
    origin = origin.rstrip("/")
    report = Report("static", root=str(root), origin=origin)
    headers_path = root / "_headers"
    rules = parse_headers_file(headers_path.read_text(encoding="utf-8")) if headers_path.is_file() else {}
    csp_value = rules.get("/*", {}).get("content-security-policy", ("", ""))[1]
    csp = _check_header_file(report, rules, csp_value)
    hidden = hidden_prefixes(root)
    scanned_assets: set[Path] = set()
    for page in _list_pages(root, hidden):
        base_url = origin + _page_url(page)
        parser = _PageParser()
        parser.feed((root / page).read_text(encoding="utf-8", errors="replace"))
        before = sum(1 for c in report.data["checks"] if c["status"] == "fail")
        if parser.inline_scripts and csp and not csp_allows_inline(csp, "script-src"):
            report.add("fail", "csp", f"{page}: inline script is blocked by script-src "
                       "(no 'unsafe-inline')", page=page)
        if parser.inline_styles and csp and not csp_allows_inline(csp, "style-src"):
            report.add("fail", "csp", f"{page}: inline style is blocked by style-src "
                       "(no 'unsafe-inline')", page=page)
        for directive, ref, tag in parser.refs:
            _check_ref(report, root, origin, csp, hidden, page, base_url, directive, ref, f"<{tag}>")
            if directive in ("script-src", "style-src"):
                url = urllib.parse.urljoin(base_url, ref)
                if url.lower().startswith(origin.lower() + "/"):
                    asset = resolve_local(root, urllib.parse.urlsplit(url).path)
                    if asset and asset not in scanned_assets:
                        scanned_assets.add(asset)
                        _check_asset(report, root, origin, csp, hidden, page, url, asset, directive)
        _check_card(report, root, origin, page, parser)
        after = sum(1 for c in report.data["checks"] if c["status"] == "fail")
        if after == before:
            report.add("pass", "page", f"{page}: every resource and link checks out", page=page)
    return report.summary()


def _check_asset(report, root, origin, csp, hidden, page, url, asset: Path, directive: str) -> None:
    text = asset.read_text(encoding="utf-8", errors="replace")
    name = asset.relative_to(root.resolve()).as_posix()
    if directive == "script-src":
        for _, ref in FETCH_RE.findall(text):
            _check_ref(report, root, origin, csp, hidden, page, url, "connect-src", ref, f"fetch() in {name}")
    else:
        for ref in CSS_IMPORT_RE.findall(text):
            _check_ref(report, root, origin, csp, hidden, page, url, "style-src", ref, f"@import in {name}")
        for ref in CSS_URL_RE.findall(text):
            kind = "font-src" if ref.lower().split("?")[0].endswith(FONT_EXT) else "img-src"
            _check_ref(report, root, origin, csp, hidden, page, url, kind, ref, f"url() in {name}")


# ------------------------------------------------------------------ live checks

@dataclass
class Response:
    status: int
    headers: dict[str, str]  # lowercase names
    body: bytes


class FetchError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_url(url: str, timeout: float = 15.0) -> Response:
    """One GET, redirects NOT followed, HTTPS certificates verified, body capped at 2 MB."""
    opener = urllib.request.build_opener(_NoRedirect())
    req = urllib.request.Request(url, headers={"User-Agent": f"{FORMAT} (+{RULE})"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()},
                            resp.read(MAX_BODY))
    except urllib.error.HTTPError as e:  # 3xx (not followed), 4xx, 5xx
        body = e.read(MAX_BODY) if e.fp else b""
        return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise FetchError(str(getattr(e, "reason", e))) from None


def _norm(value: str) -> str:
    return " ".join(value.split())


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host == "localhost"


def _hidden_samples(root: Path, hidden: list[str]) -> list[str]:
    out = []
    for prefix in hidden:
        folder = root / prefix.strip("/")
        files = sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []
        out.append("/" + files[0].relative_to(root).as_posix() if files else prefix)
    return out


def _og_image(body: bytes) -> str | None:
    parser = _PageParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    return parser.meta.get("og:image") or None


def live_check(base_url: str, root: Path = DEFAULT_ROOT, fetch: Callable[[str], Response] = fetch_url,
               pages: list[str] | None = None, check_www: bool = True, share_image: bool = True) -> dict:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("--url must start with https:// (or http:// for a local test server)")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("--url must be the site's address only, e.g. https://boatsnhoes.meme")
    base = f"{parsed.scheme}://{parsed.netloc}"
    host = parsed.hostname
    root = Path(root)
    rules = parse_headers_file((root / "_headers").read_text(encoding="utf-8"))
    report = Report("live", base_url=base)

    def get(url: str) -> Response | None:
        try:
            return fetch(url)
        except FetchError as e:
            report.add("fail", "fetch", f"{url}: could not connect ({e})", url=url)
            return None

    # 1. Pages answer 200 with the headers _headers promises.
    home: Response | None = None
    for path in pages or DEFAULT_LIVE_PAGES:
        url = base + path
        r = get(url)
        if r is None:
            continue
        if path == "/":
            home = r
        if r.status != 200:
            report.add("fail", "page", f"{path}: answered {r.status}, expected 200", url=url)
            continue
        wanted = headers_for(rules, path)
        bad = []
        for key, (name, value) in wanted.items():
            got = r.headers.get(key)
            if got is None:
                bad.append(f"{name} missing")
            elif _norm(got) != _norm(value):
                bad.append(f"{name} is {got!r}, _headers says {value!r}")
        if bad:
            report.add("fail", "headers", f"{path}: " + "; ".join(bad), url=url)
        else:
            report.add("pass", "headers", f"{path}: 200 with all {len(wanted)} headers from _headers", url=url)

    # 2. http:// -> https://, same host.
    if parsed.scheme != "https":
        report.add("skip", "https-redirect", "base URL is http://, so there is no HTTPS to check")
    else:
        url = f"http://{parsed.netloc}/"
        r = get(url)
        if r is not None:
            loc = r.headers.get("location", "")
            if r.status in REDIRECT_CODES and loc.startswith(f"https://{parsed.netloc}/"):
                report.add("pass", "https-redirect", f"{url} -> {r.status} {loc}", url=url)
            else:
                report.add("fail", "https-redirect", f"{url} answered {r.status} {loc or '(no Location)'}, "
                           f"expected a redirect to https://{parsed.netloc}/", url=url)

    # 3. www. -> bare domain.
    if not check_www or parsed.scheme != "https" or _is_ip(host) or host.startswith("www."):
        report.add("skip", "www-redirect", "not checked (turned off, local address, or already www)")
    else:
        url = f"https://www.{parsed.netloc}/"
        r = get(url)
        if r is not None:
            loc = r.headers.get("location", "")
            if r.status in REDIRECT_CODES and loc.startswith(base + "/"):
                report.add("pass", "www-redirect", f"{url} -> {r.status} {loc}", url=url)
            else:
                report.add("fail", "www-redirect", f"{url} answered {r.status} {loc or '(no Location)'}, "
                           f"expected a redirect to {base}/", url=url)

    # 4. Folders netlify.toml hides really answer 404 (a real file, not a made-up path).
    for path in _hidden_samples(root, hidden_prefixes(root)):
        url = base + path
        r = get(url)
        if r is None:
            continue
        if r.status == 404:
            report.add("pass", "hidden-path", f"{path}: 404 as netlify.toml says", url=url)
        else:
            report.add("fail", "hidden-path", f"{path}: answered {r.status}; netlify.toml should hide "
                       f"/{path.strip('/').split('/')[0]}/ with a 404", url=url)

    # 5. The homepage's share image loads as an image.
    if not share_image:
        report.add("skip", "share-image", "turned off")
    elif home is None or home.status != 200:
        report.add("fail", "share-image", "homepage didn't load, so its share image wasn't checked")
    else:
        image = _og_image(home.body)
        if not image:
            report.add("fail", "share-image", "homepage has no og:image")
        else:
            r = get(image)
            if r is not None:
                ctype = r.headers.get("content-type", "")
                if r.status == 200 and ctype.lower().startswith("image/"):
                    report.add("pass", "share-image", f"{image}: 200 {ctype}, {len(r.body)} bytes", url=image)
                else:
                    report.add("fail", "share-image", f"{image}: answered {r.status} {ctype or '(no type)'}, "
                               "expected an image", url=image)
    return report.summary()


# ------------------------------------------------------------------ CLI

def _print(result: dict) -> None:
    # Details quote headers, redirects and page text the live site (or whatever answers at
    # --url) sent: printed safely (`operator-screen-v1`, app/screen.py).
    lines = [f"{c['status'].upper():5} {c['name']:15} {c['detail']}" for c in result["checks"]]
    counts = result.get("counts", {})
    lines += ["", f"{'OK' if result['ok'] else 'PROBLEMS FOUND'}: "
              + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))]
    sys.stdout.write(say_lines(lines))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.site_check", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("static", help="check the repo's pages and _headers (reads files only)")
    st.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    st.add_argument("--origin", default=DEFAULT_ORIGIN)
    st.add_argument("--out", type=Path)
    lv = sub.add_parser("live", help="check the deployed site (plain GET requests)")
    lv.add_argument("--url", default=DEFAULT_ORIGIN)
    lv.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    lv.add_argument("--page", action="append", help="path to check (repeatable); default: main pages")
    lv.add_argument("--no-www", action="store_true", help="skip the www. -> bare domain check")
    lv.add_argument("--no-share-image", action="store_true", help="skip loading the share image")
    lv.add_argument("--out", type=Path)
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    try:
        if args.cmd == "static":
            result = static_check(args.root, args.origin)
        else:
            result = live_check(args.url, root=args.root, pages=args.page, check_www=not args.no_www,
                                share_image=not args.no_share_image)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    _print(result)
    if args.out:
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
