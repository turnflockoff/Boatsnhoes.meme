"""Record one test run on one GPU card, with sensor readings taken while it runs (Phase 3).

Run it on the Ubuntu machine that holds the cards, putting your own test command after `--`:

    python -m app.gpu_run --card card1 --label "first smoke test" --out run1.json -- \\
        ./llama-cli -m models/tiny.gguf -p "Hello" -n 16 --seed 1

What it does, in order:
1. Refuses to start if --out already exists, if the command is sudo/su/doas/pkexec/runuser,
   or if an --env name looks like a secret (TOKEN, SECRET, PASSWORD, KEY, ...).
2. Hashes the program and every argument that names an existing file (for example the model),
   so the report says exactly which files were used.
3. Takes one sensor reading of every card, starts your command (no shell, no sudo, stdin
   closed), reads every card again every --interval seconds until it ends or --timeout
   passes (then it is stopped), and takes one last reading.
4. Writes a versioned JSON report: exit code, duration, a hash and the last lines of the
   output, whether the output matched what you expected (--expect-sha256 or --expect-file),
   and, per card, temperature, power, VRAM and busy % before, during and after.

The most useful line is which card was actually busy. If you asked for card1 but card0 did
the work (or no card did, meaning it ran on the CPU), the findings say so.

What it never does: use a shell or sudo, install anything, change drivers, fans or clocks,
or write to sysfs. It runs only the command you give it, as you.

A run report is a record, not proof. It becomes evidence when someone else can repeat the
same command with the same file hashes and get the same output. `proves_inference` is
always false.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import gpu_sensors as gs
from app.hw_inventory import _redactor

SCHEMA_VERSION = "bnh-gpu-run/1"
NOTE = ("This is a record of one run on one machine. It is not proof that a GPU ran AI "
        "inference: it becomes evidence when someone else repeats the same command with the "
        "same file hashes and gets the same output.")
MIN_INTERVAL, MAX_INTERVAL = 0.5, 60.0
MIN_TIMEOUT, MAX_TIMEOUT = 1.0, 6 * 3600.0
MAX_READINGS = 20_000            # stop keeping readings past this (about 5.5 h at 1 s)
BUSY_THRESHOLD = 10              # a card counts as "busy" at or above this gpu_busy_percent
TAIL_BYTES = 8192                # keep at most this much of the end of each output stream
TAIL_LINES = 40
MAX_COMPARE_BYTES = 4 * 1024 * 1024   # keep at most this much stdout to compare as text
MAX_HASH_FILE_BYTES = 64 * 1024**3    # don't hash argument files bigger than this
STOP_GRACE_SECONDS = 5.0
REFUSED_PROGRAMS = {"sudo", "su", "doas", "pkexec", "runuser"}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASS|KEY|CREDENTIAL|AUTH|COOKIE", re.IGNORECASE)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RunRefused(ValueError):
    """The run was refused before anything started."""


# --- helpers -------------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def collapse_ws(text: str) -> str:
    """Same tolerance as the llm:generate task: equal after collapsing whitespace."""
    return " ".join(text.split())


def parse_env(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        name, sep, value = item.partition("=")
        if not sep or not _ENV_NAME.match(name):
            raise RunRefused(f"--env must look like NAME=value (got {item!r})")
        if _SECRET_NAME.search(name):
            raise RunRefused(f"--env {name}: names that look like secrets are refused, because "
                             "the report records every --env value")
        if name in out:
            raise RunRefused(f"--env {name} given twice")
        out[name] = value
    return out


def _argument_files(argv: list[str]) -> list[dict[str, Any]]:
    """Hash every argument (or the part after '=') that names an existing regular file."""
    files, seen = [], set()
    for i, arg in enumerate(argv[1:], start=1):
        candidates = [arg]
        if "=" in arg:
            candidates.append(arg.split("=", 1)[1])
        for cand in candidates:
            if not cand or cand in seen:
                continue
            p = Path(cand)
            try:
                if not p.is_file():
                    continue
                size = p.stat().st_size
            except OSError:
                continue
            seen.add(cand)
            entry: dict[str, Any] = {"arg_index": i, "path": cand, "bytes": size}
            if size > MAX_HASH_FILE_BYTES:
                entry["sha256"] = None
                entry["skipped"] = "too large to hash"
            else:
                try:
                    entry["sha256"] = _sha256_file(p)
                except OSError as exc:
                    entry["sha256"] = None
                    entry["skipped"] = f"could not read: {exc.strerror or exc}"
            files.append(entry)
            break
    return files


class _Capture:
    """Hash and count a whole output stream, keeping only its end (and a bounded copy)."""

    def __init__(self, keep_full: bool):
        self.sha = hashlib.sha256()
        self.bytes = 0
        self.tail = bytearray()
        self.keep_full = keep_full
        self.full = bytearray()
        self.full_overflow = False

    def feed(self, chunk: bytes) -> None:
        self.sha.update(chunk)
        self.bytes += len(chunk)
        self.tail += chunk
        if len(self.tail) > TAIL_BYTES:
            del self.tail[:len(self.tail) - TAIL_BYTES]
        if self.keep_full and not self.full_overflow:
            if len(self.full) + len(chunk) > MAX_COMPARE_BYTES:
                self.full_overflow = True
                self.full.clear()
            else:
                self.full += chunk

    def tail_text(self) -> str:
        text = bytes(self.tail).decode("utf-8", errors="replace")
        lines = text.splitlines()
        cut = self.bytes > len(self.tail) or len(lines) > TAIL_LINES
        return ("[...]\n" if cut else "") + "\n".join(lines[-TAIL_LINES:])


def _pump(stream: Any, cap: _Capture) -> None:
    fd = stream.fileno()
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        cap.feed(chunk)


def _stop(proc: subprocess.Popen) -> None:
    """Stop the command and anything it started (it runs in its own process group)."""
    for sig, wait in ((signal.SIGTERM, STOP_GRACE_SECONDS), (signal.SIGKILL, STOP_GRACE_SECONDS)):
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue


def _read_all(sysfs_root: Path) -> list[dict[str, Any]]:
    return [gs.read_card(p) for p in gs.list_cards(sysfs_root)]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the run -------------------------------------------------------------------------------

def run(command: list[str], *, card: str, sysfs_root: Path = gs.DEFAULT_SYSFS,
        timeout: float = 900.0, interval: float = 1.0, env: dict[str, str] | None = None,
        expect_sha256: str | None = None, expect_text: str | None = None,
        label: str | None = None, hostname: str | None = None, username: str | None = None,
        wall_clock: Callable[[], float] = time.time,
        on_reading: Callable[[int, list[dict[str, Any]]], None] | None = None) -> dict[str, Any]:
    """Run `command` once, reading every card's sensors while it runs. Returns the report."""
    if not command:
        raise RunRefused("give the command to run after --")
    if not gs._CARD_RE.match(card):
        raise RunRefused(f"--card must look like card0, card1, ... (got {card!r})")
    if not 0 < interval <= MAX_INTERVAL:
        raise RunRefused(f"interval must be above 0 and at most {MAX_INTERVAL:g} seconds")
    if not 0 < timeout <= MAX_TIMEOUT:
        raise RunRefused(f"timeout must be above 0 and at most {MAX_TIMEOUT:g} seconds")
    if expect_sha256 is not None and expect_text is not None:
        raise RunRefused("give --expect-sha256 or --expect-file, not both")
    if expect_sha256 is not None:
        expect_sha256 = expect_sha256.strip().lower()
        if not _SHA256.match(expect_sha256):
            raise RunRefused("--expect-sha256 must be 64 hex characters")
    if Path(command[0]).name in REFUSED_PROGRAMS:
        raise RunRefused(f"refusing to run {Path(command[0]).name}: this tool never uses "
                         "elevated privileges. Run the test as your normal user.")
    extra_env = dict(env or {})
    for name in extra_env:
        if not _ENV_NAME.match(name) or _SECRET_NAME.search(name):
            raise RunRefused(f"refusing --env {name!r}")
    program = shutil.which(command[0])
    if program is None:
        raise RunRefused(f"program not found: {command[0]!r}")
    program_path = Path(program).resolve()

    program_sha = _sha256_file(program_path)
    files = _argument_files(command)
    child_env = dict(os.environ)
    child_env.update(extra_env)

    before = _read_all(sysfs_root)
    during: dict[str, list[dict[str, Any]]] = {}
    readings = 0
    out_cap, err_cap = _Capture(keep_full=expect_text is not None), _Capture(keep_full=False)
    timed_out = stopped_early = False
    started_wall = wall_clock()
    t0 = time.monotonic()
    proc = subprocess.Popen([str(program_path), *command[1:]], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env,
                            start_new_session=True, shell=False)
    pumps = [threading.Thread(target=_pump, args=(proc.stdout, out_cap), daemon=True),
             threading.Thread(target=_pump, args=(proc.stderr, err_cap), daemon=True)]
    for t in pumps:
        t.start()
    try:
        while True:
            try:
                proc.wait(timeout=interval)
                break
            except subprocess.TimeoutExpired:
                pass
            now = _read_all(sysfs_root)
            if readings < MAX_READINGS:
                for r in now:
                    during.setdefault(r["card"], []).append(r)
                readings += 1
                if on_reading:
                    on_reading(readings, now)
            if time.monotonic() - t0 >= timeout:
                timed_out = True
                _stop(proc)
                break
    except KeyboardInterrupt:
        stopped_early = True
        _stop(proc)
    duration = time.monotonic() - t0
    for t in pumps:
        t.join(timeout=STOP_GRACE_SECONDS)
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except OSError:
            pass
    after = _read_all(sysfs_root)
    exit_code = proc.poll()

    # --- expected output ---
    matched: bool | None = None
    method = None
    if expect_sha256 is not None:
        method = "sha256-exact"
        matched = None if (timed_out or stopped_early) else out_cap.sha.hexdigest() == expect_sha256
    elif expect_text is not None:
        method = "text-collapsed-whitespace"
        if out_cap.full_overflow or timed_out or stopped_early:
            matched = None
        else:
            got = bytes(out_cap.full).decode("utf-8", errors="replace")
            matched = collapse_ws(got) == collapse_ws(expect_text)

    # --- per-card sensor summary ---
    by_card_before = {r["card"]: r for r in before}
    by_card_after = {r["card"]: r for r in after}
    names = sorted(set(by_card_before) | set(by_card_after) | set(during),
                   key=lambda n: int(n[4:]))
    cards_out = []
    for name in names:
        rs = during.get(name, [])
        b, a = by_card_before.get(name), by_card_after.get(name)
        last = a or (rs[-1] if rs else b)
        summ = gs.summarize(rs)
        peak_v = summ["vram_used_mib"]["max"]
        base_v = b["vram_used_mib"] if b else None
        cards_out.append({
            "card": name,
            "pci_slot": last["pci_slot"] if last else None,
            "is_amd": bool(last and last["is_amd"]),
            "driver": last["driver"] if last else None,
            "before": b,
            "during": summ,
            "after": a,
            "vram_added_mib": (round(peak_v - base_v, 1)
                               if peak_v is not None and base_v is not None else None),
        })

    target = next((c for c in cards_out if c["card"] == card), None)

    def peak_busy(c: dict[str, Any]) -> int | float | None:
        return c["during"]["busy_percent"]["max"]

    target_busy: bool | None = None
    if target is not None and target["during"]["busy_percent"]["readings"]:
        target_busy = (peak_busy(target) or 0) >= BUSY_THRESHOLD
    busy_elsewhere = [c["card"] for c in cards_out
                      if c["card"] != card and c["during"]["busy_percent"]["readings"]
                      and (peak_busy(c) or 0) >= BUSY_THRESHOLD]

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "proves_inference": False,
        "note": NOTE,
        "label": label,
        "target_card": card,
        "command": {
            "argv": [str(program_path), *command[1:]],
            "program_sha256": program_sha,
            "files": files,
            "env": extra_env,
        },
        "run": {
            "started_at": _iso(started_wall),
            "duration_seconds": round(duration, 2),
            "timeout_seconds": timeout,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stopped_early": stopped_early,
            "ran_as_root": hasattr(os, "geteuid") and os.geteuid() == 0,
        },
        "output": {
            "stdout_sha256": out_cap.sha.hexdigest(),
            "stdout_bytes": out_cap.bytes,
            "stdout_tail": out_cap.tail_text(),
            "stderr_bytes": err_cap.bytes,
            "stderr_tail": err_cap.tail_text(),
        },
        "expected": {
            "method": method,
            "expected_sha256": expect_sha256 if expect_sha256 is not None else (
                hashlib.sha256(expect_text.encode("utf-8")).hexdigest()
                if expect_text is not None else None),
            "matched": matched,
            "too_long_to_compare": out_cap.full_overflow,
        },
        "sensors": {
            "interval_seconds": interval,
            "readings_during_run": readings,
            "busy_threshold_percent": BUSY_THRESHOLD,
            "cards": cards_out,
        },
        "evidence": {
            "exit_ok": exit_code == 0 and not timed_out and not stopped_early,
            "output_matched": matched,
            "target_card_busy": target_busy,
            "busy_elsewhere": busy_elsewhere,
        },
    }
    report["findings"] = _findings(report, target)
    return _redactor(hostname, username)(report)


def _findings(report: dict[str, Any], target: dict[str, Any] | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    def add(level: str, msg: str) -> None:
        out.append({"level": level, "msg": msg})

    run_ = report["run"]
    card = report["target_card"]
    ev = report["evidence"]
    if run_["ran_as_root"]:
        add("warn", "This ran as root. A GPU test doesn't need it; run it as your normal user "
                    "(in the render group) instead.")
    if run_["timed_out"]:
        add("warn", f"The command was still running after {run_['timeout_seconds']:g} s and was "
                    "stopped. Output and sensor numbers cover only part of the run.")
    elif run_["stopped_early"]:
        add("warn", "Stopped with Ctrl-C before the command finished. Output and sensor numbers "
                    "cover only part of the run.")
    elif run_["exit_code"] != 0:
        add("warn", f"The command exited with code {run_['exit_code']}, so the run failed. "
                    "The last lines of its error output are in output.stderr_tail.")
    else:
        add("ok", f"The command finished with exit code 0 in {run_['duration_seconds']:g} s.")

    exp = report["expected"]
    if exp["method"] is None:
        add("info", "No expected output was given, so nobody checked the answer. Next time pass "
                    "--expect-file or --expect-sha256 with a known-good output.")
    elif exp["matched"] is True:
        add("ok", f"The output matched the expected output ({exp['method']}).")
    elif exp["matched"] is False:
        add("warn", f"The output did NOT match the expected output ({exp['method']}).")
    elif exp["too_long_to_compare"]:
        add("warn", "The output was too long to compare as text; use --expect-sha256 instead.")
    else:
        add("info", "The output wasn't compared because the run didn't finish.")

    for f in report["command"]["files"]:
        if f.get("sha256") is None:
            add("info", f"Argument file {f['path']} was not hashed ({f.get('skipped')}).")

    if target is None:
        add("warn", f"{card} doesn't exist under /sys/class/drm, so there are no readings for it. "
                    "Run python -m app.gpu_sensors to see the card names.")
        return out
    name = f"{card} ({target['pci_slot'] or 'unknown slot'})"
    if not target["is_amd"]:
        add("warn", f"{name} is not an AMD card.")
    elif target["driver"] != "amdgpu":
        add("warn", f"{name}: driver is '{target['driver'] or 'none'}', not amdgpu, so the "
                    "readings are mostly missing.")

    readings = report["sensors"]["readings_during_run"]
    before_busy = (target["before"] or {}).get("busy_percent")
    if isinstance(before_busy, (int, float)) and before_busy >= BUSY_THRESHOLD:
        add("warn", f"{name} was already {before_busy:g}% busy before the run started, so "
                    "something else was using it; the numbers may mix two jobs.")
    if readings == 0:
        add("info", "The command ended before the first reading during the run, so there's no "
                    "way to tell which card did the work. Use a longer test or a smaller "
                    "--interval.")
    elif ev["target_card_busy"] is None:
        add("info", f"{name} has no busy % reading (gpu_busy_percent not exposed), so this can't "
                    "show which card did the work.")
    elif ev["target_card_busy"]:
        peak = target["during"]["busy_percent"]["max"]
        add("ok", f"{name} was busy during the run (peak {peak:g}%).")
        if ev["busy_elsewhere"]:
            add("info", "Other cards were busy too: " + ", ".join(ev["busy_elsewhere"])
                        + ". If the test should use one card only, check its device setting.")
    elif ev["busy_elsewhere"]:
        add("warn", f"{name} stayed idle but " + ", ".join(ev["busy_elsewhere"]) + " got busy, "
                    "so the test most likely ran on the other card. Check the program's device "
                    "setting (for llama.cpp's Vulkan build, GGML_VK_VISIBLE_DEVICES) and that "
                    "card numbers match Vulkan device numbers.")
    else:
        add("warn", f"No card reached {BUSY_THRESHOLD}% busy during the run, so the test most "
                    "likely ran on the CPU, not the GPU.")

    peak_t = target["during"]["temp_c"]["max"]
    crit = (target["after"] or target["before"] or {}).get("temp_crit_c")
    if peak_t is not None and (peak_t >= gs.TEMP_WARN_C
                               or (crit is not None and peak_t >= crit - gs.CRIT_MARGIN_C)):
        add("warn", f"{name}: temperature reached {peak_t:g} °C during the run. Check airflow "
                    "before longer runs.")
    peak_v = target["during"]["vram_used_mib"]["max"]
    total_v = (target["after"] or target["before"] or {}).get("vram_total_mib")
    if peak_v is not None and total_v:
        frac = peak_v / total_v
        if frac >= gs.VRAM_WARN_FRACTION:
            add("warn", f"{name}: VRAM use reached {peak_v:g} of {total_v:g} MiB ({frac:.0%}). "
                        "This model may not fit reliably.")
        elif target["vram_added_mib"] is not None:
            add("info", f"{name}: the run added up to {target['vram_added_mib']:g} MiB of VRAM "
                        f"(peak {peak_v:g} of {total_v:g} MiB).")
    return out


# --- command line --------------------------------------------------------------------------

def _progress(i: int, now: list[dict[str, Any]], card: str) -> None:
    r = next((x for x in now if x["card"] == card), None)
    if r is None:
        print(f"reading {i}: {card} not found", file=sys.stderr)
        return
    print(f"reading {i}: {card} {gs._fmt(r['temp_c'], '°C')} {gs._fmt(r['power_w'], 'W')} "
          f"VRAM {gs._fmt(r['vram_used_mib'], '')}/{gs._fmt(r['vram_total_mib'], ' MiB')} "
          f"busy {gs._fmt(r['busy_percent'], '%')}", file=sys.stderr)


def main(argv: list[str] | None = None, *, sysfs_root: Path = gs.DEFAULT_SYSFS,
         min_interval: float = MIN_INTERVAL, min_timeout: float = MIN_TIMEOUT) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.gpu_run",
        description="Run one test command and record exit code, output hash and GPU sensor "
                    "readings while it runs. Put the command after --.")
    parser.add_argument("--card", required=True, metavar="cardN",
                        help="the card the test is meant to use, e.g. card1")
    parser.add_argument("--out", required=True, help="write the JSON report here (never overwrites)")
    parser.add_argument("--label", help="a short note saved in the report, e.g. 'tinyllama q4, gpu1'")
    parser.add_argument("--timeout", type=float, default=900.0,
                        help=f"stop the command after this many seconds (default 900, max "
                             f"{MAX_TIMEOUT:g})")
    parser.add_argument("--interval", type=float, default=1.0,
                        help=f"seconds between sensor readings ({MIN_INTERVAL:g}-{MAX_INTERVAL:g}, "
                             "default 1)")
    parser.add_argument("--env", action="append", metavar="NAME=VALUE",
                        help="extra environment variable for the command (repeatable; saved in "
                             "the report, so secrets are refused)")
    parser.add_argument("--expect-sha256", help="sha256 of the exact expected output (stdout)")
    parser.add_argument("--expect-file",
                        help="file with the expected output; compared after collapsing whitespace")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command to run")
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("give the command to run after --, e.g. -- ./llama-cli -m model.gguf ...")
    if not min_interval <= args.interval <= MAX_INTERVAL:
        parser.error(f"--interval must be between {MIN_INTERVAL:g} and {MAX_INTERVAL:g}")
    if not min_timeout <= args.timeout <= MAX_TIMEOUT:
        parser.error(f"--timeout must be between {MIN_TIMEOUT:g} and {MAX_TIMEOUT:g}")
    if os.path.exists(args.out):
        print(f"refusing to overwrite {args.out}", file=sys.stderr)
        return 2
    expect_text = None
    if args.expect_file:
        try:
            expect_text = Path(args.expect_file).read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            print(f"can't read --expect-file: {exc}", file=sys.stderr)
            return 2
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = None
    try:
        username = getpass.getuser()
    except (KeyError, OSError):
        username = None
    if username == "root":  # nothing personal to hide, and blanking "root" would garble messages
        username = None

    try:
        env = parse_env(args.env)
        report = run(command, card=args.card, sysfs_root=sysfs_root, timeout=args.timeout,
                     interval=args.interval, env=env, expect_sha256=args.expect_sha256,
                     expect_text=expect_text, label=args.label, hostname=hostname,
                     username=username,
                     on_reading=lambda i, now: _progress(i, now, args.card))
    except RunRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    try:
        with open(args.out, "x", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except FileExistsError:
        print(f"refusing to overwrite {args.out}", file=sys.stderr)
        return 2
    for f in report["findings"]:
        print(f"[{f['level']}] {f['msg']}")
    print(f"saved {args.out}")
    ev = report["evidence"]
    return 0 if ev["exit_ok"] and ev["output_matched"] is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
