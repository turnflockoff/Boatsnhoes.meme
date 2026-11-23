"""Phase 3 evidence check: which hardware-validation items do the rig's reports back up?

After running the read-only tools on the GPU machine:

    python -m app.hw_inventory --out inventory.json
    python -m app.gpu_run --card card0 --expect-file expected.txt --out run-card0.json -- <test>
    python -m app.gpu_run --card card1 --expect-file expected.txt --out run-card1.json -- <test>

run this anywhere (it only reads the JSON files):

    python -m app.phase3_check --inventory inventory.json run-card0.json run-card1.json

It walks the ten Track B items in docs/ROADMAP.md and gives each one a status:

    evidenced  the reports contain what the item asks for
    partial    some of it is there; the note says what's missing
    missing    nothing in the reports covers it yet
    problem    the reports contradict the item (wrong driver, CPU-only Vulkan, ...)

It also cross-checks the files against each other: a run only counts if it exited cleanly,
matched a known-good answer, kept the card it was aimed at busy (and no other card), and that
card's PCI slot is one the inventory saw. "Repeat on GPU 1" needs the SAME test (same program,
same files, same expected answer) passing on a second, different card.

What it can't do: the reports aren't signed, so this catches mistakes and mismatches, not a
determined forger. It never upgrades anything to proof of inference (`proves_inference` stays
false). Evidence gets strong when someone else repeats the same run and gets the same answer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "bnh-phase3-check/1"
INVENTORY_SCHEMA = "bnh-hw-inventory/1"
RUN_SCHEMA = "bnh-gpu-run/1"
RX470_PCI_DEVICE = "67df"  # Polaris 10 (Ellesmere): RX 470, and also RX 480/570/580
NOTE = ("This check reads unsigned report files. It catches mistakes and mismatches, not a "
        "determined forger, and it is not proof that a GPU ran AI inference.")

ITEMS: list[tuple[str, str]] = [
    ("os", "Record Ubuntu release"),
    ("kernel", "Record kernel"),
    ("pci_drivers", "Capture PCI IDs and bound drivers"),
    ("vulkan", "Capture Vulkan devices"),
    ("dri", "Confirm /dev/dri permissions"),
    ("two_cards", "Confirm both RX 470 cards enumerate independently"),
    ("smoke_first", "Run a minimal Vulkan compute smoke test on GPU 0"),
    ("smoke_second", "Repeat on GPU 1"),
    ("sensors", "Record temperature, power, VRAM use, throughput, errors, and stability"),
    ("versions", "Document exact working driver/runtime versions"),
]
# Items whose "evidenced" status lets the check exit 0. The last two are never more than
# partial here: stability and the runtime's own version need a person to judge or write down.
REQUIRED_FOR_PASS = {"os", "kernel", "pci_drivers", "vulkan", "dri", "two_cards",
                     "smoke_first", "smoke_second"}
STATUS_MARK = {"evidenced": "[x]", "partial": "[~]", "missing": "[ ]", "problem": "[!]"}


class InputError(ValueError):
    """A file couldn't be read or isn't the kind of report it should be."""


def normalize_slot(slot: str | None) -> str | None:
    """'0000:01:00.0' (sysfs) and '01:00.0' (lspci) name the same slot."""
    if not slot:
        return None
    s = slot.strip().lower()
    parts = s.split(":")
    if len(parts) == 3 and parts[0] == "0000":
        s = ":".join(parts[1:])
    return s


def load_report(path: Path, schema: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InputError(f"can't read {path.name}: {exc.strerror or exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{path.name} is not valid JSON ({exc})") from exc
    if not isinstance(data, dict) or data.get("schema_version") != schema:
        got = data.get("schema_version") if isinstance(data, dict) else None
        raise InputError(f"{path.name} is not a {schema} report (schema_version: {got!r})")
    return data, hashlib.sha256(raw).hexdigest()


def _cmd_ok(inv: dict[str, Any], command: str) -> bool:
    c = (inv.get("commands") or {}).get(command) or {}
    return bool(c.get("available")) and c.get("returncode") == 0


def _amd_pci(inv: dict[str, Any]) -> list[dict[str, Any]]:
    return [g for g in inv.get("pci_display_devices") or [] if g.get("is_amd")]


def _amd_vulkan(inv: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in (inv.get("vulkan") or {}).get("devices") or []
            if not d.get("is_cpu") and str(d.get("vendor_id", "")).lower() == "0x1002"]


def run_fingerprint(run: dict[str, Any]) -> str:
    """Same program, same argument files, same expected answer = the same test."""
    cmd = run.get("command") or {}
    files = sorted(str(f.get("sha256")) for f in cmd.get("files") or [])
    parts = [str(cmd.get("program_sha256")), *files,
             str((run.get("expected") or {}).get("expected_sha256"))]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def assess_run(run: dict[str, Any], inventory_slots: set[str]) -> dict[str, Any]:
    """Does this one run count as a passing smoke test? Every reason it doesn't is listed."""
    reasons: list[str] = []
    problems: list[str] = []
    ev = run.get("evidence") or {}
    rn = run.get("run") or {}
    target_name = run.get("target_card")
    cards = (run.get("sensors") or {}).get("cards") or []
    target = next((c for c in cards if c.get("card") == target_name), None)
    slot = normalize_slot(target.get("pci_slot")) if target else None

    if run.get("proves_inference") is not False:
        problems.append("says proves_inference is not false, which gpu_run never writes; the "
                        "file may have been edited")
    if not ev.get("exit_ok"):
        if rn.get("timed_out"):
            reasons.append("timed out")
        elif rn.get("stopped_early"):
            reasons.append("was stopped early")
        else:
            reasons.append(f"exited with code {rn.get('exit_code')}")
    matched = (run.get("expected") or {}).get("matched")
    if (run.get("expected") or {}).get("method") is None:
        reasons.append("had no expected answer, so nobody checked the output")
    elif matched is False:
        reasons.append("output did NOT match the expected answer")
    elif matched is not True:
        reasons.append("output wasn't compared")
    if target is None:
        reasons.append(f"has no sensor readings for {target_name}")
    else:
        if not target.get("is_amd"):
            reasons.append(f"{target_name} is not an AMD card")
        elif target.get("driver") != "amdgpu":
            reasons.append(f"{target_name} driver is {target.get('driver')!r}, not amdgpu")
        if ev.get("target_card_busy") is None:
            reasons.append(f"can't tell whether {target_name} did the work (no busy readings)")
        elif not ev.get("target_card_busy"):
            reasons.append(f"{target_name} never got busy (the test likely ran somewhere else)")
        if slot is None:
            reasons.append(f"{target_name} has no PCI slot recorded")
        elif slot not in inventory_slots:
            problems.append(f"{target_name} is in slot {slot}, which the inventory doesn't list "
                            "as an AMD card (another machine, or cards moved: re-run the "
                            "inventory)")
    elsewhere = ev.get("busy_elsewhere") or []
    if elsewhere:
        reasons.append("other card(s) got busy too: " + ", ".join(elsewhere))
    notes = []
    if rn.get("ran_as_root"):
        notes.append("ran as root (not needed; use your normal user)")

    during = (target or {}).get("during") or {}
    peaks = {k: (during.get(k) or {}).get("max") for k in
             ("temp_c", "power_w", "vram_used_mib", "busy_percent")}
    sensor_counts = {k: (during.get(k) or {}).get("readings", 0) for k in
                     ("temp_c", "power_w", "vram_used_mib")}
    return {
        "label": run.get("label"),
        "target_card": target_name,
        "pci_slot": slot,
        "test_fingerprint": run_fingerprint(run),
        "passes": not reasons and not problems,
        "reasons": reasons,
        "problems": problems,
        "notes": notes,
        "duration_seconds": rn.get("duration_seconds"),
        "started_at": rn.get("started_at"),
        "program_sha256": (run.get("command") or {}).get("program_sha256"),
        "peaks": peaks,
        "vram_added_mib": (target or {}).get("vram_added_mib"),
        "has_core_sensors": all(n > 0 for n in sensor_counts.values()),
    }


def _item(status: str, note: str) -> dict[str, str]:
    return {"status": status, "note": note}


def _model_note(amd: list[dict[str, Any]], vk: list[dict[str, Any]]) -> tuple[bool, str]:
    not_polaris = [g["slot"] for g in amd if g.get("device_id") != RX470_PCI_DEVICE]
    if not_polaris:
        return False, (f"slot(s) {', '.join(not_polaris)} are not PCI device 1002:{RX470_PCI_DEVICE}, "
                       "so they are not RX 470s")
    says_470 = sum(1 for g in amd if "470" in (g.get("subsystem") or ""))
    vk_470 = sum(1 for d in vk if "470" in (d.get("device_name") or ""))
    if says_470 >= len(amd) or vk_470 >= len(amd):
        return True, "the card names say RX 470"
    return True, (f"PCI device 1002:{RX470_PCI_DEVICE} is the Polaris 10 chip, shared by RX "
                  "470/480/570/580; the names don't say which, so check the sticker")


def check(inventory: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    items: dict[str, dict[str, str]] = {}
    amd = _amd_pci(inventory)
    vk_all = (inventory.get("vulkan") or {}).get("devices") or []
    vk = _amd_vulkan(inventory)
    slots = {normalize_slot(g.get("slot")) for g in amd} - {None}

    os_info = inventory.get("os") or {}
    if os_info.get("pretty_name"):
        note = f"{os_info['pretty_name']}"
        if (os_info.get("id") or "").lower() != "ubuntu":
            note += " (not Ubuntu; the plan assumes Ubuntu)"
        items["os"] = _item("evidenced", note)
    else:
        items["os"] = _item("missing", "the inventory has no OS release (/etc/os-release unreadable)")

    kernel = inventory.get("kernel")
    items["kernel"] = (_item("evidenced", f"kernel {kernel}") if kernel
                       else _item("missing", "`uname -r` gave nothing"))

    if not _cmd_ok(inventory, "lspci -nnk"):
        items["pci_drivers"] = _item("missing", "`lspci -nnk` didn't run (package pciutils)")
    elif not amd:
        items["pci_drivers"] = _item("problem", "lspci ran but saw no AMD graphics card")
    else:
        wrong = [f"{g['slot']} ({g.get('driver_in_use') or 'no driver'})" for g in amd
                 if g.get("driver_in_use") != "amdgpu"]
        listed = ", ".join(f"{g['slot']} [1002:{g.get('device_id')}]" for g in amd)
        if wrong:
            items["pci_drivers"] = _item("problem", f"{listed}; not on amdgpu: {', '.join(wrong)}")
        else:
            items["pci_drivers"] = _item("evidenced", f"{listed}, all on amdgpu")

    if not _cmd_ok(inventory, "vulkaninfo --summary"):
        items["vulkan"] = _item("missing", "`vulkaninfo --summary` didn't run (package vulkan-tools)")
    elif not vk_all:
        items["vulkan"] = _item("missing", "vulkaninfo ran but listed no devices")
    elif not vk:
        items["vulkan"] = _item("problem", "Vulkan sees no AMD GPU (only a CPU/software renderer "
                                           "or another vendor)")
    else:
        names = "; ".join(f"GPU{d.get('index')}: {d.get('device_name', '?')} "
                          f"({d.get('driver_info') or d.get('driver_name') or '?'})" for d in vk)
        items["vulkan"] = _item("evidenced", names)

    renders = [n for n in inventory.get("dri_nodes") or [] if n.get("kind") == "render"]
    blocked = [n["name"] for n in renders if not n.get("user_can_open")]
    if not renders:
        items["dri"] = _item("missing", "no /dev/dri/renderD* nodes in the inventory")
    elif blocked:
        items["dri"] = _item("problem", f"your user can't open {', '.join(blocked)} (usually: not "
                                        "in the 'render' group; changing groups is your call)")
    else:
        items["dri"] = _item("evidenced", f"your user can open {len(renders)} render node(s)")

    counts = (len(amd), len(vk), len(renders))
    if min(counts) >= 2:
        is_polaris, model = _model_note(amd, vk)
        items["two_cards"] = (_item("evidenced", f"2+ AMD cards on PCI, in Vulkan and as render "
                                                 f"nodes; {model}") if is_polaris
                              else _item("partial", f"two cards enumerate, but {model}"))
    elif max(counts) >= 2:
        items["two_cards"] = _item("problem", f"counts disagree: {counts[0]} AMD on PCI, {counts[1]} "
                                              f"in Vulkan, {counts[2]} render node(s); each card "
                                              "must show up everywhere")
    else:
        items["two_cards"] = _item("missing", "only 1 AMD card found" if max(counts) == 1
                                   else "no AMD card found")

    assessed = [assess_run(r, slots) for r in runs]
    passing = [a for a in assessed if a["passes"]]
    by_slot: dict[str, list[dict[str, Any]]] = {}
    for a in passing:
        by_slot.setdefault(a["pci_slot"], []).append(a)
    pass_slots = sorted(by_slot)

    if not passing:
        why = ("no gpu_run reports given" if not runs
               else "no run passed (see `runs` for the reasons)")
        items["smoke_first"] = _item("missing", why)
        items["smoke_second"] = _item("missing", "needs a passing run on a first card first")
    else:
        first = pass_slots[0]
        items["smoke_first"] = _item("evidenced", f"passing run on slot {first} "
                                                  f"({by_slot[first][0]['target_card']})")
        if len(pass_slots) < 2:
            others = sorted(slots - {first})
            items["smoke_second"] = _item("missing", "no passing run on a second card yet"
                                          + (f" (slot {', '.join(others)})" if others else ""))
        else:
            pairs = [(s1, s2) for i, s1 in enumerate(pass_slots) for s2 in pass_slots[i + 1:]
                     if {a["test_fingerprint"] for a in by_slot[s1]}
                     & {a["test_fingerprint"] for a in by_slot[s2]}]
            if pairs:
                s1, s2 = pairs[0]
                items["smoke_second"] = _item("evidenced", f"the same test passed on slot {s1} "
                                                           f"and slot {s2}")
            else:
                items["smoke_second"] = _item("partial", "two cards passed, but with different "
                                                         "tests (program, files or expected answer "
                                                         "differ); run the same test on both")

    if not passing:
        items["sensors"] = _item("missing", "needs at least one passing run")
    elif not all(a["has_core_sensors"] for a in passing):
        items["sensors"] = _item("partial", "some passing runs lack temperature, power or VRAM "
                                            "readings; stability isn't judged here")
    else:
        parts = []
        for s in pass_slots:
            rs = by_slot[s]
            mins = sum(a["duration_seconds"] or 0 for a in rs) / 60
            temp = max((a["peaks"]["temp_c"] or 0) for a in rs)
            power = max((a["peaks"]["power_w"] or 0) for a in rs)
            parts.append(f"slot {s}: {len(rs)} run(s), {mins:.1f} min, peak {temp:g} °C, "
                         f"{power:g} W")
        items["sensors"] = _item("partial", "; ".join(parts) + ". Temperature, power and VRAM "
                                 "are recorded; throughput comes from the test's own output, and "
                                 "stability needs longer or repeated runs, judged by you")

    drivers = sorted({d.get("driver_info") for d in vk if d.get("driver_info")})
    if not drivers or not kernel:
        items["versions"] = _item("missing", "the inventory lacks the Vulkan driver version or "
                                             "the kernel")
    else:
        progs = sorted({a["program_sha256"][:12] for a in passing if a["program_sha256"]})
        tail = (f"; test program fingerprint(s) {', '.join(progs)}: also write down its "
                "version or build (for llama.cpp, the commit)" if progs
                else "; no passing run yet, so nothing is shown working")
        items["versions"] = _item("partial", f"kernel {kernel}, {', '.join(drivers)}{tail}")

    out_items = [{"id": key, "roadmap_item": title, **items[key]} for key, title in ITEMS]
    evidenced = [i["id"] for i in out_items if i["status"] == "evidenced"]
    return {
        "schema_version": SCHEMA_VERSION,
        "proves_inference": False,
        "note": NOTE,
        "items": out_items,
        "runs": assessed,
        "evidenced": evidenced,
        "all_required_evidenced": REQUIRED_FOR_PASS <= set(evidenced),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.phase3_check",
        description="Check the rig's inventory and gpu_run reports against the Phase 3 "
                    "checklist. Reads files only.")
    parser.add_argument("--inventory", required=True, help="inventory.json from app.hw_inventory")
    parser.add_argument("runs", nargs="*", help="report files from app.gpu_run")
    parser.add_argument("--out", help="also save the JSON result here (refuses to overwrite)")
    args = parser.parse_args(argv)

    try:
        inventory, inv_sha = load_report(Path(args.inventory), INVENTORY_SCHEMA)
        runs, inputs, seen = [], [{"file": Path(args.inventory).name, "sha256": inv_sha}], {inv_sha}
        for p in args.runs:
            data, sha = load_report(Path(p), RUN_SCHEMA)
            if sha in seen:
                print(f"note: {Path(p).name} is a copy of a file already given; counted once",
                      file=sys.stderr)
                continue
            seen.add(sha)
            runs.append(data)
            inputs.append({"file": Path(p).name, "sha256": sha})
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result = check(inventory, runs)
    result["inputs"] = inputs
    if args.out:
        try:
            with open(args.out, "x", encoding="utf-8") as fh:
                fh.write(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        except FileExistsError:
            print(f"refusing to overwrite {args.out}", file=sys.stderr)
            return 2

    for i in result["items"]:
        print(f"{STATUS_MARK[i['status']]} {i['roadmap_item']}: {i['note']}")
    for r, inp in zip(result["runs"], inputs[1:]):
        verdict = "passes" if r["passes"] else "doesn't count: " + "; ".join(
            r["problems"] + r["reasons"])
        print(f"  run {inp['file']} ({r['target_card']}): {verdict}")
    print("Not proof of inference: these files are unsigned; a repeat by someone else is what "
          "makes them evidence.")
    if args.out:
        print(f"saved {args.out}")
    return 0 if result["all_required_evidenced"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
