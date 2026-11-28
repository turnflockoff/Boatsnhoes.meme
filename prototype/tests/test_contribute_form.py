"""Guards for what volunteers paste into the /contribute tester form (0.5.77).

Before 0.5.77 the "Hardware check output" field was posted as typed. Output copied from a
terminal usually carries the prompt (`alice@rig-01:~$`), home folders (`/home/alice/...`) and,
from `vulkaninfo`, device UUIDs; the page only asked people to remove them by hand. Now the
form finds them, says what it found in a spoken status line, offers "Remove them", and blocks
sending while one is left. The page's own matching code is run for real under Node here.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parents[2]
JS = (SITE / "assets" / "contribute.js").read_text(encoding="utf-8")
HTML = (SITE / "contribute.html").read_text(encoding="utf-8")

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="needs Node to run the page's own code")

# Real `vulkaninfo --summary` lines (Mesa llvmpipe in this sandbox, a home-folder layer added),
# lspci from the hw_inventory tests, and the browser report the page itself makes.
VULKAN_REAL = """\
ERROR: [Loader Message] Code 0 : /home/alice/obs-vkcapture/libVkLayer_obs.so: cannot open shared object file: No such file or directory
'DISPLAY' environment variable not set... skipping surface info
==========
VULKANINFO
==========

Vulkan Instance Version: 1.3.275

Instance Layers: count = 3
--------------------------
VK_LAYER_INTEL_nullhw       INTEL NULL HW                1.1.73   version 1
VK_LAYER_MESA_device_select Linux device selection layer 1.4.303  version 1

Devices:
========
GPU0:
\tapiVersion         = 1.4.318
\tdriverVersion      = 25.2.8
\tvendorID           = 0x10005
\tdeviceName         = llvmpipe (LLVM 20.1.2, 256 bits)
\tdriverInfo         = Mesa 25.2.8-0ubuntu0.24.04.5 (LLVM 20.1.2)
\tconformanceVersion = 1.3.1.1
\tdeviceUUID         = 6d657361-3235-2e32-2e38-2d3075627500
\tdriverUUID         = 6c6c766d-7069-7065-5555-494400000000
"""
LSPCI = """\
alice@rig-01:~$ lspci -nnk | grep -A3 -E 'VGA|Display'
01:00.0 VGA compatible controller [0300]: Advanced Micro Devices, Inc. [AMD/ATI] Ellesmere [Radeon RX 470/480/570/570X/580/580X/590] [1002:67df] (rev ef)
\tSubsystem: Sapphire Technology Limited Nitro+ Radeon RX 470/480 [1da2:e366]
\tKernel driver in use: amdgpu
\tKernel modules: amdgpu
[alice@rig-01 ~]$ ls -l /dev/dri
crw-rw----+ 1 root video  226,   0 Nov 28 10:00 card0
crw-rw-rw-  1 root render 226, 128 Nov 28 10:00 renderD128
"""
BROWSER_REPORT = """\
Boats N Hoes browser check
Result: Polaris-family AMD GPU
Graphics (WebGL): ANGLE (AMD, AMD Radeon RX 470 Graphics (polaris10, LLVM 15.0.7, DRM 3.54, 6.8.0-45-generic), OpenGL 4.6)
GPU vendor: amd
WebGPU: Yes · gcn-4
Max GPU buffer: 2048 MB (browser limit, not VRAM)
CPU threads: 12
Device memory: ≈8 GB (rounded by browser)
Platform: Linux
Checked: 2026-11-28T10:00:00.000Z"""
CLEAN = {
    "browser report": BROWSER_REPORT,
    "lspci without the prompt": "\n".join(l for l in LSPCI.splitlines() if "@" not in l),
    "uname": "6.8.0-45-generic",
    "pci addresses": "0000:01:00.0 and 0000:02:00.0, ids [1002:67df]",
    "versions": "vulkan-tools 1.3.275.0, Mesa 25.2.8, kernel 6.8.0-45",
    "already scrubbed": "[user]@[host]:~$ ls /home/[user]/x C:\\Users\\[user]\\y [uuid] [mac]",
    "plain words": "Two RX 470s on Ubuntu 24.04, 8 GB each. Works at home/office.",
}
DIRTY = {
    "prompt": ("alice@rig-01:~$ uname -r", "a user@host name (prompt or email)"),
    "fedora prompt": ("[bob@miner ~]$ uname -r", "a user@host name (prompt or email)"),
    "email": ("contact me at bob.smith@example.com please", "a user@host name (prompt or email)"),
    "home": ("ICD at /home/alice/.local/share/vulkan", "a home folder name"),
    "mac home": ("/Users/carol/Library/x", "a home folder name"),
    "silverblue home": ("/var/home/dan/.config", "a home folder name"),
    "windows home": ("C:\\Users\\Jane Doe\\AppData\\x", "a Windows user folder name"),
    "uuid": ("deviceUUID = 00000000-0300-0000-0000-000000000000", "a device ID (UUID)"),
    "mac": ("ether 3c:7c:3f:1a:2b:4d brd", "a network card address (MAC)"),
    "mac dashes": ("3C-7C-3F-1A-2B-4D", "a network card address (MAC)"),
}


def _code() -> str:
    start = JS.index("const REPORT_MAX = ")
    end = JS.index("const field = $('#reportfield')")
    return JS[start:end]


def _run(texts: dict[str, str]) -> dict[str, dict]:
    script = (
        _code()
        + "\nconst texts = JSON.parse(process.argv[1]);\nconst out = {};\n"
        + "for (const [k, t] of Object.entries(texts)) {\n"
        + "  const s = scrubPersonal(t);\n"
        + "  out[k] = {found: findPersonal(t), problem: reportProblem(t), scrubbed: s,\n"
        + "            after: findPersonal(s), again: scrubPersonal(s)};\n"
        + "}\nconsole.log(JSON.stringify(out));\n"
    )
    done = subprocess.run(["node", "-e", script, json.dumps(texts)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_form_has_a_spoken_status_line_and_a_remove_button():
    assert 'aria-describedby="report-status"' in HTML
    assert re.search(r'<p[^>]*id="report-status"[^>]*role="status"[^>]*aria-live="polite"', HTML)
    assert re.search(r'<button[^>]*id="report-scrub"[^>]*type="button"[^>]*hidden>Remove them<', HTML)


def test_field_limit_matches_the_page():
    assert 'id="reportfield" rows="5" maxlength="12000"' in HTML
    assert "const REPORT_MAX = 12000;" in JS and "the limit is 12,000" in JS


def test_check_runs_on_typing_on_attach_on_load_and_blocks_send():
    assert "field.addEventListener('input', checkReport)" in JS
    attach = JS[JS.index("$('#attachreport')"):]
    assert attach.index("checkReport()") < attach.index("scrollIntoView")
    assert "field.setCustomValidity(problem)" in JS
    assert re.search(r"addEventListener\('submit', \(e\) => \{ if \(checkReport\(\)\) "
                     r"\{ e\.preventDefault\(\)", JS)
    assert 'checkReport(); // a value the browser restored after "Back"' in JS


def test_page_no_longer_leaves_it_to_people_alone():
    assert "Remove usernames or hostnames before you share the output." not in HTML
    assert "offers to remove them before anything is sent" in HTML


@needs_node
def test_real_outputs_are_found_and_fully_cleaned():
    got = _run({"vulkan": VULKAN_REAL, "lspci": LSPCI})
    assert set(got["vulkan"]["found"]) == {"a home folder name", "a device ID (UUID)"}
    assert set(got["lspci"]["found"]) == {"a user@host name (prompt or email)"}
    for k, r in got.items():
        assert r["after"] == [], k
        assert r["again"] == r["scrubbed"], k  # cleaning twice changes nothing
        assert "alice" not in r["scrubbed"] and "rig-01" not in r["scrubbed"], k
        assert "6d657361" not in r["scrubbed"], k
    # Everything that isn't personal is kept as it was.
    assert "deviceName         = llvmpipe (LLVM 20.1.2, 256 bits)" in got["vulkan"]["scrubbed"]
    assert "/home/[user]/obs-vkcapture/libVkLayer_obs.so" in got["vulkan"]["scrubbed"]
    assert "[user]@[host]:~$ lspci -nnk" in got["lspci"]["scrubbed"]
    assert "[[user]@[host] ~]$ ls -l /dev/dri" in got["lspci"]["scrubbed"]
    assert "[1002:67df] (rev ef)" in got["lspci"]["scrubbed"]


@needs_node
def test_each_kind_is_found_and_named():
    got = _run({k: v[0] for k, v in DIRTY.items()})
    for k, (text, label) in DIRTY.items():
        assert got[k]["found"] == [label], (k, got[k])
        assert label in got[k]["problem"], k
        assert got[k]["after"] == [] and got[k]["again"] == got[k]["scrubbed"], k


@needs_node
def test_ordinary_output_is_left_alone():
    got = _run(CLEAN)
    for k, text in CLEAN.items():
        assert got[k]["found"] == [] and got[k]["problem"] == "", (k, got[k])
        assert got[k]["scrubbed"] == text, k


@needs_node
def test_too_long_output_is_refused_with_the_limit():
    got = _run({"long": "x" * 12001, "edge": "x" * 12000})
    assert "12,001 characters; the limit is 12,000" in got["long"]["problem"]
    assert got["edge"]["problem"] == ""


def test_no_look_behind_in_any_page_script():
    # Safari before 16.4 can't parse `(?<=` / `(?<!`; one in a file stops that whole script.
    for js in sorted((SITE / "assets").glob("*.js")):
        assert not re.search(r"\(\?<[=!]", js.read_text(encoding="utf-8")), js.name


@needs_node
def test_neighbouring_matches_are_all_cleaned():
    got = _run({"two": "a@b c@d\nx@y /home/p /home/q 00:11:22:33:44:55 66:77:88:99:aa:bb"})
    assert got["two"]["scrubbed"] == ("[user]@[host] [user]@[host]\n[user]@[host] /home/[user] "
                                      "/home/[user] [mac] [mac]")
