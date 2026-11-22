"""Tests for the read-only hardware inventory (Phase 3 prep).

The command outputs below are FORMAT SAMPLES written to match what `lspci -nnk` and
`vulkaninfo --summary` print. They are not output from the owner's rig, and passing
these tests says nothing about whether an RX 470 works.
"""
from __future__ import annotations

import json

import pytest

from app import hw_inventory as hw

LSPCI_TWO_AMD = """\
00:02.0 Display controller [0380]: Intel Corporation HD Graphics 530 [8086:1912] (rev 06)
\tSubsystem: Dell HD Graphics 530 [1028:06b9]
\tKernel driver in use: i915
\tKernel modules: i915
00:1f.3 Audio device [0403]: Intel Corporation 100 Series HD Audio [8086:a170] (rev 31)
\tKernel driver in use: snd_hda_intel
01:00.0 VGA compatible controller [0300]: Advanced Micro Devices, Inc. [AMD/ATI] Ellesmere [Radeon RX 470/480/570/570X/580/580X/590] [1002:67df] (rev cf)
\tSubsystem: Sapphire Technology Limited Radeon RX 470 [1da2:e366]
\tKernel driver in use: amdgpu
\tKernel modules: amdgpu
01:00.1 Audio device [0403]: Advanced Micro Devices, Inc. [AMD/ATI] Ellesmere HDMI Audio [1002:aaf0]
\tKernel driver in use: snd_hda_intel
02:00.0 VGA compatible controller [0300]: Advanced Micro Devices, Inc. [AMD/ATI] Ellesmere [Radeon RX 470/480/570/570X/580/580X/590] [1002:67df] (rev cf)
\tSubsystem: Sapphire Technology Limited Radeon RX 470 [1da2:e366]
\tKernel driver in use: amdgpu
\tKernel modules: radeon, amdgpu
"""

VULKAN_TWO_AMD = """\
==========
VULKANINFO
==========

Vulkan Instance Version: 1.3.275


Devices:
========
GPU0:
\tapiVersion         = 1.3.274
\tdriverVersion      = 24.0.9
\tvendorID           = 0x1002
\tdeviceID           = 0x67df
\tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName         = AMD Radeon RX 470 Graphics (RADV POLARIS10)
\tdriverID           = DRIVER_ID_MESA_RADV
\tdriverName         = radv
\tdriverInfo         = Mesa 24.0.9-0ubuntu0.1
\tconformanceVersion = 1.3.0.0
\tdeviceUUID         = 00000000-0100-0000-0000-000000000000
\tdriverUUID         = 414d442d-4d45-5341-2d44-525600000000
GPU1:
\tapiVersion         = 4206847 (1.3.255)
\tvendorID           = 0x1002
\tdeviceID           = 0x67df
\tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName         = AMD Radeon RX 470 Graphics (RADV POLARIS10)
\tdriverName         = radv
\tdriverInfo         = Mesa 24.0.9-0ubuntu0.1
GPU2:
\tapiVersion         = 1.3.274
\tvendorID           = 0x10005
\tdeviceID           = 0x0000
\tdeviceType         = PHYSICAL_DEVICE_TYPE_CPU
\tdeviceName         = llvmpipe (LLVM 17.0.6, 256 bits)
\tdriverName         = llvmpipe
\tdriverInfo         = Mesa 24.0.9-0ubuntu0.1 (LLVM 17.0.6)
"""

VULKAN_CPU_ONLY = """\
Vulkan Instance Version: 1.3.275

Devices:
========
GPU0:
\tapiVersion         = 1.3.274
\tvendorID           = 0x10005
\tdeviceType         = PHYSICAL_DEVICE_TYPE_CPU
\tdeviceName         = llvmpipe (LLVM 17.0.6, 256 bits)
\tdriverName         = llvmpipe
"""

OS_RELEASE = 'NAME="Ubuntu"\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04.1 LTS"\nID=ubuntu\n'


def fake_runner(outputs: dict[str, str], missing: tuple[str, ...] = ()):
    calls: list[list[str]] = []

    def run(argv: list[str]) -> hw.CommandResult:
        calls.append(list(argv))
        if argv[0] in missing:
            return hw.CommandResult(argv=argv, available=False, returncode=None,
                                    stdout="", stderr="", error="not installed")
        return hw.CommandResult(argv=argv, available=True, returncode=0,
                                stdout=outputs.get(argv[0], ""), stderr="", error=None)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def make_dri(tmp_path, names=("card0", "card1", "renderD128", "renderD129")):
    dri = tmp_path / "dri"
    dri.mkdir()
    for n in names:
        (dri / n).write_text("")
    return dri


def collect(tmp_path, outputs=None, missing=(), access=lambda p: True, **kw):
    osr = tmp_path / "os-release"
    osr.write_text(OS_RELEASE)
    runner = fake_runner(outputs if outputs is not None else
                         {"uname": "6.8.0-45-generic\n", "lspci": LSPCI_TWO_AMD,
                          "vulkaninfo": VULKAN_TWO_AMD}, missing)
    report = hw.collect_inventory(runner=runner, dri_dir=make_dri(tmp_path),
                                  os_release_path=osr, access=access, **kw)
    return report, runner


def levels(report, level):
    return [f["msg"] for f in report["findings"] if f["level"] == level]


# --- parsers -------------------------------------------------------------------------------

def test_lspci_parser_keeps_only_display_devices_with_ids_and_driver():
    gpus = hw.parse_lspci_nnk(LSPCI_TWO_AMD)
    assert [g["slot"] for g in gpus] == ["00:02.0", "01:00.0", "02:00.0"]
    amd = gpus[1]
    assert amd["vendor_id"] == "1002" and amd["device_id"] == "67df"
    assert amd["is_amd"] is True and gpus[0]["is_amd"] is False
    assert amd["driver_in_use"] == "amdgpu"
    assert gpus[2]["kernel_modules"] == ["radeon", "amdgpu"]
    assert "Ellesmere" in amd["name"]


def test_lspci_parser_reports_missing_driver_as_none():
    text = ("03:00.0 VGA compatible controller [0300]: Advanced Micro Devices, Inc. [AMD/ATI] "
            "Ellesmere [1002:67df]\n\tKernel modules: amdgpu\n")
    (gpu,) = hw.parse_lspci_nnk(text)
    assert gpu["driver_in_use"] is None


def test_vulkan_parser_reads_instance_version_and_each_device():
    info = hw.parse_vulkaninfo_summary(VULKAN_TWO_AMD)
    assert info["instance_version"] == "1.3.275"
    assert len(info["devices"]) == 3
    gpu0, gpu1, cpu = info["devices"]
    assert gpu0["driver_name"] == "radv" and gpu0["driver_info"].startswith("Mesa 24.0.9")
    assert gpu1["api_version"] == "1.3.255"  # older numeric "4206847 (1.3.255)" form
    assert cpu["is_cpu"] is True and gpu0["is_cpu"] is False
    assert gpu0["vendor_id"] == "0x1002"
    # UUIDs identify a specific machine and aren't needed: they are not kept.
    assert all("uuid" not in k.lower() for d in info["devices"] for k in d)


def test_os_release_parser():
    assert hw.parse_os_release(OS_RELEASE)["pretty_name"] == "Ubuntu 24.04.1 LTS"


# --- findings ------------------------------------------------------------------------------

def test_two_amd_cards_on_amdgpu_and_radv_give_no_warnings(tmp_path):
    report, _ = collect(tmp_path)
    assert levels(report, "warn") == []
    assert report["summary"]["amd_gpus_on_pci"] == 2
    assert report["summary"]["amd_gpus_in_vulkan"] == 2


def test_cpu_only_vulkan_is_flagged_as_software_renderer(tmp_path):
    report, _ = collect(tmp_path, outputs={"uname": "6.8.0\n", "lspci": LSPCI_TWO_AMD,
                                           "vulkaninfo": VULKAN_CPU_ONLY})
    warns = " ".join(levels(report, "warn"))
    assert "software" in warns.lower() and "cpu" in warns.lower()


def test_old_radeon_driver_is_flagged(tmp_path):
    lspci = LSPCI_TWO_AMD.replace("Kernel driver in use: amdgpu\n\tKernel modules: radeon, amdgpu",
                                  "Kernel driver in use: radeon\n\tKernel modules: radeon, amdgpu")
    report, _ = collect(tmp_path, outputs={"uname": "6.8.0\n", "lspci": lspci,
                                           "vulkaninfo": VULKAN_TWO_AMD})
    warns = levels(report, "warn")
    assert any("02:00.0" in w and "radeon" in w for w in warns)


def test_card_on_pci_but_missing_from_vulkan_is_flagged(tmp_path):
    one = VULKAN_TWO_AMD.split("GPU1:")[0] + "GPU2:" + VULKAN_TWO_AMD.split("GPU2:")[1]
    report, _ = collect(tmp_path, outputs={"uname": "6.8.0\n", "lspci": LSPCI_TWO_AMD,
                                           "vulkaninfo": one})
    assert any("2" in w and "1" in w and "Vulkan" in w for w in levels(report, "warn"))


def test_render_node_without_access_is_flagged(tmp_path):
    report, _ = collect(tmp_path, access=lambda p: not p.endswith("renderD129"))
    nodes = {n["name"]: n for n in report["dri_nodes"]}
    assert nodes["renderD128"]["user_can_open"] is True
    assert nodes["renderD129"]["user_can_open"] is False
    assert any("renderD129" in w for w in levels(report, "warn"))


def test_missing_tools_are_recorded_with_a_hint_and_nothing_is_installed(tmp_path):
    report, runner = collect(tmp_path, missing=("lspci", "vulkaninfo"))
    warns = " ".join(levels(report, "warn"))
    assert "pciutils" in warns and "vulkan-tools" in warns
    assert report["commands"]["vulkaninfo --summary"]["available"] is False
    for argv in runner.calls:
        assert argv[0] not in {"apt", "apt-get", "sudo", "pip"}


# --- safety --------------------------------------------------------------------------------

def test_only_allowlisted_read_only_commands_run(tmp_path):
    _, runner = collect(tmp_path)
    assert {tuple(c) for c in runner.calls} <= set(hw.ALLOWED_COMMANDS)
    with pytest.raises(ValueError):
        hw.run_command(["sudo", "lspci"])
    with pytest.raises(ValueError):
        hw.run_command(["lspci", "-nnk", ";", "rm", "-rf", "/"])


def test_hostname_username_and_uuids_are_redacted_everywhere(tmp_path):
    secret_vk = VULKAN_TWO_AMD.replace("Mesa 24.0.9-0ubuntu0.1\n",
                                       "Mesa 24.0.9 built on rigbox by alice /home/alice\n", 1)
    report, _ = collect(tmp_path, outputs={"uname": "6.8.0\n", "lspci": LSPCI_TWO_AMD,
                                           "vulkaninfo": secret_vk},
                        hostname="rigbox", username="alice")
    blob = json.dumps(report)
    assert "rigbox" not in blob and "alice" not in blob
    assert "414d442d-4d45-5341-2d44-525600000000" not in blob
    assert "[host]" in blob and "[user]" in blob


def test_report_is_versioned_and_never_claims_inference(tmp_path):
    report, _ = collect(tmp_path)
    assert report["schema_version"] == "bnh-hw-inventory/1"
    assert report["proves_inference"] is False
    assert "not proof" in report["note"]
    assert report["kernel"] == "6.8.0-45-generic"
    assert report["os"]["pretty_name"] == "Ubuntu 24.04.1 LTS"


def test_cli_writes_json_and_refuses_to_overwrite(tmp_path, monkeypatch):
    out = tmp_path / "inventory.json"
    monkeypatch.setattr(hw, "collect_inventory", lambda **kw: {"schema_version": "x",
                                                                "findings": []})
    assert hw.main(["--out", str(out)]) == 0
    assert json.loads(out.read_text())["schema_version"] == "x"
    assert hw.main(["--out", str(out)]) == 2
