"""Check the website's pages for common accessibility problems (`a11y-check-v1`).

    python -m app.a11y_check [--root ..] [--owner-page index.html] [--out a11y.json]

Reads files only (CI runs it on every site change). For every page `site_check` would check
(folders hidden by `netlify.toml`, like /docs/, are skipped), it reads the HTML and the CSS
the page loads (local stylesheets and `<style>` blocks) and checks:

  * the page says its language (`<html lang>`), has a title, and doesn't block zooming
    (`user-scalable=no` or `maximum-scale` under 2 in the viewport tag);
  * every image has `alt` (empty is fine for decoration); every inline `<svg>` is either
    hidden from screen readers (`aria-hidden="true"`) or is `role="img"` with a name;
  * every link and button has a name a screen reader can read out (its text, an image's
    alt, `aria-label` or `aria-labelledby`); text inside `aria-hidden` doesn't count;
  * every form field has a label (`<label>` around it or `for=` its id, `aria-label`,
    `aria-labelledby` or `title`); a placeholder is not a label;
  * exactly one `<h1>`, no skipped heading levels on the way down (h2 then h4), exactly one
    `<main>`; a page with a `<nav>` starts with a skip link to an id that exists;
  * ids are unique, and `for`, `aria-controls`, `aria-labelledby`, `aria-describedby` point
    at ids that exist; no `tabindex` above 0; nothing focusable inside `aria-hidden="true"`;
  * CSS: text colour against its own background (each gradient stop), or the page
    background when it sets none, is at least 4.5:1 (WCAG AA for normal text; we use it
    for large text too); a background with no text colour is checked against the body text
    colour; the focus outline is at least 3:1 against the page; `outline: none` is only
    allowed in a `:focus` rule that draws a replacement (box-shadow, border, background);
    any transition, animation or smooth scrolling comes with a `prefers-reduced-motion`
    rule. Colours it can't read (hsl(), images, `currentColor`) are noted, not passed.

Pages listed with `--owner-page` (default: `index.html`, the owner-approved homepage) are
checked the same way, but their findings are `note`s, not failures: they're for the owner
to decide on, and CI stays green.

What it can't see: content scripts add later (the browser check's report, the logbook's
period cards), colours that depend on which element sits on which background (text in a
card is compared with the page background), real screen-reader behaviour, and anything
about wording. A pass means "none of these known problems", not "fully accessible".

Writes a `bnh-a11y-check/1` JSON report with `--out`. Exit 0 when nothing failed, 1 when
something did, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from app.site_check import _list_pages, hidden_prefixes, resolve_local

RULE = "a11y-check-v1"
FORMAT = "bnh-a11y-check/1"
DEFAULT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OWNER_PAGES = ("index.html",)
TEXT_RATIO = 4.5
NON_TEXT_RATIO = 3.0
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr"}
NAMED_COLORS = {"white": (255, 255, 255), "black": (0, 0, 0)}


# ------------------------------------------------------------------ a tiny DOM

@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    parent: "Node | None" = None
    children: list = field(default_factory=list)  # Node or str

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name, default)

    def walk(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.walk()

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {})
        self.cur = self.root
        self.styles: list[str] = []
        self.stylesheets: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        node = Node(tag, a, self.cur)
        self.cur.children.append(node)
        if tag == "link" and "stylesheet" in a.get("rel", "").lower().split() and a.get("href"):
            self.stylesheets.append(a["href"])
        if tag == "style":
            self._in_style = True
            self.styles.append("")
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.cur.tag == tag:
            self.cur = self.cur.parent or self.root

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        if self._in_style:
            self.styles[-1] += data
        else:
            self.cur.children.append(data)


def parse_html(text: str) -> _TreeBuilder:
    b = _TreeBuilder()
    b.feed(text)
    b.close()
    return b


def _hidden(node: Node) -> bool:
    return any(n.get("aria-hidden", "").lower() == "true" for n in [node, *node.ancestors()])


def _text(node: Node, ids: dict[str, Node]) -> str:
    """Text a screen reader would read for this subtree (simplified accessible-name rules)."""
    if node.get("aria-hidden", "").lower() == "true":
        return ""
    for attr in ("aria-labelledby",):
        if node.get(attr):
            return " ".join(_text(ids[i], ids) for i in node.get(attr).split() if i in ids).strip()
    if node.get("aria-label", "").strip():
        return node.get("aria-label").strip()
    if node.tag == "img":
        return (node.get("alt") or "").strip()
    if node.tag == "svg":
        for c in node.children:
            if isinstance(c, Node) and c.tag == "title":
                return _plain(c)
        return ""
    if node.tag in ("script", "style", "template"):
        return ""
    parts = []
    for c in node.children:
        parts.append(c if isinstance(c, str) else _text(c, ids))
    got = " ".join(" ".join(parts).split())
    return got or (node.get("title") or "").strip()


def _plain(node: Node) -> str:
    return " ".join("".join(c if isinstance(c, str) else _plain(c) for c in node.children).split())


def _focusable(n: Node) -> bool:
    if n.tag == "a" and n.get("href") is not None:
        return True
    if n.tag in ("button", "select", "textarea") or (n.tag == "input" and n.get("type", "").lower() != "hidden"):
        return True
    ti = n.get("tabindex")
    return ti is not None and ti.strip().lstrip("+").isdigit()


# ------------------------------------------------------------------ colours

def parse_color(value: str) -> tuple[int, int, int, float] | None:
    v = value.strip().lower()
    if v in NAMED_COLORS:
        return (*NAMED_COLORS[v], 1.0)
    m = re.fullmatch(r"#([0-9a-f]{3,8})", v)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h)
        if len(h) not in (6, 8):
            return None
        alpha = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha
    m = re.fullmatch(r"rgba?\(\s*([^)]*)\)", v)
    if m:
        parts = [p for p in re.split(r"[\s,/]+", m.group(1).strip()) if p]
        if len(parts) not in (3, 4):
            return None
        try:
            rgb = []
            for p in parts[:3]:
                rgb.append(round(float(p[:-1]) * 2.55) if p.endswith("%") else round(float(p)))
            a = 1.0
            if len(parts) == 4:
                a = float(parts[3][:-1]) / 100 if parts[3].endswith("%") else float(parts[3])
        except ValueError:
            return None
        if not all(0 <= c <= 255 for c in rgb) or not 0 <= a <= 1:
            return None
        return rgb[0], rgb[1], rgb[2], a
    return None


def _lum(rgb) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(a, b) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return round((la + 0.05) / (lb + 0.05), 2)


def _blend(fg, bg) -> tuple[int, int, int]:
    a = fg[3] if len(fg) > 3 else 1.0
    return tuple(round(fg[i] * a + bg[i] * (1 - a)) for i in range(3))


# ------------------------------------------------------------------ CSS

@dataclass
class Rule:
    selector: str
    decls: dict[str, str]
    media: str


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def parse_css(css: str, media: str = "") -> list[Rule]:
    css = _strip_comments(css)
    rules: list[Rule] = []
    i, n = 0, len(css)
    while i < n:
        j = css.find("{", i)
        if j < 0:
            break
        head = css[i:j].strip()
        depth, k = 1, j + 1
        while k < n and depth:
            if css[k] == "{":
                depth += 1
            elif css[k] == "}":
                depth -= 1
            k += 1
        body = css[j + 1:k - 1]
        if head.startswith("@"):
            if head.lower().startswith(("@media", "@supports", "@layer")):
                rules.extend(parse_css(body, (media + " " + head).strip()))
        else:
            decls = {}
            for d in body.split(";"):
                if ":" in d:
                    prop, val = d.split(":", 1)
                    decls[prop.strip().lower()] = re.sub(r"\s*!important\s*$", "", val.strip())
            rules.append(Rule(" ".join(head.split()), decls, media))
        i = k
    return rules


def _custom_props(rules: list[Rule]) -> dict[str, str]:
    props: dict[str, str] = {}
    for r in rules:
        if r.media:
            continue
        if any(s.strip() in (":root", "html") for s in r.selector.split(",")):
            for k, v in r.decls.items():
                if k.startswith("--"):
                    props[k] = v
    return props


def _resolve(value: str, props: dict[str, str], depth: int = 0) -> str:
    if depth > 10:
        return value
    def sub(m):
        name, fallback = m.group(1), m.group(2)
        if name in props:
            return _resolve(props[name], props, depth + 1)
        return _resolve(fallback.strip(), props, depth + 1) if fallback is not None else "unset"
    return re.sub(r"var\(\s*(--[\w-]+)\s*(?:,\s*((?:[^()]|\([^()]*\))*))?\)", sub, value)


COLOR_TOKEN = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)")


def _background_colors(value: str):
    """('none',) | ('colors', [rgba...]) | ('unknown', reason)."""
    v = value.strip().lower()
    if v in ("", "none", "transparent", "inherit", "initial", "unset", "0"):
        return ("none", [])
    if "url(" in v:
        return ("unknown", "an image background")
    tokens = COLOR_TOKEN.findall(value)
    words = [w for w in re.split(r"[\s,()]+", v) if w in NAMED_COLORS]
    colors = []
    for t in tokens + words:
        c = parse_color(t)
        if c is None:
            return ("unknown", f"colour {t!r}")
        colors.append(c)
    if not colors:
        if "gradient" in v or "transparent" in v:
            return ("none", [])
        return ("unknown", f"background {value.strip()!r}")
    return ("colors", colors)


def _is_decoration(rule: Rule) -> bool:
    sel = rule.selector
    parts = [s.strip() for s in sel.split(",")]
    return all("::before" in p or "::after" in p or "::marker" in p or "::selection" in p
               or "::placeholder" in p for p in parts) or "content" in rule.decls


# ------------------------------------------------------------------ selectors (a small subset)

_COMPOUND = re.compile(r"""^(?P<tag>[a-zA-Z][\w-]*|\*)?(?P<rest>(?:[.#][\w-]+|\[[^\]]*\]|::?[\w-]+(?:\([^)]*\))?)*)$""")
_PART = re.compile(r"""[.#][\w-]+|\[[^\]]*\]|::?[\w-]+(?:\([^)]*\))?""")
_STATE_PSEUDO = {"hover", "focus", "focus-visible", "focus-within", "active", "visited", "link",
                 "first-child", "last-child", "checked", "disabled"}


def _parse_selector(sel: str):
    """List of (combinator, compound) from left to right, or None if unsupported."""
    sel = re.sub(r"\s*>\s*", " > ", sel.strip())
    tokens = sel.split()
    out, comb = [], " "
    for t in tokens:
        if t == ">":
            comb = ">"
            continue
        if t in ("+", "~") or "+" in t or "~" in t:
            return None
        m = _COMPOUND.match(t)
        if not m:
            return None
        parts = _PART.findall(m.group("rest") or "")
        spec = [0, 0, 0]
        need = {"tag": (m.group("tag") or "*").lower(), "classes": [], "id": None, "attrs": [], "pseudo_el": False}
        if need["tag"] != "*":
            spec[2] += 1
        for part in parts:
            if part.startswith("::"):
                need["pseudo_el"] = True
                spec[2] += 1
            elif part.startswith(":"):
                name = part[1:].split("(")[0].lower()
                if name not in _STATE_PSEUDO and name != "root":
                    return None
                if name == "root":
                    need["tag"] = "html"
                spec[1] += 1
            elif part.startswith("."):
                need["classes"].append(part[1:])
                spec[1] += 1
            elif part.startswith("#"):
                need["id"] = part[1:]
                spec[0] += 1
            else:
                inner = part[1:-1]
                if "=" in inner:
                    k, v = inner.split("=", 1)
                    need["attrs"].append((k.strip("~|^$* ").lower(), v.strip().strip("'\"")))
                else:
                    need["attrs"].append((inner.strip().lower(), None))
                spec[1] += 1
        out.append((comb, need, tuple(spec)))
        comb = " "
    return out or None


def _matches_compound(n: "Node", need) -> bool:
    if need["tag"] != "*" and n.tag != need["tag"]:
        return False
    if need["id"] and n.get("id") != need["id"]:
        return False
    classes = (n.get("class") or "").split()
    if any(c not in classes for c in need["classes"]):
        return False
    for k, v in need["attrs"]:
        if k not in n.attrs or (v is not None and n.attrs[k] != v):
            return False
    return True


def _matches(n: "Node", parsed) -> bool:
    *rest, (_, last, _) = parsed
    if not _matches_compound(n, last):
        return False
    node = n
    comb = parsed[-1][0]
    for c, need, _ in reversed(rest):
        if comb == ">":
            node = node.parent
            if node is None or not _matches_compound(node, need):
                return False
        else:
            node = node.parent
            while node is not None and not _matches_compound(node, need):
                node = node.parent
            if node is None:
                return False
        comb = c
    return True


def _specificity(parsed) -> tuple[int, int, int]:
    return tuple(sum(p[2][i] for p in parsed) for i in range(3))


class _Cascade:
    """Which (non-@media, non-pseudo-element) rule gives an element its colour or background."""

    def __init__(self, rules: list["Rule"], props: dict[str, str]):
        self.entries = []
        for order, r in enumerate(rules):
            if r.media:
                continue
            for sel in r.selector.split(","):
                parsed = _parse_selector(sel)
                if parsed and not any(p[1]["pseudo_el"] for p in parsed):
                    self.entries.append((parsed, _specificity(parsed), order, r))
        self.props = props

    def _winner(self, n: "Node", props: tuple[str, ...]):
        inline = _inline(n)
        if any(p in inline for p in props):
            return Rule(f'<{n.tag} style="...">', inline, "")
        best = None
        for parsed, spec, order, r in self.entries:
            if any(p in r.decls for p in props) and _matches(n, parsed):
                key = (spec, order)
                if best is None or key > best[0]:
                    best = (key, r)
        return best[1] if best else None

    def background(self, n: "Node"):
        """('colors', [...]) from the nearest element (itself or an ancestor) with a background."""
        node = n
        while node is not None and node.tag != "#document":
            r = self._winner(node, ("background", "background-color"))
            if r is not None:
                raw = r.decls.get("background-color", r.decls.get("background"))
                kind, cols = _background_colors(_resolve(raw, self.props))
                if kind != "none":
                    return kind, cols
            node = node.parent
        return "none", []

    def color(self, n: "Node"):
        node = n
        while node is not None and node.tag != "#document":
            r = self._winner(node, ("color",))
            if r is not None:
                raw = _resolve(r.decls["color"], self.props).strip()
                if raw.lower() not in ("inherit", "unset", "initial"):
                    return parse_color(raw)
            node = node.parent
        return None


def _inline(n: "Node") -> dict[str, str]:
    decls = {}
    for d in (n.get("style") or "").split(";"):
        if ":" in d:
            k, v = d.split(":", 1)
            decls[k.strip().lower()] = re.sub(r"\s*!important\s*$", "", v.strip())
    return decls


def _own_text(n: "Node") -> bool:
    return any(isinstance(c, str) and c.strip() for c in n.children) or any(
        isinstance(c, Node) and c.tag == "img" and (c.get("alt") or "").strip() for c in n.children)


def _matched_elements(doc, rule: "Rule") -> list["Node"] | None:
    """Elements on the page the rule applies to; None when the selector is beyond this checker."""
    if doc is None:
        return None
    found = []
    for sel in rule.selector.split(","):
        parsed = _parse_selector(sel)
        if parsed is None:
            return None
        found.extend(n for n in doc.root.walk() if n.tag != "#document" and _matches(n, parsed))
    return found


@dataclass
class Finding:
    status: str
    name: str
    detail: str


def check_css(rules: list[Rule], where: str, doc: "_TreeBuilder | None" = None) -> list[Finding]:
    out: list[Finding] = []
    props = _custom_props(rules)
    page_bg = (255, 255, 255, 1.0)
    body_color = (0, 0, 0, 1.0)
    for r in rules:
        if r.media:
            continue
        sels = [s.strip() for s in r.selector.split(",")]
        if any(s in ("body", "html") for s in sels):
            for prop in ("background-color", "background"):
                if prop in r.decls:
                    kind, cols = _background_colors(_resolve(r.decls[prop], props))
                    if kind == "colors":
                        page_bg = cols[0]
            if "body" in sels and "color" in r.decls:
                c = parse_color(_resolve(r.decls["color"], props))
                if c:
                    body_color = c
    page_bg = _blend(page_bg, (255, 255, 255)) + (1.0,)
    cascade = _Cascade(rules, props)

    inline_rules = []
    if doc is not None:
        for el in doc.root.walk():
            decls = _inline(el) if el.tag != "#document" else {}
            if any(k in decls for k in ("color", "background", "background-color")):
                inline_rules.append((Rule(f'<{el.tag} style="{el.get("style")}">', decls, ""), el))
    for r in rules + [ir for ir, _ in inline_rules]:
        inline_el = next((el for ir, el in inline_rules if ir is r), None)
        label = f"{where}: `{r.selector}`" + (f" ({r.media})" if r.media else "")
        if inline_el is not None:
            label = f"inline {r.selector}"
        elif _is_decoration(r):
            continue
        bg_raw = r.decls.get("background-color", r.decls.get("background"))
        if bg_raw is not None:
            kind, bgs = _background_colors(_resolve(bg_raw, props))
        else:
            kind, bgs = "none", []
        on_page = [inline_el] if inline_el is not None else _matched_elements(doc, r)
        if on_page and inline_el is None:
            keys = [k for k in ("color", "background", "background-color") if k in r.decls]
            kept = [el for el in on_page if not all(k in _inline(el) or (k.startswith("background") and (
                "background" in _inline(el) or "background-color" in _inline(el))) for k in keys)]
            if not kept:
                continue  # every element it styles here overrides it inline (checked below)
            on_page = kept
        pairs = []  # (text colour, [background colours])
        if "color" in r.decls:
            raw = _resolve(r.decls["color"], props)
            if raw.strip().lower() in ("inherit", "unset", "initial"):
                continue
            fg = parse_color(raw)
            if fg is None:
                out.append(Finding("note", "contrast", f"{label}: can't read colour {raw!r}, not checked"))
                continue
            if kind == "unknown":
                out.append(Finding("note", "contrast", f"{label}: {bgs}, text contrast not checked"))
                continue
            if kind == "colors":
                pairs.append((fg, bgs))
            elif on_page:
                for el in on_page:
                    k2, b2 = cascade.background(el)
                    if k2 == "unknown":
                        out.append(Finding("note", "contrast", f"{label}: sits on {b2}, not checked"))
                    else:
                        pairs.append((fg, b2 if k2 == "colors" else [page_bg]))
            else:
                pairs.append((fg, [page_bg]))
        elif kind == "colors":
            if on_page is not None and on_page:
                texts = [el for el in on_page if _own_text(el) or any(
                    _own_text(d) for d in el.walk() if cascade.color(d) == cascade.color(el))]
                if not texts:
                    continue  # empty decoration (a dot, a bar): no text to read
                for el in texts:
                    pairs.append((cascade.color(el) or body_color, bgs))
            else:
                pairs.append((body_color, bgs))
        elif kind == "unknown":
            out.append(Finding("note", "contrast", f"{label}: {bgs}, text contrast not checked"))
            continue
        else:
            continue
        worst = None
        for fg_c, back_list in pairs:
            for b in back_list:
                b = _blend(b, page_bg)
                ratio = contrast_ratio(_blend(fg_c, b), b)
                if worst is None or ratio < worst[0]:
                    worst = (ratio, b, fg_c)
        if worst and worst[0] < TEXT_RATIO:
            out.append(Finding("fail", "contrast",
                               f"{label}: text {_hex(_blend(worst[2], worst[1]))} on {_hex(worst[1])} is "
                               f"{worst[0]}:1, needs {TEXT_RATIO}:1"))

    # focus rings
    for r in rules:
        sel = r.selector
        label = f"{where}: `{sel}`"
        outline = _resolve(r.decls.get("outline", ""), props).strip().lower()
        style = _resolve(r.decls.get("outline-style", ""), props).strip().lower()
        removes = outline in ("none", "0", "0px", "0 none") or style == "none" \
            or _resolve(r.decls.get("outline-width", ""), props).strip() in ("0", "0px")
        if removes:
            replaced = any(k in r.decls for k in ("box-shadow", "border", "border-color", "border-bottom",
                                                  "background", "background-color", "text-decoration"))
            if ":focus" not in sel:
                out.append(Finding("fail", "focus", f"{label}: removes the focus outline outside a :focus "
                                   "rule, so keyboard users can't see where they are"))
            elif not replaced:
                out.append(Finding("fail", "focus", f"{label}: removes the focus outline and draws "
                                   "nothing in its place"))
            continue
        if ":focus" in sel and outline:
            cols = [parse_color(t) for t in COLOR_TOKEN.findall(outline)] + \
                   [parse_color(w) for w in outline.split() if w in NAMED_COLORS]
            cols = [c for c in cols if c]
            for c in cols:
                ratio = contrast_ratio(_blend(c, page_bg), page_bg)
                if ratio < NON_TEXT_RATIO:
                    out.append(Finding("fail", "focus", f"{label}: focus outline {_hex(_blend(c, page_bg))} "
                                       f"is {ratio}:1 against the page, needs {NON_TEXT_RATIO}:1"))

    # motion
    moving = []
    for r in rules:
        if "prefers-reduced-motion" in r.media:
            continue
        for k, v in r.decls.items():
            v = v.strip().lower()
            if (k in ("transition", "transition-property", "transition-duration") and v not in ("none", "0s", "0")) \
                    or (k in ("animation", "animation-name") and v not in ("none",)) \
                    or (k == "scroll-behavior" and v == "smooth"):
                moving.append(r.selector)
                break
    if moving and not any("prefers-reduced-motion" in r.media for r in rules):
        out.append(Finding("fail", "motion", f"{where}: {len(moving)} rule(s) move things "
                           f"({', '.join(moving[:3])}) but there's no prefers-reduced-motion rule"))
    return out


def _hex(rgb) -> str:
    return "#" + "".join(f"{c:02x}" for c in rgb[:3])


# ------------------------------------------------------------------ HTML checks

def check_html(doc: _TreeBuilder) -> list[Finding]:
    out: list[Finding] = []
    nodes = list(doc.root.walk())[1:]
    ids: dict[str, Node] = {}
    dupes = set()
    for n in nodes:
        i = n.get("id")
        if i:
            if i in ids:
                dupes.add(i)
            ids[i] = n
    for i in sorted(dupes):
        out.append(Finding("fail", "ids", f"id {i!r} is used more than once"))

    html = next((n for n in nodes if n.tag == "html"), None)
    if html is None or not (html.get("lang") or "").strip():
        out.append(Finding("fail", "lang", "<html> has no lang, so screen readers guess the language"))
    title = next((n for n in nodes if n.tag == "title"), None)
    if title is None or not _plain(title):
        out.append(Finding("fail", "title", "page has no title"))
    vp = next((n for n in nodes if n.tag == "meta" and (n.get("name") or "").lower() == "viewport"), None)
    if vp is None:
        out.append(Finding("fail", "zoom", "no viewport tag, so phones show a shrunken desktop page"))
    else:
        content = (vp.get("content") or "").lower().replace(" ", "")
        kv = dict(p.split("=", 1) for p in content.split(",") if "=" in p)
        try:
            max_scale = float(kv.get("maximum-scale", "10"))
        except ValueError:
            max_scale = 10
        if kv.get("user-scalable") in ("no", "0") or max_scale < 2:
            out.append(Finding("fail", "zoom", f"viewport {content!r} stops people zooming in"))

    for n in nodes:
        if n.tag == "img" and "alt" not in n.attrs:
            out.append(Finding("fail", "image-alt", f"<img src={n.get('src')!r}> has no alt "
                               "(use alt=\"\" if it's decoration)"))
        if n.tag == "svg" and not _hidden(n):
            if (n.get("role") or "").lower() != "img":
                out.append(Finding("fail", "svg", "inline <svg> is neither aria-hidden=\"true\" "
                                   "(decoration) nor role=\"img\" with a label"))
            elif not _text(n, ids):
                out.append(Finding("fail", "svg", "<svg role=\"img\"> has no name (aria-label or <title>)"))

    for n in nodes:
        if _hidden(n):
            continue
        if n.tag == "a" and n.get("href") is not None and not _text(n, ids):
            out.append(Finding("fail", "link-name", f"link to {n.get('href')!r} has no text a screen "
                               "reader can read"))
        is_button = n.tag == "button" or (n.get("role") or "").lower() == "button"
        if is_button and not _text(n, ids):
            out.append(Finding("fail", "button-name", f"button{_ident(n)} has no text a screen reader "
                               "can read"))
        if n.tag == "input" and (n.get("type") or "").lower() in ("submit", "reset", "button") \
                and (n.get("type") or "").lower() == "button" and not (n.get("value") or n.get("aria-label")):
            out.append(Finding("fail", "button-name", f"input button{_ident(n)} has no value or label"))
        if n.tag == "input" and (n.get("type") or "").lower() == "image" and not (n.get("alt") or "").strip():
            out.append(Finding("fail", "button-name", f"image button{_ident(n)} has no alt"))
        if _is_field(n) and not _field_named(n, nodes, ids):
            out.append(Finding("fail", "field-label", f"form field{_ident(n)} has no label "
                               "(a placeholder isn't one)"))

    # references
    for n in nodes:
        for attr in ("aria-controls", "aria-labelledby", "aria-describedby", "for"):
            if n.get(attr) is None or (attr == "for" and n.tag != "label"):
                continue
            for ref in n.get(attr).split():
                if ref not in ids:
                    out.append(Finding("fail", "reference", f"<{n.tag} {attr}={ref!r}> points at an id "
                                       "that doesn't exist"))

    # headings
    heads = [(int(n.tag[1]), _plain(n)) for n in nodes if re.fullmatch(r"h[1-6]", n.tag)]
    h1 = sum(1 for lvl, _ in heads if lvl == 1)
    if h1 != 1:
        out.append(Finding("fail", "headings", f"page has {h1} <h1> (needs exactly one)"))
    prev = 0
    for lvl, text in heads:
        if prev and lvl > prev + 1:
            out.append(Finding("fail", "headings", f"<h{lvl}> {text[:40]!r} skips a level after <h{prev}>"))
        prev = lvl

    mains = [n for n in nodes if n.tag == "main" or (n.get("role") or "").lower() == "main"]
    if len(mains) != 1:
        out.append(Finding("fail", "landmarks", f"page has {len(mains)} <main> (needs exactly one)"))

    if any(n.tag == "nav" for n in nodes):
        first = next((n for n in nodes if _focusable(n) and not _hidden(n)
                      and not (n.get("tabindex") or "").startswith("-")), None)
        href = (first.get("href") or "") if first is not None and first.tag == "a" else ""
        if not href.startswith("#") or len(href) < 2:
            out.append(Finding("fail", "skip-link", "page has a menu but doesn't start with a skip link, so "
                               "keyboard users tab through the menu on every page"))
        elif urllib.parse.unquote(href[1:]) not in ids:
            out.append(Finding("fail", "skip-link", f"skip link points at {href!r}, which doesn't exist"))
        elif any(a.tag in ("header", "nav") for a in first.ancestors()):
            out.append(Finding("fail", "skip-link", f"the first link ({href!r}) is part of the header, not a "
                               "skip link; add one before the header that jumps to the main content"))
        else:
            target = ids[urllib.parse.unquote(href[1:])]
            if not any(t.tag == "main" or (t.get("role") or "").lower() == "main"
                       for t in [target, *target.ancestors()]):
                out.append(Finding("fail", "skip-link", f"skip link points at {href!r}, which isn't the "
                                   "main content"))

    for n in nodes:
        ti = (n.get("tabindex") or "").strip()
        if ti.lstrip("+").isdigit() and int(ti) > 0:
            out.append(Finding("fail", "tabindex", f"<{n.tag}{_ident(n)}> has tabindex={ti}, which "
                               "changes the keyboard order"))
        if n.get("aria-hidden", "").lower() == "true":
            if any(_focusable(d) and not (d.get("tabindex") or "").startswith("-") for d in n.walk()):
                out.append(Finding("fail", "aria-hidden", f"<{n.tag}{_ident(n)} aria-hidden=\"true\"> "
                                   "contains something keyboard users can still reach"))
    return out


def _ident(n: Node) -> str:
    if n.get("id"):
        return f" #{n.get('id')}"
    if n.get("name"):
        return f" name={n.get('name')!r}"
    return ""


def _is_field(n: Node) -> bool:
    if n.tag in ("select", "textarea"):
        return True
    return n.tag == "input" and (n.get("type") or "text").lower() not in (
        "hidden", "submit", "reset", "button", "image")


def _field_named(n: Node, nodes: list[Node], ids: dict[str, Node]) -> bool:
    if (n.get("aria-label") or "").strip() or (n.get("title") or "").strip():
        return True
    if n.get("aria-labelledby"):
        return bool(_text(n, ids))
    for a in n.ancestors():
        if a.tag == "label":
            return bool(_text(a, ids))
    fid = n.get("id")
    if fid:
        return any(x.tag == "label" and x.get("for") == fid and _text(x, ids) for x in nodes)
    return False


# ------------------------------------------------------------------ site

def _load_css(root: Path, page: str, doc: _TreeBuilder, origin: str = "https://site.invalid") -> tuple[list[Rule], list[str]]:
    rules: list[Rule] = []
    missing = []
    base = origin + "/" + page
    for href in doc.stylesheets:
        url = urllib.parse.urljoin(base, href)
        if not url.startswith(origin + "/"):
            missing.append(href)
            continue
        path = resolve_local(root, urllib.parse.urlsplit(url).path)
        if path is None:
            missing.append(href)
            continue
        rules.extend(parse_css(path.read_text(encoding="utf-8", errors="replace")))
    for css in doc.styles:
        rules.extend(parse_css(css))
    return rules, missing


def check_site(root: Path = DEFAULT_ROOT, owner_pages=DEFAULT_OWNER_PAGES) -> dict:
    root = Path(root)
    if not root.is_dir():
        raise OSError(f"{root} is not a folder")
    owner = set(owner_pages)
    data = {"format": FORMAT, "rule": RULE, "root": str(root),
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "owner_pages": sorted(owner), "ok": True, "checks": []}
    for page in _list_pages(root, hidden_prefixes(root)):
        doc = parse_html((root / page).read_text(encoding="utf-8", errors="replace"))
        findings = check_html(doc)
        rules, missing = _load_css(root, page, doc)
        for href in missing:
            findings.append(Finding("note", "css", f"stylesheet {href!r} isn't a local file, not checked"))
        findings.extend(check_css(rules, "CSS", doc))
        is_owner = page in owner
        failed = False
        for f in findings:
            status = f.status
            detail = f"{page}: {f.detail}"
            if status == "fail" and is_owner:
                status = "note"
                detail += " (owner-approved page: noted only)"
            if status == "fail":
                failed = True
                data["ok"] = False
            data["checks"].append({"name": f.name, "status": status, "detail": detail, "page": page})
        if not failed:
            data["checks"].append({"name": "page", "status": "pass", "page": page,
                                   "detail": f"{page}: no accessibility failures found"})
    counts: dict[str, int] = {}
    for c in data["checks"]:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    data["counts"] = counts
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.a11y_check", description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--owner-page", action="append",
                    help="page whose findings are notes only (repeatable; default: index.html)")
    ap.add_argument("--out", type=Path)
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    try:
        result = check_site(args.root, tuple(args.owner_page) if args.owner_page else DEFAULT_OWNER_PAGES)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for c in result["checks"]:
        print(f"{c['status'].upper():5} {c['name']:12} {c['detail']}")
    counts = result["counts"]
    print(f"\n{'OK' if result['ok'] else 'PROBLEMS FOUND'}: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    if args.out:
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
