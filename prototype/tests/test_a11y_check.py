"""Tests for the website accessibility checker (`a11y-check-v1`): offline checks of each
page's HTML and the CSS it loads (names for links, buttons, images and form fields, headings,
landmarks, skip link, zoom, focus rings, reduced motion and text contrast)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import a11y_check as ac

REPO = Path(__file__).resolve().parents[2]

GOOD_CSS = """:root{--bg:#090705;--text:#f4f1ed;--acid:#ff9c22}
body{background:var(--bg);color:var(--text)}
:focus-visible{outline:3px solid var(--acid)}
.btn{background:linear-gradient(115deg,#ffb21e,#ff493e);color:#160d08;transition:transform .2s}
@media (prefers-reduced-motion:reduce){.btn{transition:none}}
"""

HEAD = """<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test page</title><link rel="stylesheet" href="/assets/site.css"></head>"""

BODY = """<a class="skip" href="#main">Skip to content</a>
<header><a href="/" aria-label="Home"><img src="/a.svg" alt=""></a>
<button id="menubtn" type="button" aria-expanded="false" aria-controls="navlinks">Menu</button>
<nav id="navlinks" aria-label="Main"><a href="#one">One</a></nav></header>
<main id="main"><h1>Title</h1><section id="one"><h2>Part</h2><h3>Bit</h3>
<form><label>Email<input name="email" type="email"></label>
<label for="os">OS</label><select id="os"><option>Linux</option></select>
<input type="hidden" name="form-name" value="x"><button type="submit">Send</button></form>
</section></main>"""


def page(body: str = BODY, head: str = HEAD, lang: str = ' lang="en"') -> str:
    return f"<!DOCTYPE html><html{lang}>{head}<body>{body}</body></html>"


def make_site(tmp_path: Path, pages: dict[str, str], css: str = GOOD_CSS) -> Path:
    root = tmp_path / "site"
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "site.css").write_text(css)
    (root / "netlify.toml").write_text('[[redirects]]\n  from = "/docs/*"\n  to = "/404.html"\n'
                                       '  status = 404\n  force = true\n')
    for name, html in pages.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html)
    return root


def fails(root: Path, **kw) -> list[dict]:
    return [c for c in ac.check_site(root, **kw)["checks"] if c["status"] == "fail"]


def names(checks: list[dict]) -> set[str]:
    return {c["name"] for c in checks}


# ------------------------------------------------------------------ the good page passes

def test_good_page_passes(tmp_path):
    root = make_site(tmp_path, {"good.html": page()})
    result = ac.check_site(root, owner_pages=())
    assert result["ok"], fails(root, owner_pages=())
    assert result["rule"] == "a11y-check-v1" and result["format"] == "bnh-a11y-check/1"
    assert any(c["status"] == "pass" and c["page"] == "good.html" for c in result["checks"])


# ------------------------------------------------------------------ document basics

def test_missing_lang_fails(tmp_path):
    root = make_site(tmp_path, {"p.html": page(lang="")})
    assert "lang" in names(fails(root, owner_pages=()))


def test_empty_title_fails(tmp_path):
    root = make_site(tmp_path, {"p.html": page(head=HEAD.replace("Test page", "  "))})
    assert "title" in names(fails(root, owner_pages=()))


@pytest.mark.parametrize("viewport", ["width=device-width, user-scalable=no",
                                      "width=device-width, maximum-scale=1",
                                      "width=device-width,user-scalable=0"])
def test_viewport_that_blocks_zoom_fails(tmp_path, viewport):
    head = HEAD.replace("width=device-width, initial-scale=1", viewport)
    root = make_site(tmp_path, {"p.html": page(head=head)})
    assert "zoom" in names(fails(root, owner_pages=()))


def test_maximum_scale_of_five_is_fine(tmp_path):
    head = HEAD.replace("initial-scale=1", "initial-scale=1, maximum-scale=5")
    root = make_site(tmp_path, {"p.html": page(head=head)})
    assert not fails(root, owner_pages=())


# ------------------------------------------------------------------ names

def test_image_without_alt_fails_and_empty_alt_is_fine(tmp_path):
    root = make_site(tmp_path, {"p.html": page(BODY.replace('alt=""', ''))})
    assert "image-alt" in names(fails(root, owner_pages=()))


def test_link_with_only_an_unlabelled_image_has_no_name(tmp_path):
    body = BODY.replace('aria-label="Home"', '')
    root = make_site(tmp_path, {"p.html": page(body)})
    assert "link-name" in names(fails(root, owner_pages=()))


def test_link_named_by_its_image_alt(tmp_path):
    body = BODY.replace('aria-label="Home"', '').replace('alt=""', 'alt="Home"')
    root = make_site(tmp_path, {"p.html": page(body)})
    assert not fails(root, owner_pages=())


def test_empty_button_fails(tmp_path):
    root = make_site(tmp_path, {"p.html": page(BODY.replace(">Send<", "><"))})
    assert "button-name" in names(fails(root, owner_pages=()))


def test_button_text_inside_aria_hidden_does_not_count(tmp_path):
    body = BODY.replace(">Send<", '><span aria-hidden="true">→</span><')
    root = make_site(tmp_path, {"p.html": page(body)})
    assert "button-name" in names(fails(root, owner_pages=()))


def test_aria_labelledby_names_a_button(tmp_path):
    body = BODY.replace('<button type="submit">Send</button>',
                        '<span id="lbl">Send it</span><button type="submit" aria-labelledby="lbl"></button>')
    root = make_site(tmp_path, {"p.html": page(body)})
    assert not fails(root, owner_pages=())


def test_input_without_label_fails_placeholder_is_not_a_label(tmp_path):
    body = BODY.replace('<label>Email<input name="email" type="email"></label>',
                        '<input name="email" type="email" placeholder="Email">')
    root = make_site(tmp_path, {"p.html": page(body)})
    assert "field-label" in names(fails(root, owner_pages=()))


def test_label_for_pointing_elsewhere_does_not_name_the_select(tmp_path):
    root = make_site(tmp_path, {"p.html": page(BODY.replace('for="os"', 'for="nope"'))})
    got = names(fails(root, owner_pages=()))
    assert "field-label" in got and "reference" in got


def test_aria_label_names_a_textarea(tmp_path):
    body = BODY.replace("</form>", '<textarea aria-label="Notes"></textarea></form>')
    root = make_site(tmp_path, {"p.html": page(body)})
    assert not fails(root, owner_pages=())


def test_inline_svg_must_be_hidden_or_labelled(tmp_path):
    bad = make_site(tmp_path / "a", {"p.html": page(BODY.replace("<h1>", "<svg><circle r='1'/></svg><h1>"))})
    assert "svg" in names(fails(bad, owner_pages=()))
    for svg in ('<svg aria-hidden="true"></svg>', '<svg role="img" aria-label="Boat"></svg>',
                '<svg role="img"><title>Boat</title></svg>'):
        ok = make_site(tmp_path / str(abs(hash(svg))), {"p.html": page(BODY.replace("<h1>", svg + "<h1>"))})
        assert not fails(ok, owner_pages=()), svg
    unnamed = make_site(tmp_path / "c", {"p.html": page(BODY.replace("<h1>", '<svg role="img"></svg><h1>'))})
    assert "svg" in names(fails(unnamed, owner_pages=()))


# ------------------------------------------------------------------ structure

def test_page_needs_exactly_one_h1(tmp_path):
    none = make_site(tmp_path / "a", {"p.html": page(BODY.replace("<h1>Title</h1>", ""))})
    two = make_site(tmp_path / "b", {"p.html": page(BODY.replace("<h1>Title</h1>", "<h1>A</h1><h1>B</h1>"))})
    assert "headings" in names(fails(none, owner_pages=()))
    assert "headings" in names(fails(two, owner_pages=()))


def test_skipping_a_heading_level_fails_going_back_up_is_fine(tmp_path):
    skip = make_site(tmp_path / "a", {"p.html": page(BODY.replace("<h3>Bit</h3>", "<h4>Bit</h4>"))})
    assert "headings" in names(fails(skip, owner_pages=()))
    back = make_site(tmp_path / "b", {"p.html": page(BODY.replace("<h3>Bit</h3>", "<h3>Bit</h3><h2>Next</h2>"))})
    assert not fails(back, owner_pages=())


def test_page_needs_one_main(tmp_path):
    root = make_site(tmp_path, {"p.html": page(BODY.replace('<main id="main">', '<div id="main">')
                                                .replace("</main>", "</div>"))})
    assert "landmarks" in names(fails(root, owner_pages=()))


def test_page_with_nav_needs_skip_link_first(tmp_path):
    root = make_site(tmp_path, {"p.html": page(BODY.replace('<a class="skip" href="#main">Skip to content</a>', ""))})
    assert "skip-link" in names(fails(root, owner_pages=()))


def test_skip_link_to_missing_target_fails(tmp_path):
    root = make_site(tmp_path, {"p.html": page(BODY.replace('href="#main"', 'href="#content"'))})
    got = names(fails(root, owner_pages=()))
    assert "skip-link" in got


def test_page_without_nav_needs_no_skip_link(tmp_path):
    body = '<main><h1>Lost</h1><a href="/">Back</a></main>'
    root = make_site(tmp_path, {"p.html": page(body)})
    assert not fails(root, owner_pages=())


def test_duplicate_ids_fail(tmp_path):
    root = make_site(tmp_path, {"p.html": page(BODY.replace('<section id="one">', '<section id="main">'))})
    assert "ids" in names(fails(root, owner_pages=()))


def test_aria_controls_to_missing_id_fails(tmp_path):
    root = make_site(tmp_path, {"p.html": page(BODY.replace('aria-controls="navlinks"', 'aria-controls="gone"'))})
    assert "reference" in names(fails(root, owner_pages=()))


def test_positive_tabindex_fails_minus_one_is_fine(tmp_path):
    bad = make_site(tmp_path / "a", {"p.html": page(BODY.replace("<button id", '<button tabindex="2" id'))})
    assert "tabindex" in names(fails(bad, owner_pages=()))
    ok = make_site(tmp_path / "b", {"p.html": page(BODY.replace("<button id", '<button tabindex="-1" id'))})
    assert not fails(ok, owner_pages=())


def test_focusable_inside_aria_hidden_fails(tmp_path):
    body = BODY.replace("</main>", '<div aria-hidden="true"><a href="/x">x</a></div></main>')
    root = make_site(tmp_path, {"p.html": page(body)})
    assert "aria-hidden" in names(fails(root, owner_pages=()))


# ------------------------------------------------------------------ CSS

def test_contrast_ratio_matches_wcag_examples():
    assert ac.contrast_ratio((255, 255, 255), (0, 0, 0)) == pytest.approx(21.0)
    assert ac.contrast_ratio((119, 119, 119), (255, 255, 255)) == pytest.approx(4.48, abs=0.01)
    assert ac.contrast_ratio((0, 0, 0), (0, 0, 0)) == pytest.approx(1.0)


@pytest.mark.parametrize("value,expected", [
    ("#fff", (255, 255, 255, 1.0)), ("#090705", (9, 7, 5, 1.0)), ("#ff9c2233", (255, 156, 34, 0.2)),
    ("rgb(1, 2, 3)", (1, 2, 3, 1.0)), ("rgba(1,2,3,.5)", (1, 2, 3, 0.5)), ("rgb(1 2 3 / 50%)", (1, 2, 3, 0.5)),
    ("white", (255, 255, 255, 1.0)), ("BLACK", (0, 0, 0, 1.0)),
])
def test_parse_color(value, expected):
    got = ac.parse_color(value)
    assert got[:3] == expected[:3] and got[3] == pytest.approx(expected[3], abs=0.01)


@pytest.mark.parametrize("value", ["inherit", "currentColor", "hsl(1 2% 3%)", "#12", "nonsense"])
def test_parse_color_refuses_what_it_cannot_read(value):
    assert ac.parse_color(value) is None


def test_low_contrast_text_fails(tmp_path):
    root = make_site(tmp_path, {"p.html": page()}, css=GOOD_CSS + ".fine{color:#3a3430}")
    bad = fails(root, owner_pages=())
    assert "contrast" in names(bad)
    assert any(".fine" in c["detail"] for c in bad)


def test_text_on_its_own_light_background_is_checked(tmp_path):
    root = make_site(tmp_path, {"p.html": page()}, css=GOOD_CSS + ".tag{background:#ffffff;color:#aaaaaa}")
    assert "contrast" in names(fails(root, owner_pages=()))


def test_every_gradient_stop_is_checked(tmp_path):
    css = GOOD_CSS + ".pill{background:linear-gradient(90deg,#ffffff,#111111);color:#000}"
    root = make_site(tmp_path, {"p.html": page()}, css=css)
    assert "contrast" in names(fails(root, owner_pages=()))


def test_background_without_color_is_checked_against_body_text(tmp_path):
    root = make_site(tmp_path, {"p.html": page()}, css=GOOD_CSS + ".box{background:#eeeeee}")
    assert "contrast" in names(fails(root, owner_pages=()))


def test_decorative_pseudo_elements_are_skipped(tmp_path):
    css = GOOD_CSS + '.badge::before{content:"";background:#ffffff}'
    root = make_site(tmp_path, {"p.html": page()}, css=css)
    assert not fails(root, owner_pages=())


def test_vars_with_fallbacks_and_translucent_text_resolve(tmp_path):
    css = GOOD_CSS + ".a{color:var(--missing, #f4f1ed)}.b{color:#f4f1ed22}"
    root = make_site(tmp_path, {"p.html": page()}, css=css)
    bad = fails(root, owner_pages=())
    assert any(".b" in c["detail"] for c in bad) and not any(".a" in c["detail"] for c in bad)


def test_unreadable_colors_are_noted_not_passed_silently(tmp_path):
    root = make_site(tmp_path, {"p.html": page()}, css=GOOD_CSS + ".h{color:hsl(0 0% 50%)}")
    result = ac.check_site(root, owner_pages=())
    assert any(c["status"] == "note" and c["name"] == "contrast" for c in result["checks"])


def test_removing_the_focus_ring_without_a_replacement_fails(tmp_path):
    for css in ("a{outline:none}", "button:focus{outline:0}"):
        root = make_site(tmp_path / str(len(css)), {"p.html": page()}, css=GOOD_CSS + css)
        assert "focus" in names(fails(root, owner_pages=())), css
    ok = make_site(tmp_path / "ok", {"p.html": page()},
                   css=GOOD_CSS + "input:focus{outline:none;box-shadow:0 0 0 3px #ff9c22}")
    assert not fails(ok, owner_pages=())


def test_faint_focus_ring_fails(tmp_path):
    root = make_site(tmp_path, {"p.html": page()},
                     css=GOOD_CSS.replace("outline:3px solid var(--acid)", "outline:3px solid #1a1612"))
    assert "focus" in names(fails(root, owner_pages=()))


def test_motion_without_reduced_motion_rule_fails(tmp_path):
    css = GOOD_CSS.replace("@media (prefers-reduced-motion:reduce){.btn{transition:none}}", "")
    root = make_site(tmp_path, {"p.html": page()}, css=css)
    assert "motion" in names(fails(root, owner_pages=()))
    smooth = make_site(tmp_path / "s", {"p.html": page()}, css=css.replace("transition:transform .2s", "")
                       + "html{scroll-behavior:smooth}")
    assert "motion" in names(fails(smooth, owner_pages=()))


def test_inline_style_block_is_read_too(tmp_path):
    head = HEAD.replace("</head>", "<style>.x{color:#111}</style></head>")
    root = make_site(tmp_path, {"p.html": page(head=head)})
    assert "contrast" in names(fails(root, owner_pages=()))


# ------------------------------------------------------------------ site-wide behaviour

def test_owner_pages_get_notes_not_failures(tmp_path):
    root = make_site(tmp_path, {"index.html": page(lang="")})
    result = ac.check_site(root)  # default owner page is index.html
    assert result["ok"]
    assert any(c["status"] == "note" and c["name"] == "lang" and c["page"] == "index.html"
               for c in result["checks"])
    strict = ac.check_site(root, owner_pages=())
    assert not strict["ok"]


def test_hidden_folders_are_skipped(tmp_path):
    root = make_site(tmp_path, {"p.html": page(), "docs/draft.html": page(lang="")})
    result = ac.check_site(root, owner_pages=())
    assert result["ok"] and not any(c.get("page", "").startswith("docs/") for c in result["checks"])


def test_cli_exit_codes_and_report(tmp_path, capsys):
    good = make_site(tmp_path / "g", {"p.html": page()})
    out = tmp_path / "r.json"
    assert ac.main(["--root", str(good), "--owner-page", "none.html", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["ok"] is True
    bad = make_site(tmp_path / "b", {"p.html": page(lang="")})
    assert ac.main(["--root", str(bad)]) == 1
    assert "FAIL" in capsys.readouterr().out
    assert ac.main(["--root", str(tmp_path / "missing")]) == 2


def test_real_site_passes():
    result = ac.check_site(REPO)
    assert result["ok"], [c for c in result["checks"] if c["status"] == "fail"]
    pages = {c["page"] for c in result["checks"] if c.get("page")}
    assert {"contribute.html", "reports/index.html", "404.html", "thanks.html", "index.html"} <= pages


# ------------------------------------------------------------------ elements on the page

def test_text_in_a_card_is_checked_against_the_card(tmp_path):
    css = GOOD_CSS + ".card{background:#ffffff;color:#111}.card p{color:#333}"
    body = BODY.replace("</main>", '<div class="card"><p>Hi</p></div></main>')
    root = make_site(tmp_path, {"p.html": page(body)}, css=css)
    assert not fails(root, owner_pages=())  # #333 on white is fine, on the dark page it isn't


def test_dark_text_in_a_light_card_on_another_page_still_counts(tmp_path):
    css = GOOD_CSS + ".card{background:#ffffff;color:#111}.card p{color:#cccccc}"
    body = BODY.replace("</main>", '<div class="card"><p>Hi</p></div></main>')
    root = make_site(tmp_path, {"p.html": page(body)}, css=css)
    assert "contrast" in names(fails(root, owner_pages=()))


def test_child_combinator_and_more_specific_background_win(tmp_path):
    css = GOOD_CSS + (".panel{background:#ffffff}.panel.dark{background:#000000}"
                      ".panel > span{color:#eeeeee}")
    body = BODY.replace("</main>", '<div class="panel dark"><span>Hi</span></div></main>')
    root = make_site(tmp_path, {"p.html": page(body)}, css=css)
    assert not fails(root, owner_pages=())


def test_empty_decoration_with_a_background_is_skipped(tmp_path):
    css = GOOD_CSS + ".dot{width:7px;height:7px;background:#ff593a}"
    body = BODY.replace("<h1>Title</h1>", '<h1><span class="dot"></span>Title</h1>')
    root = make_site(tmp_path, {"p.html": page(body)}, css=css)
    assert not fails(root, owner_pages=())


def test_rules_that_match_nothing_on_the_page_are_still_checked_against_the_page(tmp_path):
    # e.g. elements a script adds later: compare with the page background, to be safe
    root = make_site(tmp_path, {"p.html": page()}, css=GOOD_CSS + ".verdict span{color:#3a3430}")
    assert "contrast" in names(fails(root, owner_pages=()))


def test_brand_link_to_the_top_is_not_a_skip_link(tmp_path):
    body = BODY.replace('<a class="skip" href="#main">Skip to content</a>', "") \
               .replace('<a href="/" aria-label="Home">', '<a href="#main" aria-label="Home">')
    root = make_site(tmp_path, {"p.html": page(body)})
    assert "skip-link" in names(fails(root, owner_pages=()))


def test_skip_link_must_land_in_main(tmp_path):
    body = BODY.replace('href="#main"', 'href="#menubtn"')
    root = make_site(tmp_path, {"p.html": page(body)})
    assert "skip-link" in names(fails(root, owner_pages=()))


def test_inline_style_overrides_the_rule_it_beats(tmp_path):
    css = GOOD_CSS + ".card{background:#ffffff}.card .k{color:#ff9a27}"
    body = BODY.replace("</main>", '<div class="card"><p class="k" style="color:#222">Hi</p></div></main>')
    root = make_site(tmp_path, {"p.html": page(body)}, css=css)
    assert not fails(root, owner_pages=())


def test_inline_style_colours_are_checked_too(tmp_path):
    body = BODY.replace("<h1>Title</h1>", '<h1>Title</h1><p style="color:#1a1a1a">dim</p>')
    root = make_site(tmp_path, {"p.html": page(body)})
    bad = fails(root, owner_pages=())
    assert "contrast" in names(bad) and any("style=" in c["detail"] for c in bad)


# ------------------------------------------------------------------ edges the first tests missed

def test_threshold_is_exactly_4_5(tmp_path):
    just_under = make_site(tmp_path / "a", {"p.html": page()},
                           css=GOOD_CSS + ".t{background:#ffffff;color:#777777}")  # 4.48:1
    assert "contrast" in names(fails(just_under, owner_pages=()))
    just_over = make_site(tmp_path / "b", {"p.html": page()},
                          css=GOOD_CSS + ".t{background:#ffffff;color:#767676}")  # 4.54:1
    assert not fails(just_over, owner_pages=())


def test_hover_colours_are_checked_too(tmp_path):
    root = make_site(tmp_path, {"p.html": page()}, css=GOOD_CSS + "nav a:hover{color:#2a2420}")
    assert "contrast" in names(fails(root, owner_pages=()))


def test_specificity_beats_source_order(tmp_path):
    css = GOOD_CSS + (".panel.dark{background:#000000}.panel{background:#ffffff}"
                      ".panel span{color:#eeeeee}")
    body = BODY.replace("</main>", '<div class="panel dark"><span>Hi</span></div></main>')
    root = make_site(tmp_path, {"p.html": page(body)}, css=css)
    assert not fails(root, owner_pages=())


def test_inline_background_on_an_ancestor_is_used(tmp_path):
    css = GOOD_CSS + ".x{color:#333333}"
    body = BODY.replace("</main>", '<div style="background:#ffffff"><p class="x">Hi</p></div></main>')
    root = make_site(tmp_path, {"p.html": page(body)}, css=css)
    assert not fails(root, owner_pages=())


def test_child_combinator_only_matches_direct_children(tmp_path):
    css = GOOD_CSS + ".light{background:#ffffff;color:#111}.light > span{color:#eeeeee}"
    body = BODY.replace("</main>", '<div class="light"><b><span>Hi</span></b></div></main>')
    root = make_site(tmp_path, {"p.html": page(body)}, css=css)
    # `.light > span` matches nothing here, so it's compared with the dark page: fine
    assert not fails(root, owner_pages=())
