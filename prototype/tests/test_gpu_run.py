"""Tests for the one-run recorder (Phase 3 prep).

The "GPU work" here is a small Python program that writes numbers into a FAKE sysfs tree
laid out like amdgpu's, so a test can make card0 or card1 look busy. Passing these tests
says nothing about whether an RX 470 can run inference.
"""
from __future__ import annotations

import hashlib
import json
import sys
import textwrap
from pathlib import Path

import pytest

from app import gpu_run as gr
from tests.test_gpu_sensors import make_card

FAST = 0.05  # reading interval for tests (the command line allows 0.5 s and up)


def child(code: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(code)]


def busy_file(drm: Path, card: str) -> Path:
    return drm / card / "device" / "gpu_busy_percent"


def work_on(drm: Path, card: str, *, seconds: float = 0.6, busy: int = 97,
            vram: int | None = None, out: str = "hello world\n") -> list[str]:
    """A command that makes `card` look busy for a moment, prints `out`, then goes idle."""
    dev = drm / card / "device"
    vram_line = (f"open({str(dev / 'mem_info_vram_used')!r}, 'w').write('{vram}\\n')"
                 if vram is not None else "")
    return child(f"""
        import sys, time
        open({str(busy_file(drm, card))!r}, 'w').write('{busy}\\n')
        {vram_line}
        time.sleep({seconds})
        sys.stdout.write({out!r})
        open({str(busy_file(drm, card))!r}, 'w').write('0\\n')
    """)


@pytest.fixture
def rig(tmp_path):
    """Two idle AMD cards."""
    make_card(tmp_path, 0, slot="0000:01:00.0", busy=0)
    drm = make_card(tmp_path, 1, slot="0000:02:00.0", busy=0)
    return drm


def levels(report):
    return [f["level"] for f in report["findings"]]


def text(report):
    return " | ".join(f["msg"] for f in report["findings"])


# --- which card did the work ---------------------------------------------------------------

def test_target_card_busy_is_recorded(rig):
    r = gr.run(work_on(rig, "card1"), card="card1", sysfs_root=rig, interval=FAST)
    assert r["schema_version"] == "bnh-gpu-run/1"
    assert r["proves_inference"] is False
    assert r["evidence"]["exit_ok"] is True
    assert r["evidence"]["target_card_busy"] is True
    assert r["evidence"]["busy_elsewhere"] == []
    assert r["sensors"]["readings_during_run"] >= 2
    c1 = next(c for c in r["sensors"]["cards"] if c["card"] == "card1")
    assert c1["during"]["busy_percent"]["max"] == 97
    assert c1["before"]["busy_percent"] == 0
    assert c1["after"]["busy_percent"] == 0
    assert "card1 (0000:02:00.0) was busy during the run (peak 97%)" in text(r)


def test_work_on_the_wrong_card_is_flagged(rig):
    r = gr.run(work_on(rig, "card0"), card="card1", sysfs_root=rig, interval=FAST)
    assert r["evidence"]["target_card_busy"] is False
    assert r["evidence"]["busy_elsewhere"] == ["card0"]
    assert "stayed idle but card0 got busy" in text(r)
    assert "warn" in levels(r)


def test_no_card_busy_means_probably_cpu(rig):
    r = gr.run(child("import time; time.sleep(0.4)"), card="card1", sysfs_root=rig,
               interval=FAST)
    assert r["evidence"]["target_card_busy"] is False
    assert r["evidence"]["busy_elsewhere"] == []
    assert "most likely ran on the CPU" in text(r)


def test_busy_below_threshold_does_not_count(rig):
    r = gr.run(work_on(rig, "card1", busy=gr.BUSY_THRESHOLD - 1), card="card1",
               sysfs_root=rig, interval=FAST)
    assert r["evidence"]["target_card_busy"] is False


def test_other_card_also_busy_is_noted(rig):
    busy_file(rig, "card0").write_text("50\n")
    r = gr.run(work_on(rig, "card1"), card="card1", sysfs_root=rig, interval=FAST)
    assert r["evidence"]["target_card_busy"] is True
    assert r["evidence"]["busy_elsewhere"] == ["card0"]
    assert "Other cards were busy too: card0" in text(r)


def test_target_already_busy_before_start_is_warned(rig):
    busy_file(rig, "card1").write_text("80\n")
    r = gr.run(child("import time; time.sleep(0.3)"), card="card1", sysfs_root=rig,
               interval=FAST)
    assert "was already 80% busy before the run started" in text(r)


def test_too_short_to_tell(rig):
    r = gr.run(child("pass"), card="card1", sysfs_root=rig, interval=5)
    assert r["sensors"]["readings_during_run"] == 0
    assert r["evidence"]["target_card_busy"] is None
    assert "ended before the first reading" in text(r)


def test_missing_card(rig):
    r = gr.run(child("pass"), card="card7", sysfs_root=rig, interval=FAST)
    assert "card7 doesn't exist" in text(r)
    assert r["evidence"]["target_card_busy"] is None


def test_card_without_busy_sensor(tmp_path):
    drm = make_card(tmp_path, 0, busy=None)
    r = gr.run(child("import time; time.sleep(0.3)"), card="card0", sysfs_root=drm,
               interval=FAST)
    assert r["evidence"]["target_card_busy"] is None
    assert "no busy % reading" in text(r)


def test_wrong_driver_and_non_amd(tmp_path):
    make_card(tmp_path, 0, slot="0000:01:00.0", driver="radeon", busy=0)
    drm = make_card(tmp_path, 1, slot="0000:02:00.0", vendor="0x10de", driver="nvidia", busy=0)
    r0 = gr.run(child("pass"), card="card0", sysfs_root=drm, interval=FAST)
    assert "driver is 'radeon', not amdgpu" in text(r0)
    r1 = gr.run(child("pass"), card="card1", sysfs_root=drm, interval=FAST)
    assert "is not an AMD card" in text(r1)


def test_vram_added_and_vram_warning(rig):
    r = gr.run(work_on(rig, "card1", vram=2 * 1024**3), card="card1", sysfs_root=rig,
               interval=FAST)
    c1 = next(c for c in r["sensors"]["cards"] if c["card"] == "card1")
    assert c1["vram_added_mib"] == 1024.0
    assert "added up to 1024 MiB of VRAM" in text(r)
    r2 = gr.run(work_on(rig, "card1", vram=int(3.8 * 1024**3)), card="card1",
                sysfs_root=rig, interval=FAST)
    assert "may not fit reliably" in text(r2)


def test_hot_card_is_warned(tmp_path):
    drm = make_card(tmp_path, 0, busy=0, temp_mc=60000)
    hot = drm / "card0" / "device" / "hwmon" / "hwmon3" / "temp1_input"
    cmd = child(f"""
        import time
        open({str(hot)!r}, 'w').write('90000\\n')
        time.sleep(0.4)
    """)
    r = gr.run(cmd, card="card0", sysfs_root=drm, interval=FAST)
    assert "temperature reached 90 °C during the run" in text(r)


# --- exit codes, output and expected answers -----------------------------------------------

def test_failed_command(rig):
    r = gr.run(child("import sys; sys.stderr.write('vk error: no device\\n'); sys.exit(3)"),
               card="card1", sysfs_root=rig, interval=FAST)
    assert r["run"]["exit_code"] == 3
    assert r["evidence"]["exit_ok"] is False
    assert "vk error: no device" in r["output"]["stderr_tail"]
    assert "exited with code 3" in text(r)


def test_output_hash_and_tail(rig):
    r = gr.run(child("print('line'); print('last line')"), card="card1", sysfs_root=rig,
               interval=FAST)
    raw = b"line\nlast line\n"
    assert r["output"]["stdout_sha256"] == hashlib.sha256(raw).hexdigest()
    assert r["output"]["stdout_bytes"] == len(raw)
    assert r["output"]["stdout_tail"] == "line\nlast line"


def test_tail_is_bounded(rig):
    r = gr.run(child("for i in range(5000): print('row', i)"), card="card1", sysfs_root=rig,
               interval=FAST)
    tail = r["output"]["stdout_tail"]
    assert tail.startswith("[...]\n")
    assert tail.endswith("row 4999")
    assert len(tail.splitlines()) == gr.TAIL_LINES + 1
    assert r["output"]["stdout_bytes"] > gr.TAIL_BYTES


def test_expect_sha256_match_and_mismatch(rig):
    good = hashlib.sha256(b"42\n").hexdigest()
    r = gr.run(child("print(42)"), card="card1", sysfs_root=rig, interval=FAST,
               expect_sha256=good.upper())
    assert r["expected"]["matched"] is True
    assert r["evidence"]["output_matched"] is True
    r2 = gr.run(child("print(41)"), card="card1", sysfs_root=rig, interval=FAST,
                expect_sha256=good)
    assert r2["expected"]["matched"] is False
    assert "did NOT match" in text(r2)


def test_expect_text_uses_whitespace_tolerance(rig):
    r = gr.run(child("print('The  answer\\n is 42 ')"), card="card1", sysfs_root=rig,
               interval=FAST, expect_text="The answer is 42\n")
    assert r["expected"]["method"] == "text-collapsed-whitespace"
    assert r["expected"]["matched"] is True
    r2 = gr.run(child("print('The answer is 43')"), card="card1", sysfs_root=rig,
                interval=FAST, expect_text="The answer is 42")
    assert r2["expected"]["matched"] is False


def test_expect_text_too_long(rig, monkeypatch):
    monkeypatch.setattr(gr, "MAX_COMPARE_BYTES", 100)
    r = gr.run(child("print('x' * 500)"), card="card1", sysfs_root=rig, interval=FAST,
               expect_text="x" * 500)
    assert r["expected"]["matched"] is None
    assert r["expected"]["too_long_to_compare"] is True
    assert "too long to compare" in text(r)


def test_no_expectation_is_noted(rig):
    r = gr.run(child("print(1)"), card="card1", sysfs_root=rig, interval=FAST)
    assert r["expected"]["matched"] is None
    assert "nobody checked the answer" in text(r)


# --- timeouts --------------------------------------------------------------------------------

def test_timeout_stops_the_command_and_its_children(rig, tmp_path):
    marker = tmp_path / "grandchild-finished"
    grandchild = f"import time; time.sleep(3); open({str(marker)!r}, 'w').write('x')"
    cmd = child(f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, '-c', {grandchild!r}])
        time.sleep(30)
    """)
    r = gr.run(cmd, card="card1", sysfs_root=rig, interval=FAST, timeout=0.5)
    assert r["run"]["timed_out"] is True
    assert r["run"]["duration_seconds"] < 10
    assert r["evidence"]["exit_ok"] is False
    assert "was stopped" in text(r)
    import time
    time.sleep(3.5)
    assert not marker.exists()  # the whole process group was stopped


def test_timeout_does_not_compare_partial_output(rig):
    good = hashlib.sha256(b"").hexdigest()
    r = gr.run(child("import time; time.sleep(30)"), card="card1", sysfs_root=rig,
               interval=FAST, timeout=0.3, expect_sha256=good)
    assert r["expected"]["matched"] is None


# --- file hashes, environment, redaction -----------------------------------------------------

def test_argument_files_are_hashed(rig, tmp_path):
    model = tmp_path / "tiny.gguf"
    model.write_bytes(b"GGUF fake model bytes")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hi")
    r = gr.run(child("pass") + ["-m", str(model), f"--file={prompt}", "not-a-file"],
               card="card1", sysfs_root=rig, interval=FAST)
    files = r["command"]["files"]
    assert [f["path"] for f in files] == [str(model), str(prompt)]
    assert files[0]["sha256"] == hashlib.sha256(b"GGUF fake model bytes").hexdigest()
    assert files[0]["bytes"] == len(b"GGUF fake model bytes")
    assert r["command"]["program_sha256"] == hashlib.sha256(
        Path(sys.executable).resolve().read_bytes()).hexdigest()


def test_huge_argument_file_is_not_hashed(rig, tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "MAX_HASH_FILE_BYTES", 4)
    big = tmp_path / "big.bin"
    big.write_bytes(b"0123456789")
    r = gr.run(child("pass") + [str(big)], card="card1", sysfs_root=rig, interval=FAST)
    assert r["command"]["files"][0]["sha256"] is None
    assert "was not hashed (too large to hash)" in text(r)


def test_env_is_passed_and_recorded(rig):
    r = gr.run(child("import os; print(os.environ['GGML_VK_VISIBLE_DEVICES'])"),
               card="card1", sysfs_root=rig, interval=FAST,
               env={"GGML_VK_VISIBLE_DEVICES": "1"})
    assert r["output"]["stdout_tail"] == "1"
    assert r["command"]["env"] == {"GGML_VK_VISIBLE_DEVICES": "1"}


@pytest.mark.parametrize("item", ["OPERATOR_TOKEN=x", "HF_TOKEN=x", "API_KEY=x", "DB_PASSWORD=x",
                                  "MY_SECRET=x", "bad name=x", "NOEQUALS"])
def test_secret_looking_or_bad_env_is_refused(item):
    with pytest.raises(gr.RunRefused):
        gr.parse_env([item])


def test_env_given_twice_is_refused():
    with pytest.raises(gr.RunRefused):
        gr.parse_env(["A=1", "A=2"])


def test_run_refuses_secret_env_directly(rig):
    with pytest.raises(gr.RunRefused):
        gr.run(child("pass"), card="card1", sysfs_root=rig, env={"HF_TOKEN": "x"})


def test_hostname_and_username_are_redacted(rig, tmp_path):
    r = gr.run(child("print('alice@rigbox ran it in /home/alice/llama')"), card="card1",
               sysfs_root=rig, interval=FAST, hostname="rigbox", username="alice")
    assert r["output"]["stdout_tail"] == "[user]@[host] ran it in /home/[user]/llama"
    assert "alice" not in json.dumps(r)


# --- refusals --------------------------------------------------------------------------------

@pytest.mark.parametrize("prog", ["sudo", "/usr/bin/sudo", "su", "doas", "pkexec", "runuser"])
def test_privilege_escalation_is_refused(rig, prog):
    with pytest.raises(gr.RunRefused, match="never uses elevated privileges"):
        gr.run([prog, "true"], card="card1", sysfs_root=rig)


def test_unknown_program_is_refused(rig):
    with pytest.raises(gr.RunRefused, match="program not found"):
        gr.run(["definitely-not-a-real-program-bnh"], card="card1", sysfs_root=rig)


@pytest.mark.parametrize("kwargs", [{"card": "gpu1"}, {"card": "card1", "interval": 0},
                                    {"card": "card1", "timeout": 0},
                                    {"card": "card1", "expect_sha256": "abc"},
                                    {"card": "card1", "expect_sha256": "a" * 64,
                                     "expect_text": "x"}])
def test_bad_settings_are_refused(rig, kwargs):
    with pytest.raises(gr.RunRefused):
        gr.run(child("pass"), sysfs_root=rig, **kwargs)


def test_empty_command_is_refused(rig):
    with pytest.raises(gr.RunRefused):
        gr.run([], card="card1", sysfs_root=rig)


# --- command line ----------------------------------------------------------------------------

def test_cli_writes_report_and_returns_0(rig, tmp_path, capsys):
    out = tmp_path / "run1.json"
    expect = tmp_path / "expected.txt"
    expect.write_text("hello   world")
    code = gr.main(["--card", "card1", "--out", str(out), "--label", "smoke 1",
                    "--interval", str(FAST), "--expect-file", str(expect), "--",
                    *work_on(rig, "card1")], sysfs_root=rig, min_interval=FAST)
    assert code == 0
    report = json.loads(out.read_text())
    assert report["label"] == "smoke 1"
    assert report["evidence"]["output_matched"] is True
    assert report["evidence"]["target_card_busy"] is True
    printed = capsys.readouterr().out
    assert "[ok] card1 (0000:02:00.0) was busy" in printed
    assert f"saved {out}" in printed


def test_cli_returns_1_on_failed_or_mismatched_run(rig, tmp_path):
    code = gr.main(["--card", "card1", "--out", str(tmp_path / "a.json"), "--interval",
                    str(FAST), "--", *child("import sys; sys.exit(2)")],
                   sysfs_root=rig, min_interval=FAST)
    assert code == 1
    code = gr.main(["--card", "card1", "--out", str(tmp_path / "b.json"), "--interval",
                    str(FAST), "--expect-sha256", "0" * 64, "--", *child("print(1)")],
                   sysfs_root=rig, min_interval=FAST)
    assert code == 1


def test_cli_refuses_to_overwrite_before_running(rig, tmp_path):
    out = tmp_path / "run.json"
    out.write_text("keep me")
    marker = tmp_path / "ran"
    code = gr.main(["--card", "card1", "--out", str(out), "--",
                    *child(f"open({str(marker)!r}, 'w').write('x')")], sysfs_root=rig)
    assert code == 2
    assert out.read_text() == "keep me"
    assert not marker.exists()


def test_cli_refusals_return_2(rig, tmp_path, capsys):
    assert gr.main(["--card", "card1", "--out", str(tmp_path / "x.json"), "--env",
                    "HF_TOKEN=abc", "--", *child("pass")], sysfs_root=rig) == 2
    assert gr.main(["--card", "card1", "--out", str(tmp_path / "y.json"), "--",
                    "sudo", "true"], sysfs_root=rig) == 2
    assert gr.main(["--card", "card1", "--out", str(tmp_path / "z.json"), "--expect-file",
                    str(tmp_path / "missing.txt"), "--", *child("pass")], sysfs_root=rig) == 2
    assert not any((tmp_path / n).exists() for n in ("x.json", "y.json", "z.json"))
    assert "abc" not in capsys.readouterr().err


@pytest.mark.parametrize("args", [["--interval", "0.1"], ["--interval", "61"],
                                  ["--timeout", "0.5"], ["--timeout", "999999"], []])
def test_cli_rejects_bad_options(rig, tmp_path, args):
    argv = ["--card", "card1", "--out", str(tmp_path / "r.json"), *args]
    if args:
        argv += ["--", *child("pass")]
    with pytest.raises(SystemExit) as exc:
        gr.main(argv, sysfs_root=rig)
    assert exc.value.code == 2
