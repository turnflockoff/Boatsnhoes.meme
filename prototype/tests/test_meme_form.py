"""Guards for the Meme Lab submission form (meme-lab.html + assets/meme-lab.js), 0.5.76.

Netlify refuses a whole form post over 8 MB, and the browser's `accept=` list is only a hint
(any file can be picked with "All files"). Before 0.5.76 a 9 MB picture or a PDF was posted
anyway (the visitor lost what they typed), and "Send it in" attached a 4-byte file containing
the text `null` when the browser couldn't make the picture. These tests keep the fix in place:
the size limit in the script matches the page, the file is sniffed before sending, a failed
picture is never attached, and the picture-sniffing function is run for real under Node.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parents[2]
JS = (SITE / "assets" / "meme-lab.js").read_text(encoding="utf-8")
HTML = (SITE / "meme-lab.html").read_text(encoding="utf-8")
NETLIFY_FORM_LIMIT = 8 * 1000 * 1000  # the smaller reading of "8 MB", to be safe


def _limit_bytes() -> float:
    m = re.search(r"const SUBMIT_MAX_BYTES = ([0-9.]+) \* 1024 \* 1024;", JS)
    assert m, "SUBMIT_MAX_BYTES missing from meme-lab.js"
    return float(m.group(1)) * 1024 * 1024


def test_limit_leaves_room_under_netlifys_8_mb():
    # The text fields and multipart framing need room too (a few KB at most).
    assert _limit_bytes() <= NETLIFY_FORM_LIMIT - 64 * 1024


def test_page_states_the_same_limit_the_script_enforces():
    mb = _limit_bytes() / (1024 * 1024)
    shown = f"{mb:g} MB"
    assert f"under {shown}" in HTML, f"page should say 'under {shown}'"
    assert "up to 8 MB" not in HTML
    assert f"under {shown}" in JS  # the error message says the same number


def test_form_has_a_spoken_status_line_for_the_file():
    assert 'id="submit-file"' in HTML and 'aria-describedby="submit-status"' in HTML
    assert re.search(r'<p[^>]*id="submit-status"[^>]*role="status"[^>]*aria-live="polite"', HTML)


def test_accept_lists_match_what_the_script_recognises():
    accepts = set(re.findall(r'accept="([^"]+)"', HTML))
    assert accepts == {"image/png,image/jpeg,image/gif,image/webp"}
    assert "^image\\/(png|jpeg|gif|webp)$" in JS


def test_check_runs_on_change_on_send_and_after_send_it_in():
    assert "submitFile.addEventListener('change'" in JS
    assert "form.addEventListener('submit'" in JS and "requestSubmit" in JS
    use = JS[JS.index("$('meme-use')"):]
    assert use.index("await checkSubmission()") < use.index("Added to the submission form")
    assert "setCustomValidity(problem)" in JS


def test_failed_picture_is_never_attached_or_thrown():
    start = JS.index("function toBlob()")
    to_blob = JS[start:JS.index("\n  }\n", start)]
    assert "try {" in to_blob and "catch (e) { resolve(null); }" in to_blob
    assert "b && b.size ? b : null" in to_blob  # an empty picture counts as no picture
    use = JS[JS.index("$('meme-use')"):]
    assert use.index("if (!blob)") < use.index("new File([blob]")


def _sniff_source() -> str:
    start = JS.index("function looksLikePicture(b)")
    end = JS.index("async function checkSubmission()")
    return JS[start:end]


SAMPLES = {
    "png": (b"\x89PNG\r\n\x1a\n" + b"\0" * 8, True),
    "jpeg": (b"\xff\xd8\xff\xe0" + b"\0" * 12, True),
    "gif87": (b"GIF87a" + b"\0" * 10, True),
    "gif89": (b"GIF89a" + b"\0" * 10, True),
    "webp": (b"RIFF\x10\0\0\0WEBPVP8 ", True),
    "empty": (b"", False),
    "pdf": (b"%PDF-1.4 hello th", False),
    "wav": (b"RIFF\x10\0\0\0WAVEfmt ", False),
    "png-short": (b"\x89PNG\r\n", False),
    "png-wrong-tail": (b"\x89PNG\r\n\x1b\n" + b"\0" * 8, False),
    "html": (b"<!doctype html><h", False),
    "svg": (b"<svg xmlns='http:", False),
    "zip": (b"PK\x03\x04" + b"\0" * 12, False),
    "jpeg-two-bytes": (b"\xff\xd8\x00\x00" + b"\0" * 12, False),
}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node to run the page's own code")
def test_sniff_function_runs_for_real_under_node():
    script = (
        _sniff_source()
        + "\nconst samples = JSON.parse(process.argv[1]);\n"
        + "const out = {};\n"
        + "for (const [k, hex] of Object.entries(samples)) out[k] = looksLikePicture(Uint8Array.from(Buffer.from(hex, 'hex')));\n"
        + "console.log(JSON.stringify(out));\n"
    )
    arg = json.dumps({k: v[0].hex() for k, v in SAMPLES.items()})
    done = subprocess.run(["node", "-e", script, arg], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got == {k: v[1] for k, v in SAMPLES.items()}
