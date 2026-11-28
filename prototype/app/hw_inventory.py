"""Read-only hardware inventory for the GPU rig (Phase 3 preparation).

Run it on the Ubuntu machine that holds the cards:

    python -m app.hw_inventory --out inventory.json

What it does: reads /etc/os-release, runs three read-only commands (`uname -r`,
`lspci -nnk`, `vulkaninfo --summary`), lists /dev/dri, and turns the results into a
JSON report with plain-language findings. Hostname, username, home folder names and device
UUIDs are redacted before anything is printed or saved.

What it never does: use sudo, install packages, change drivers or groups, download
anything, or touch the GPUs beyond asking the system what it can see.

An inventory is not proof that inference works. It only says what the operating
system and the Vulkan loader report.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = "bnh-hw-inventory/1"
NOTE = ("This is an inventory of what the system reports. It is not proof that a GPU can run "
        "AI inference; that needs a real, reproducible run on each card.")

# The only commands this tool may run, exactly as written. No shell, no sudo.
ALLOWED_COMMANDS: set[tuple[str, ...]] = {
    ("uname", "-r"),
    ("lspci", "-nnk"),
    ("vulkaninfo", "--summary"),
}
INSTALL_HINTS = {"lspci": "pciutils", "vulkaninfo": "vulkan-tools", "uname": "coreutils"}
MAX_OUTPUT = 20_000
AMD_VENDOR_PCI = "1002"
AMD_VENDOR_VK = "0x1002"


@dataclass
class CommandResult:
    argv: list[str]
    available: bool
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None


def run_command(argv: list[str], timeout: float = 20.0) -> CommandResult:
    """Run one allowlisted, read-only command without a shell."""
    if tuple(argv) not in ALLOWED_COMMANDS:
        raise ValueError(f"command not allowed: {argv!r}")
    exe = shutil.which(argv[0])
    if not exe:
        return CommandResult(argv, False, None, "", "", "not installed")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}
    try:
        proc = subprocess.run([exe, *argv[1:]], capture_output=True, text=True,
                              timeout=timeout, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(argv, True, None, "", "", type(exc).__name__)
    return CommandResult(argv, True, proc.returncode, proc.stdout[-MAX_OUTPUT:],
                         proc.stderr[-4000:], None)


# --- parsers -------------------------------------------------------------------------------

_PCI_HEAD = re.compile(
    r"^(?P<slot>[0-9a-fA-F:.]+)\s+(?P<cls>[^\[]+?)\s*\[(?P<clsid>[0-9a-fA-F]{4})\]:\s*"
    r"(?P<name>.*?)\s*\[(?P<vid>[0-9a-fA-F]{4}):(?P<did>[0-9a-fA-F]{4})\]")
_DISPLAY_CLASS_PREFIX = "03"  # PCI base class 0x03 = display controllers (VGA, 3D, other)


def parse_lspci_nnk(text: str) -> list[dict[str, Any]]:
    """Display devices from `lspci -nnk`, with IDs and the bound kernel driver."""
    devices: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if line and not line[0].isspace():
            current = None
            m = _PCI_HEAD.match(line)
            if m and m["clsid"].startswith(_DISPLAY_CLASS_PREFIX):
                current = {
                    "slot": m["slot"], "class": m["cls"].strip(), "name": m["name"].strip(),
                    "vendor_id": m["vid"].lower(), "device_id": m["did"].lower(),
                    "is_amd": m["vid"].lower() == AMD_VENDOR_PCI,
                    "subsystem": None, "driver_in_use": None, "kernel_modules": [],
                }
                devices.append(current)
        elif current is not None:
            key, _, value = line.strip().partition(":")
            value = value.strip()
            if key == "Kernel driver in use":
                current["driver_in_use"] = value or None
            elif key == "Kernel modules":
                current["kernel_modules"] = [v.strip() for v in value.split(",") if v.strip()]
            elif key == "Subsystem":
                current["subsystem"] = value
    return devices


_VK_FIELDS = {
    "apiVersion": "api_version", "driverVersion": "driver_version", "vendorID": "vendor_id",
    "deviceID": "device_id", "deviceType": "device_type", "deviceName": "device_name",
    "driverID": "driver_id", "driverName": "driver_name", "driverInfo": "driver_info",
}
_PAREN_VERSION = re.compile(r"\((\d+\.\d+\.\d+)\)")


def parse_vulkaninfo_summary(text: str) -> dict[str, Any]:
    """Instance version and per-device fields from `vulkaninfo --summary`.

    Device UUIDs are deliberately dropped: they identify one machine and aren't needed.
    """
    instance = None
    devices: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Vulkan Instance Version:"):
            instance = line.split(":", 1)[1].strip()
        elif re.fullmatch(r"GPU\d+:", line):
            current = {"index": int(line[3:-1])}
            devices.append(current)
        elif current is not None and "=" in line:
            key, _, value = (p.strip() for p in line.partition("="))
            if key in _VK_FIELDS:
                if key in ("apiVersion", "driverVersion"):
                    m = _PAREN_VERSION.search(value)
                    value = m.group(1) if m else value
                current[_VK_FIELDS[key]] = value
    for d in devices:
        d["is_cpu"] = d.get("device_type", "").endswith("_CPU")
    return {"instance_version": instance, "devices": devices}


def parse_os_release(text: str) -> dict[str, str | None]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip('"')
    return {"pretty_name": values.get("PRETTY_NAME"), "id": values.get("ID"),
            "version_id": values.get("VERSION_ID")}


# --- redaction -----------------------------------------------------------------------------

_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{12}\b")


# A home folder names its owner even when the tool runs under another account (sudo) or the
# folder's name differs from the login name, so the folder part is replaced on its own.
_HOME_PATH = re.compile(r"(?<![\w.-])(/home/|/Users/|/var/home/)(?!\[user\])[^/\\\s\"'`:;,)\]]+")
# Windows folder names often hold a space ("C:\Users\Jane Doe"), so up to the next separator.
_WIN_HOME_PATH = re.compile(r"(?<![\w.-])([A-Za-z]:\\Users\\)(?!\[user\])[^\\/\"'\r\n]+")


def _redactor(hostname: str | None, username: str | None) -> Callable[[Any], Any]:
    rules: list[tuple[re.Pattern[str], str]] = [
        (_UUID, "[uuid]"), (_HOME_PATH, r"\1[user]"), (_WIN_HOME_PATH, r"\1[user]")]
    for value, label in ((hostname, "[host]"), (username, "[user]")):
        if value:
            rules.append((re.compile(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])"), label))

    def redact(obj: Any) -> Any:
        if isinstance(obj, str):
            for pattern, label in rules:
                obj = pattern.sub(label, obj)
            return obj
        if isinstance(obj, dict):
            return {redact(k): redact(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [redact(v) for v in obj]
        return obj

    return redact


# --- report --------------------------------------------------------------------------------

def _default_access(path: str) -> bool:
    return os.access(path, os.R_OK | os.W_OK)


def _list_dri(dri_dir: Path, access: Callable[[str], bool]) -> list[dict[str, Any]]:
    if not dri_dir.is_dir():
        return []
    nodes = []
    for p in sorted(dri_dir.iterdir()):
        if p.name.startswith(("card", "renderD")):
            nodes.append({"name": p.name, "kind": "render" if p.name.startswith("renderD")
                          else "card", "user_can_open": bool(access(str(p)))})
    return nodes


def _findings(results: dict[tuple[str, ...], CommandResult], pci: list[dict[str, Any]],
              vk: dict[str, Any], dri: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    def add(level: str, msg: str) -> None:
        out.append({"level": level, "msg": msg})

    for argv, res in results.items():
        if not res.available:
            pkg = INSTALL_HINTS.get(argv[0], "?")
            add("warn", f"`{argv[0]}` isn't installed (Ubuntu package: {pkg}). Installing it is "
                        "your call; this tool never installs anything.")
        elif res.returncode != 0:
            add("warn", f"`{' '.join(argv)}` failed (exit {res.returncode}, {res.error or 'see stderr'}).")

    lspci_ok = results[("lspci", "-nnk")].returncode == 0
    vk_ok = results[("vulkaninfo", "--summary")].returncode == 0
    amd = [g for g in pci if g["is_amd"]]
    if lspci_ok:
        if amd:
            add("info", f"lspci sees {len(amd)} AMD graphics card(s): "
                        + ", ".join(f"{g['slot']} [{g['vendor_id']}:{g['device_id']}]" for g in amd))
        else:
            add("warn", "lspci sees no AMD graphics card.")
    for g in amd:
        drv = g["driver_in_use"]
        if drv == "amdgpu":
            add("ok", f"{g['slot']}: kernel driver is amdgpu.")
        elif drv is None:
            add("warn", f"{g['slot']}: no kernel driver is bound to this card.")
        else:
            add("warn", f"{g['slot']}: kernel driver is '{drv}', not amdgpu. Vulkan on AMD (RADV) "
                        "needs amdgpu. Changing drivers is your decision; don't do it blindly.")

    if vk_ok:
        real = [d for d in vk["devices"] if not d["is_cpu"]]
        amd_vk = [d for d in real if d.get("vendor_id", "").lower() == AMD_VENDOR_VK]
        if not real:
            add("warn", "Vulkan only sees a software (CPU) renderer such as llvmpipe, not a real "
                        "GPU. Work 'on Vulkan' here would run on the CPU.")
        for d in real:
            add("info", f"Vulkan GPU{d['index']}: {d.get('device_name', '?')} via "
                        f"{d.get('driver_name', '?')} ({d.get('driver_info', '?')}), "
                        f"API {d.get('api_version', '?')}.")
        if lspci_ok and len(amd) != len(amd_vk):
            add("warn", f"lspci sees {len(amd)} AMD card(s) but Vulkan sees {len(amd_vk)}. "
                        "Each card must show up in Vulkan before it can be tested on its own.")

    renders = [n for n in dri if n["kind"] == "render"]
    if not renders:
        add("warn", "No /dev/dri/renderD* nodes found, so no GPU is available for compute.")
    for n in renders:
        if not n["user_can_open"]:
            add("warn", f"Your user can't open /dev/dri/{n['name']}. This usually means the user "
                        "isn't in the 'render' group. Changing groups is your call.")
    return out


def collect_inventory(*, runner: Callable[[list[str]], CommandResult] = run_command,
                      dri_dir: Path = Path("/dev/dri"),
                      os_release_path: Path = Path("/etc/os-release"),
                      access: Callable[[str], bool] = _default_access,
                      hostname: str | None = None, username: str | None = None) -> dict[str, Any]:
    if hostname is None:
        hostname = socket.gethostname()
    if username is None:
        try:
            username = getpass.getuser()
        except Exception:  # noqa: BLE001 - no user name available is fine
            username = None

    results = {argv: runner(list(argv)) for argv in sorted(ALLOWED_COMMANDS)}
    try:
        os_info = parse_os_release(Path(os_release_path).read_text())
    except OSError:
        os_info = {"pretty_name": None, "id": None, "version_id": None}

    uname = results[("uname", "-r")]
    pci = parse_lspci_nnk(results[("lspci", "-nnk")].stdout)
    vk = parse_vulkaninfo_summary(results[("vulkaninfo", "--summary")].stdout)
    dri = _list_dri(Path(dri_dir), access)
    amd_vk = [d for d in vk["devices"]
              if not d["is_cpu"] and d.get("vendor_id", "").lower() == AMD_VENDOR_VK]

    report = {
        "schema_version": SCHEMA_VERSION,
        "proves_inference": False,
        "note": NOTE,
        "os": os_info,
        "kernel": uname.stdout.strip() or None,
        "pci_display_devices": pci,
        "vulkan": vk,
        "dri_nodes": dri,
        "summary": {
            "amd_gpus_on_pci": sum(1 for g in pci if g["is_amd"]),
            "amd_gpus_in_vulkan": len(amd_vk),
            "render_nodes": sum(1 for n in dri if n["kind"] == "render"),
        },
        "findings": _findings(results, pci, vk, dri),
        "commands": {" ".join(argv): {"available": r.available, "returncode": r.returncode,
                                      "error": r.error, "stdout": r.stdout, "stderr": r.stderr}
                     for argv, r in results.items()},
    }
    return _redactor(hostname, username)(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.hw_inventory",
                                     description="Read-only GPU rig inventory (no sudo, no installs).")
    parser.add_argument("--out", help="write the JSON report here (refuses to overwrite)")
    args = parser.parse_args(argv)
    report = collect_inventory()
    text = json.dumps(report, indent=2, sort_keys=True)
    if not args.out:
        print(text)
        return 0
    try:
        with open(args.out, "x", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except FileExistsError:
        print(f"refusing to overwrite {args.out}", file=sys.stderr)
        return 2
    for f in report.get("findings", []):
        print(f"[{f['level']}] {f['msg']}")
    print(f"saved {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
