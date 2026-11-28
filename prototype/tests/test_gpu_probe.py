"""`python3 -m app.gpu_probe_cli` output is pasted into the public tester form (0.5.77).

Before 0.5.77 it ran `vulkaninfo --summary` with the volunteer's full environment and printed
everything: device UUIDs, and home-folder paths from loader messages (a user-installed layer).
Now it runs like `app.hw_inventory` (allow-listed, no shell, bare environment) and replaces the
host name, user names (login and sudo caller), home-folder names and UUIDs (`probe-redact-v1`).
"""
from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

from app import gpu_probe
from app.hw_inventory import CommandResult

PROTO = Path(__file__).resolve().parents[1]

SUMMARY = """\
Devices:
========
GPU0:
\tdeviceName         = AMD Radeon RX 470 Graphics (RADV POLARIS10)
\tdeviceUUID         = 00000000-0300-0000-0000-000000000000
\tdriverUUID         = 414d442d-4d45-5341-2d44-525600000000
"""
STDERR = ("ERROR: [Loader Message] Code 0 : /home/alice/obs/libVkLayer_obs.so: cannot open\n"
          "note: built on rig-01 by bob for C:\\Users\\Jane Doe\\x\n")


def fake(stdout=SUMMARY, stderr=STDERR, available=True, returncode=0, error=None):
    calls = []

    def runner(argv):
        calls.append(argv)
        return CommandResult(argv, available, returncode, stdout, stderr, error)
    return runner, calls


def test_everything_personal_is_replaced():
    runner, calls = fake()
    report = gpu_probe.probe_vulkan(runner=runner, hostname="rig-01", usernames=["root", "bob"])
    assert calls == [["vulkaninfo", "--summary"]]
    blob = json.dumps(report)
    for secret in ("alice", "rig-01", "bob", "Jane Doe", "00000000-0300", "414d442d"):
        assert secret not in blob, secret
    assert "/home/[user]/obs/libVkLayer_obs.so" in report["stderr"]
    assert "built on [host] by [user]" in report["stderr"]
    assert "C:\\Users\\[user]\\x" in report["stderr"]
    assert "deviceUUID         = [uuid]" in report["summary"]
    assert "AMD Radeon RX 470 Graphics (RADV POLARIS10)" in report["summary"]
    assert report["available"] is True and report["returncode"] == 0
    assert report["rule"] == "probe-redact-v1" and "[uuid]" in report["privacy"]
    assert "not proof" in report["note"]


def test_missing_or_stuck_vulkaninfo_is_reported_plainly():
    runner, _ = fake(available=False, returncode=None, stdout="", stderr="", error="not installed")
    assert gpu_probe.probe_vulkan(runner=runner, hostname="h", usernames=[])["available"] is False
    runner, _ = fake(returncode=None, stdout="", stderr="", error="TimeoutExpired")
    report = gpu_probe.probe_vulkan(runner=runner, hostname="h", usernames=[])
    assert report == {"available": False, "reason": "vulkaninfo did not finish (TimeoutExpired)",
                      "privacy": gpu_probe.PRIVACY, "rule": "probe-redact-v1"}


def test_sudo_caller_is_counted_as_a_user(monkeypatch):
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(gpu_probe.getpass, "getuser", lambda: "root")
    monkeypatch.setattr(gpu_probe.socket, "gethostname", lambda: "rig-01")
    assert gpu_probe._local_names() == ("rig-01", ["root", "alice"])


def test_cli_runs_vulkaninfo_without_the_home_environment(tmp_path):
    """A real process: a fake `vulkaninfo` on PATH prints what it was given."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool = bindir / "vulkaninfo"
    tool.write_text("#!/bin/sh\necho \"HOME=$HOME XDG=$XDG_DATA_HOME args=$*\"\n"
                    "echo 'deviceUUID = 6d657361-3235-2e32-2e38-2d3075627500'\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": "/home/alice",
           "XDG_DATA_HOME": "/home/alice/.local/share", "USER": "alice", "LOGNAME": "alice"}
    done = subprocess.run([sys.executable, "-m", "app.gpu_probe_cli"], cwd=PROTO, env=env,
                          capture_output=True, text=True, timeout=60, check=True)
    report = json.loads(done.stdout)
    assert "HOME= XDG= args=--summary" in report["summary"]
    assert "6d657361" not in done.stdout and "alice" not in done.stdout


def test_hw_inventory_also_drops_home_folder_names_under_sudo():
    from app.hw_inventory import _redactor
    redact = _redactor("rig-01", "root")  # run with sudo: the login name is root
    out = redact({"stderr": "/home/alice/x.so /Users/carol C:\\Users\\Jane Doe\\y /home/[user]/z"})
    assert out == {"stderr": "/home/[user]/x.so /Users/[user] C:\\Users\\[user]\\y /home/[user]/z"}
