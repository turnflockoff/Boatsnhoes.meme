"""Quick Vulkan probe for volunteers (`python3 -m app.gpu_probe_cli`), linked from /contribute.

People paste this output into the public tester form, so it must not carry who they are.
Before 0.5.77 it ran `vulkaninfo --summary` with the volunteer's whole environment and printed
its output as-is: the device UUIDs, and any home-folder path the Vulkan loader complained about
(a user-installed layer such as an OBS capture layer prints `/home/<name>/...`). Now it runs the
command the same way `app.hw_inventory` does (no shell, allow-listed, a bare environment, so
layers from the home folder aren't loaded) and replaces the host name, user name, home-folder
names and UUIDs before printing (`probe-redact-v1`).
"""
from __future__ import annotations

import getpass
import os
import socket
from typing import Any, Callable

from .hw_inventory import CommandResult, _redactor, run_command

RULE = "probe-redact-v1"
NOTE = "Device enumeration is not proof of successful AI inference."
PRIVACY = ("Host name, user name, home-folder names and device UUIDs are replaced with "
           "[host], [user] and [uuid] before printing.")


def _local_names() -> tuple[str | None, list[str]]:
    """The host name and every user name this run could show (login, sudo caller)."""
    try:
        host: str | None = socket.gethostname() or None
    except OSError:
        host = None
    users: list[str] = []
    try:
        users.append(getpass.getuser())
    except Exception:  # noqa: BLE001 - no user name available is fine
        pass
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        users.append(sudo_user)
    return host, [u for u in dict.fromkeys(users) if u]


def probe_vulkan(*, runner: Callable[[list[str]], CommandResult] = run_command,
                 hostname: str | None = None,
                 usernames: list[str] | None = None) -> dict[str, Any]:
    if hostname is None and usernames is None:
        hostname, usernames = _local_names()
    result = runner(["vulkaninfo", "--summary"])
    if not result.available:
        report: dict[str, Any] = {
            "available": False,
            "reason": "vulkaninfo is not installed; install inspection tools first"}
    elif result.returncode is None:
        report = {"available": False, "reason": f"vulkaninfo did not finish ({result.error})"}
    else:
        report = {
            "available": result.returncode == 0,
            "returncode": result.returncode,
            "summary": result.stdout[-12000:],
            "stderr": result.stderr[-4000:],
            "note": NOTE,
        }
    report["privacy"] = PRIVACY
    report["rule"] = RULE
    redact = _redactor(hostname, None)
    for name in usernames or []:
        redact = _chain(redact, _redactor(None, name))
    return redact(report)


def _chain(first: Callable[[Any], Any], second: Callable[[Any], Any]) -> Callable[[Any], Any]:
    return lambda obj: second(first(obj))
