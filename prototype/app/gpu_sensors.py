"""Read-only GPU sensor readings for the rig (Phase 3 preparation).

Run it on the Ubuntu machine that holds the cards:

    python -m app.gpu_sensors                          # one snapshot of every card
    python -m app.gpu_sensors --card card1 --samples 120 --interval 1 --out run1-sensors.json

The second form takes 120 readings one second apart from card1 only, while you run a
test on that card in another terminal, and saves the lowest, highest and average
temperature, power draw, VRAM use, busy percentage and clock speeds.

Where the numbers come from: the files the amdgpu kernel driver already publishes under
/sys/class/drm/cardN/device (VRAM, busy %, clock tables) and its hwmon folder
(temperature, power, fan). The tool only opens those files for reading.

What it never does: run commands, use sudo, write to sysfs (no overclocking, fan or power
changes), install anything, or read serial numbers / unique ids.

Sensor readings are not proof that inference works. They tell you how hot, busy and
full a card was while something else ran on it.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = "bnh-gpu-sensors/1"
NOTE = ("These are sensor readings published by the kernel driver. They are not proof that a "
        "GPU can run AI inference; that needs a real, reproducible run on each card.")
DEFAULT_SYSFS = Path("/sys/class/drm")
AMD_VENDOR = "0x1002"
MAX_READ = 4096
MAX_SAMPLES = 3600
MIN_INTERVAL, MAX_INTERVAL = 0.5, 60.0
TEMP_WARN_C = 85.0           # warn at or above this edge temperature
CRIT_MARGIN_C = 5.0          # ...or within this many degrees of the driver's own critical limit
VRAM_WARN_FRACTION = 0.90    # warn when VRAM use reaches 90% of the card's total

_CARD_RE = re.compile(r"^card\d+$")
_DPM_RE = re.compile(r"^\s*(\d+):\s*(\d+)\s*Mhz\s*(\*)?\s*$", re.IGNORECASE)

# The numbers we summarise in watch mode, in the order they are printed.
METRICS = ("temp_c", "power_w", "vram_used_mib", "busy_percent", "sclk_mhz", "mclk_mhz", "fan_rpm")


# --- low-level readers ---------------------------------------------------------------------

def _read(path: Path) -> str | None:
    """Read a small sysfs file. Missing, unreadable or non-text files give None."""
    try:
        with open(path, "r", encoding="ascii") as fh:
            return fh.read(MAX_READ).strip()
    except (OSError, ValueError):  # ValueError covers UnicodeDecodeError (binary files)
        return None


def _read_int(path: Path) -> int | None:
    text = _read(path)
    if text is None:
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None


def _link_name(path: Path) -> str | None:
    """Basename of where a sysfs symlink points (the PCI slot, the driver name)."""
    try:
        if not path.is_symlink() and not path.exists():
            return None
        return Path(os.path.realpath(path)).name or None
    except OSError:
        return None


def parse_dpm(text: str | None) -> dict[str, Any] | None:
    """Parse an amdgpu clock table such as pp_dpm_sclk ("1: 608Mhz *"). The starred line is current."""
    if not text:
        return None
    levels, current = [], None
    for line in text.splitlines():
        m = _DPM_RE.match(line)
        if not m:
            continue
        mhz = int(m.group(2))
        levels.append(mhz)
        if m.group(3):
            current = mhz
    if not levels:
        return None
    return {"current_mhz": current, "max_mhz": max(levels), "levels": len(levels)}


def _hwmon_dir(device: Path) -> Path | None:
    base = device / "hwmon"
    try:
        dirs = sorted(p for p in base.iterdir() if p.name.startswith("hwmon"))
    except OSError:
        return None
    return dirs[0] if dirs else None


def _scale(value: int | None, divisor: float, digits: int = 1) -> float | None:
    return None if value is None else round(value / divisor, digits)


# --- one card, one moment ------------------------------------------------------------------

def read_card(card_dir: Path) -> dict[str, Any]:
    """Everything this tool reports about one card at one moment."""
    device = card_dir / "device"
    vendor = (_read(device / "vendor") or "").lower() or None
    hw = _hwmon_dir(device)
    temp_mc = _read_int(hw / "temp1_input") if hw else None
    crit_mc = _read_int(hw / "temp1_crit") if hw else None
    power_uw = None
    if hw:
        power_uw = _read_int(hw / "power1_average")
        if power_uw is None:
            power_uw = _read_int(hw / "power1_input")
    cap_uw = _read_int(hw / "power1_cap") if hw else None
    vram_total = _read_int(device / "mem_info_vram_total")
    vram_used = _read_int(device / "mem_info_vram_used")
    sclk = parse_dpm(_read(device / "pp_dpm_sclk"))
    mclk = parse_dpm(_read(device / "pp_dpm_mclk"))
    return {
        "card": card_dir.name,
        "pci_slot": _link_name(device),
        "vendor_id": vendor,
        "device_id": (_read(device / "device") or "").lower() or None,
        "subsystem_id": ":".join(x for x in ((_read(device / "subsystem_vendor") or "").lower(),
                                            (_read(device / "subsystem_device") or "").lower()) if x)
                        or None,
        "is_amd": vendor == AMD_VENDOR,
        "driver": _link_name(device / "driver"),
        "temp_label": _read(hw / "temp1_label") if hw else None,
        "temp_c": _scale(temp_mc, 1000),
        "temp_crit_c": _scale(crit_mc, 1000),
        "power_w": _scale(power_uw, 1_000_000),
        "power_cap_w": _scale(cap_uw, 1_000_000),
        "fan_rpm": _read_int(hw / "fan1_input") if hw else None,
        "vram_total_mib": _scale(vram_total, 1024 * 1024),
        "vram_used_mib": _scale(vram_used, 1024 * 1024),
        "busy_percent": _read_int(device / "gpu_busy_percent"),
        "sclk_mhz": sclk["current_mhz"] if sclk else None,
        "sclk_max_mhz": sclk["max_mhz"] if sclk else None,
        "mclk_mhz": mclk["current_mhz"] if mclk else None,
        "mclk_max_mhz": mclk["max_mhz"] if mclk else None,
    }


def list_cards(sysfs_root: Path = DEFAULT_SYSFS) -> list[Path]:
    """cardN entries only: not connectors (card0-DP-1) and not render nodes."""
    try:
        return sorted((p for p in Path(sysfs_root).iterdir() if _CARD_RE.match(p.name)),
                      key=lambda p: int(p.name[4:]))
    except OSError:
        return []


# --- summaries and findings ----------------------------------------------------------------

def summarize(samples: list[dict[str, Any]]) -> dict[str, dict[str, float | int | None]]:
    """Lowest, highest and average of each metric over the readings that had it."""
    out: dict[str, dict[str, float | int | None]] = {}
    for key in METRICS:
        vals = [s[key] for s in samples
                if isinstance(s.get(key), (int, float)) and math.isfinite(s[key])]
        out[key] = {
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
            "avg": round(sum(vals) / len(vals), 1) if vals else None,
            "readings": len(vals),
        }
    return out


def _findings(cards: list[dict[str, Any]], wanted: list[str] | None,
              sample_count: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    def add(level: str, msg: str) -> None:
        out.append({"level": level, "msg": msg})

    if sample_count == 0:
        add("warn", "Stopped before the first reading, so there is nothing to report.")
        return out
    if wanted:
        seen = {c["card"] for c in cards}
        for name in wanted:
            if name not in seen:
                add("warn", f"{name} was asked for but doesn't exist under /sys/class/drm.")
    if not cards:
        add("warn", "No GPU cards found under /sys/class/drm. Is this the rig, and is a driver loaded?")
        return out

    for c in cards:
        name = f"{c['card']} ({c['pci_slot'] or 'unknown slot'})"
        last = c["last"]
        summ = c["summary"]
        if not last["is_amd"]:
            add("info", f"{name}: not an AMD card (vendor {last['vendor_id'] or '?'}); listed for "
                        "completeness.")
            continue
        if last["driver"] != "amdgpu":
            add("warn", f"{name}: driver is '{last['driver'] or 'none'}', not amdgpu, so most "
                        "readings below are missing. Changing drivers is your decision.")
            continue
        if summ["temp_c"]["readings"] == 0:
            add("warn", f"{name}: no temperature reading (no readable hwmon temp1_input).")
        if summ["power_w"]["readings"] == 0:
            add("info", f"{name}: no power reading (power1_average/power1_input not exposed).")
        if summ["vram_used_mib"]["readings"] == 0:
            add("info", f"{name}: no VRAM reading (mem_info_vram_used not exposed).")

        peak_t = summ["temp_c"]["max"]
        crit = last["temp_crit_c"]
        if peak_t is not None:
            if peak_t >= TEMP_WARN_C or (crit is not None and peak_t >= crit - CRIT_MARGIN_C):
                limit = f", driver's critical limit {crit:g} °C" if crit is not None else ""
                add("warn", f"{name}: temperature reached {peak_t:g} °C (warning at "
                            f"{TEMP_WARN_C:g} °C{limit}). Check airflow before long runs.")
            else:
                add("ok", f"{name}: peak temperature {peak_t:g} °C.")
        peak_v, total_v = summ["vram_used_mib"]["max"], last["vram_total_mib"]
        if peak_v is not None and total_v:
            frac = peak_v / total_v
            level = "warn" if frac >= VRAM_WARN_FRACTION else "ok"
            add(level, f"{name}: peak VRAM use {peak_v:g} of {total_v:g} MiB ({frac:.0%})"
                       + (". A model this size may not fit reliably." if level == "warn" else "."))
        if sample_count > 1 and summ["busy_percent"]["readings"]:
            if (summ["busy_percent"]["max"] or 0) == 0:
                add("info", f"{name}: busy stayed at 0% for the whole watch, so nothing ran on "
                            "this card while it was being watched.")
    return out


# --- collection ----------------------------------------------------------------------------

def collect(*, sysfs_root: Path = DEFAULT_SYSFS, cards: list[str] | None = None,
            samples: int = 1, interval: float = 1.0,
            sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.time,
            on_sample: Callable[[int, list[dict[str, Any]]], None] | None = None) -> dict[str, Any]:
    """Take `samples` readings `interval` seconds apart and return a JSON-ready report."""
    if not 1 <= samples <= MAX_SAMPLES:
        raise ValueError(f"samples must be between 1 and {MAX_SAMPLES}")
    if samples > 1 and not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise ValueError(f"interval must be between {MIN_INTERVAL:g} and {MAX_INTERVAL:g} seconds")
    paths = list_cards(sysfs_root)
    if cards:
        paths = [p for p in paths if p.name in set(cards)]

    readings: dict[str, list[dict[str, Any]]] = {p.name: [] for p in paths}
    started = clock()
    stopped_early = False
    taken = 0
    try:
        for i in range(samples):
            if i:
                sleep(interval)
            now = [read_card(p) for p in paths]
            for r in now:
                readings[r["card"]].append(r)
            taken += 1
            if on_sample:
                on_sample(i + 1, now)
    except KeyboardInterrupt:
        stopped_early = True
    finished = clock()

    card_reports = []
    for p in paths:
        rs = readings[p.name]
        if not rs:
            continue
        card_reports.append({"card": p.name, "pci_slot": rs[-1]["pci_slot"], "last": rs[-1],
                             "summary": summarize(rs)})
    return {
        "schema_version": SCHEMA_VERSION,
        "proves_inference": False,
        "note": NOTE,
        "watch": {"samples_requested": samples, "samples_taken": taken,
                  "interval_seconds": interval if samples > 1 else None,
                  "duration_seconds": round(max(0.0, finished - started), 1),
                  "stopped_early": stopped_early},
        "cards_requested": list(cards) if cards else None,
        "cards": card_reports,
        "findings": _findings(card_reports, list(cards) if cards else None, taken),
    }


def _fmt(v: Any, unit: str) -> str:
    return "?" if v is None else f"{v:g}{unit}"


def _progress(i: int, now: list[dict[str, Any]], total: int) -> None:
    parts = [f"{r['card']} {_fmt(r['temp_c'], '°C')} {_fmt(r['power_w'], 'W')} "
             f"VRAM {_fmt(r['vram_used_mib'], '')}/{_fmt(r['vram_total_mib'], ' MiB')} "
             f"busy {_fmt(r['busy_percent'], '%')}" for r in now]
    print(f"sample {i}/{total}: " + ("; ".join(parts) or "no cards"), file=sys.stderr)


def main(argv: list[str] | None = None, *, sysfs_root: Path = DEFAULT_SYSFS,
         sleep: Callable[[float], None] = time.sleep) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.gpu_sensors",
        description="Read-only GPU temperature, power, VRAM and clock readings (amdgpu sysfs).")
    parser.add_argument("--card", action="append", metavar="cardN",
                        help="only this card (repeatable), e.g. --card card1")
    parser.add_argument("--samples", type=int, default=1,
                        help=f"number of readings (1-{MAX_SAMPLES}, default 1)")
    parser.add_argument("--interval", type=float, default=1.0,
                        help=f"seconds between readings ({MIN_INTERVAL:g}-{MAX_INTERVAL:g}, default 1)")
    parser.add_argument("--out", help="write the JSON report here (refuses to overwrite)")
    args = parser.parse_args(argv)
    for name in args.card or []:
        if not _CARD_RE.match(name):
            parser.error(f"--card must look like card0, card1, ... (got {name!r})")
    if not 1 <= args.samples <= MAX_SAMPLES:
        parser.error(f"--samples must be between 1 and {MAX_SAMPLES}")
    if args.samples > 1 and not MIN_INTERVAL <= args.interval <= MAX_INTERVAL:
        parser.error(f"--interval must be between {MIN_INTERVAL:g} and {MAX_INTERVAL:g}")
    if args.out and os.path.exists(args.out):
        print(f"refusing to overwrite {args.out}", file=sys.stderr)
        return 2

    progress = (lambda i, now: _progress(i, now, args.samples)) if args.samples > 1 else None
    report = collect(sysfs_root=sysfs_root, cards=args.card, samples=args.samples,
                     interval=args.interval, sleep=sleep, on_sample=progress)
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    if not args.out:
        print(text)
        return 0
    try:
        with open(args.out, "x", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except FileExistsError:
        print(f"refusing to overwrite {args.out}", file=sys.stderr)
        return 2
    for f in report["findings"]:
        print(f"[{f['level']}] {f['msg']}")
    print(f"saved {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
