"""Tests for the read-only GPU sensor tool (Phase 3 prep).

The sysfs trees below are FORMAT SAMPLES laid out like the files the amdgpu driver
publishes under /sys/class/drm. They are not readings from the owner's rig, and passing
these tests says nothing about whether an RX 470 works.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app import gpu_sensors as gs

SCLK = "0: 300Mhz\n1: 608Mhz\n2: 1206Mhz *\n"
MCLK = "0: 300Mhz\n1: 1750Mhz *\n"


def make_card(root: Path, n: int, *, slot: str = "0000:01:00.0", vendor: str = "0x1002",
              driver: str | None = "amdgpu", temp_mc: int | None = 62000,
              crit_mc: int | None = 94000, power_uw: int | None = 95_000_000,
              power_key: str = "power1_average", vram_total: int | None = 4 * 1024**3,
              vram_used: int | None = 1024**3, busy: int | None = 100,
              sclk: str | None = SCLK, mclk: str | None = MCLK, fan: int | None = 1500,
              hwmon: bool = True) -> Path:
    """Build a fake /sys/class/drm/cardN with the same layout (symlinks included)."""
    devices = root / "devices"
    dev = devices / slot
    dev.mkdir(parents=True)
    (dev / "vendor").write_text(vendor + "\n")
    (dev / "device").write_text("0x67df\n")
    (dev / "subsystem_vendor").write_text("0x1da2\n")
    (dev / "subsystem_device").write_text("0xe366\n")
    (dev / "unique_id").write_text("SECRET-SERIAL-123\n")
    if driver:
        drv = root / "drivers" / driver
        drv.mkdir(parents=True, exist_ok=True)
        os.symlink(drv, dev / "driver")
    for name, val in (("mem_info_vram_total", vram_total), ("mem_info_vram_used", vram_used),
                      ("gpu_busy_percent", busy)):
        if val is not None:
            (dev / name).write_text(f"{val}\n")
    if sclk is not None:
        (dev / "pp_dpm_sclk").write_text(sclk)
    if mclk is not None:
        (dev / "pp_dpm_mclk").write_text(mclk)
    if hwmon:
        hw = dev / "hwmon" / "hwmon3"
        hw.mkdir(parents=True)
        (hw / "temp1_label").write_text("edge\n")
        for name, val in (("temp1_input", temp_mc), ("temp1_crit", crit_mc),
                          (power_key, power_uw), ("power1_cap", 145_000_000), ("fan1_input", fan)):
            if val is not None:
                (hw / name).write_text(f"{val}\n")
    drm = root / "drm"
    drm.mkdir(exist_ok=True)
    card = drm / f"card{n}"
    card.mkdir()
    os.symlink(dev, card / "device")
    return drm


@pytest.fixture
def two_cards(tmp_path):
    make_card(tmp_path, 0, slot="0000:01:00.0")
    drm = make_card(tmp_path, 1, slot="0000:02:00.0", temp_mc=58000, vram_used=512 * 1024**2)
    # Things that live next to the cards and must be skipped.
    (drm / "card0-DP-1").mkdir()
    (drm / "renderD128").mkdir()
    (drm / "version").write_text("drm 1.1.0\n")
    return drm


def test_lists_only_cards_in_numeric_order(tmp_path):
    for n, slot in ((10, "0000:0a:00.0"), (2, "0000:02:00.0"), (1, "0000:01:00.0")):
        drm = make_card(tmp_path, n, slot=slot)
    (drm / "card1-HDMI-A-1").mkdir()
    (drm / "renderD129").mkdir()
    assert [p.name for p in gs.list_cards(drm)] == ["card1", "card2", "card10"]


def test_missing_sysfs_root_gives_no_cards(tmp_path):
    assert gs.list_cards(tmp_path / "nope") == []
    report = gs.collect(sysfs_root=tmp_path / "nope")
    assert report["cards"] == []
    assert any("No GPU cards" in f["msg"] for f in report["findings"])


def test_reads_one_card_in_plain_units(two_cards):
    r = gs.read_card(two_cards / "card0")
    assert r["pci_slot"] == "0000:01:00.0"
    assert r["is_amd"] and r["vendor_id"] == "0x1002" and r["device_id"] == "0x67df"
    assert r["subsystem_id"] == "0x1da2:0xe366"
    assert r["driver"] == "amdgpu"
    assert r["temp_c"] == 62.0 and r["temp_crit_c"] == 94.0 and r["temp_label"] == "edge"
    assert r["power_w"] == 95.0 and r["power_cap_w"] == 145.0
    assert r["vram_total_mib"] == 4096.0 and r["vram_used_mib"] == 1024.0
    assert r["busy_percent"] == 100 and r["fan_rpm"] == 1500
    assert (r["sclk_mhz"], r["sclk_max_mhz"]) == (1206, 1206)
    assert (r["mclk_mhz"], r["mclk_max_mhz"]) == (1750, 1750)


def test_never_reports_serial_numbers(two_cards):
    text = json.dumps(gs.collect(sysfs_root=two_cards))
    assert "SECRET-SERIAL" not in text and "unique_id" not in text


def test_power_falls_back_to_power1_input(tmp_path):
    drm = make_card(tmp_path, 0, power_key="power1_input", power_uw=40_500_000)
    assert gs.read_card(drm / "card0")["power_w"] == 40.5


def test_missing_and_garbled_files_become_none(tmp_path):
    drm = make_card(tmp_path, 0, hwmon=False, vram_used=None, busy=None, sclk="garbage\n",
                    mclk=None)
    (tmp_path / "devices" / "0000:01:00.0" / "mem_info_vram_total").write_text("lots\n")
    r = gs.read_card(drm / "card0")
    for key in ("temp_c", "temp_crit_c", "power_w", "fan_rpm", "vram_used_mib",
                "vram_total_mib", "busy_percent", "sclk_mhz", "mclk_mhz", "temp_label"):
        assert r[key] is None, key


def test_unreadable_file_is_none_not_a_crash(tmp_path, monkeypatch):
    drm = make_card(tmp_path, 0)
    real_open = open
    target = str(tmp_path / "devices" / "0000:01:00.0" / "gpu_busy_percent")

    def fake_open(path, *a, **kw):
        if os.path.realpath(path) == target:
            raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    assert gs.read_card(drm / "card0")["busy_percent"] is None


def test_binary_file_is_none(tmp_path):
    drm = make_card(tmp_path, 0)
    (tmp_path / "devices" / "0000:01:00.0" / "gpu_busy_percent").write_bytes(b"\xff\xfe\x00")
    assert gs.read_card(drm / "card0")["busy_percent"] is None


def test_reads_are_capped(tmp_path):
    drm = make_card(tmp_path, 0)
    (tmp_path / "devices" / "0000:01:00.0" / "pp_dpm_sclk").write_text(
        "0: 300Mhz\n" * 2000 + "9: 1206Mhz *\n")
    r = gs.read_card(drm / "card0")
    assert r["sclk_mhz"] is None and r["sclk_max_mhz"] == 300  # starred line is past the cap


@pytest.mark.parametrize("text,expected", [
    ("0: 300Mhz\n1: 608Mhz *\n2: 1206Mhz\n", {"current_mhz": 608, "max_mhz": 1206, "levels": 3}),
    ("0: 300Mhz\n1: 1206Mhz\n", {"current_mhz": None, "max_mhz": 1206, "levels": 2}),
    ("0: 300MHz *\n", {"current_mhz": 300, "max_mhz": 300, "levels": 1}),
    ("", None), (None, None), ("OD_SCLK:\n", None),
])
def test_parse_dpm(text, expected):
    assert gs.parse_dpm(text) == expected


def test_only_requested_cards_are_read(two_cards):
    report = gs.collect(sysfs_root=two_cards, cards=["card1"])
    assert [c["card"] for c in report["cards"]] == ["card1"]
    assert report["cards_requested"] == ["card1"]


def test_unknown_requested_card_is_a_warning(two_cards):
    report = gs.collect(sysfs_root=two_cards, cards=["card7"])
    assert report["cards"] == []
    assert any("card7 was asked for" in f["msg"] for f in report["findings"])


def test_single_snapshot_report_shape(two_cards):
    report = gs.collect(sysfs_root=two_cards, sleep=lambda s: pytest.fail("no sleep for 1 sample"))
    assert report["schema_version"] == "bnh-gpu-sensors/1"
    assert report["proves_inference"] is False
    assert report["watch"]["samples_taken"] == 1 and report["watch"]["interval_seconds"] is None
    assert [c["card"] for c in report["cards"]] == ["card0", "card1"]
    assert report["cards"][0]["summary"]["temp_c"] == {"min": 62.0, "max": 62.0, "avg": 62.0,
                                                       "readings": 1}
    assert any(f["level"] == "ok" and "peak temperature 62" in f["msg"]
               for f in report["findings"])


def test_watch_mode_summarizes_min_max_avg(tmp_path):
    drm = make_card(tmp_path, 0)
    hw = tmp_path / "devices" / "0000:01:00.0" / "hwmon" / "hwmon3"
    dev = tmp_path / "devices" / "0000:01:00.0"
    temps = iter([60000, 70000, 80000])
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        (hw / "temp1_input").write_text(f"{next(temps)}\n")
        (dev / "gpu_busy_percent").write_text("0\n" if len(sleeps) == 1 else "100\n")

    (hw / "temp1_input").write_text("50000\n")
    seen = []
    report = gs.collect(sysfs_root=drm, samples=4, interval=2.0, sleep=fake_sleep,
                        on_sample=lambda i, now: seen.append((i, now[0]["temp_c"])))
    assert sleeps == [2.0, 2.0, 2.0]
    assert seen == [(1, 50.0), (2, 60.0), (3, 70.0), (4, 80.0)]
    t = report["cards"][0]["summary"]["temp_c"]
    assert t == {"min": 50.0, "max": 80.0, "avg": 65.0, "readings": 4}
    assert report["cards"][0]["summary"]["busy_percent"]["min"] == 0
    assert report["cards"][0]["last"]["temp_c"] == 80.0
    assert report["watch"] == {"samples_requested": 4, "samples_taken": 4, "interval_seconds": 2.0,
                               "duration_seconds": report["watch"]["duration_seconds"],
                               "stopped_early": False}


def test_summary_skips_missing_readings():
    s = gs.summarize([{"temp_c": 60.0}, {"temp_c": None}, {"temp_c": 70.0}, {}])
    assert s["temp_c"] == {"min": 60.0, "max": 70.0, "avg": 65.0, "readings": 2}
    assert s["power_w"] == {"min": None, "max": None, "avg": None, "readings": 0}


def test_ctrl_c_keeps_what_was_read(tmp_path):
    drm = make_card(tmp_path, 0)
    calls = []

    def interrupt_on_second(s):
        calls.append(s)
        if len(calls) == 2:
            raise KeyboardInterrupt

    report = gs.collect(sysfs_root=drm, samples=10, interval=1.0, sleep=interrupt_on_second)
    assert report["watch"]["stopped_early"] is True
    assert report["watch"]["samples_taken"] == 2
    assert report["cards"][0]["summary"]["temp_c"]["readings"] == 2


def test_ctrl_c_before_first_reading(tmp_path, monkeypatch):
    drm = make_card(tmp_path, 0)

    def boom(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(gs, "read_card", boom)
    report = gs.collect(sysfs_root=drm, cards=["card0"])
    assert report["watch"]["samples_taken"] == 0 and report["cards"] == []
    msgs = [f["msg"] for f in report["findings"]]
    assert msgs == ["Stopped before the first reading, so there is nothing to report."]


@pytest.mark.parametrize("kwargs", [{"samples": 0}, {"samples": gs.MAX_SAMPLES + 1},
                                    {"samples": 2, "interval": 0.1},
                                    {"samples": 2, "interval": 61}])
def test_collect_refuses_bad_bounds(tmp_path, kwargs):
    with pytest.raises(ValueError):
        gs.collect(sysfs_root=tmp_path, **kwargs)


def test_hot_card_warns(tmp_path):
    drm = make_card(tmp_path, 0, temp_mc=86000, crit_mc=None)
    f = gs.collect(sysfs_root=drm)["findings"]
    assert any(x["level"] == "warn" and "reached 86 °C" in x["msg"] for x in f)


def test_near_driver_critical_limit_warns_below_85(tmp_path):
    drm = make_card(tmp_path, 0, temp_mc=76000, crit_mc=80000)
    f = gs.collect(sysfs_root=drm)["findings"]
    assert any(x["level"] == "warn" and "critical limit 80" in x["msg"] for x in f)


def test_cool_card_is_ok(tmp_path):
    drm = make_card(tmp_path, 0, temp_mc=84000, crit_mc=94000)
    f = gs.collect(sysfs_root=drm)["findings"]
    assert not any(x["level"] == "warn" for x in f)


def test_vram_nearly_full_warns(tmp_path):
    drm = make_card(tmp_path, 0, vram_used=int(3.8 * 1024**3))
    f = gs.collect(sysfs_root=drm)["findings"]
    assert any(x["level"] == "warn" and "peak VRAM use" in x["msg"] for x in f)


def test_vram_with_room_is_ok(tmp_path):
    drm = make_card(tmp_path, 0, vram_used=1024**3)
    f = gs.collect(sysfs_root=drm)["findings"]
    assert any(x["level"] == "ok" and "(25%)" in x["msg"] for x in f)


def test_wrong_driver_warns_and_skips_numbers(tmp_path):
    drm = make_card(tmp_path, 0, driver="radeon", temp_mc=99000)
    f = gs.collect(sysfs_root=drm)["findings"]
    assert any(x["level"] == "warn" and "'radeon', not amdgpu" in x["msg"] for x in f)
    assert not any("99" in x["msg"] for x in f)


def test_no_driver_warns(tmp_path):
    drm = make_card(tmp_path, 0, driver=None)
    f = gs.collect(sysfs_root=drm)["findings"]
    assert any("'none', not amdgpu" in x["msg"] for x in f)


def test_non_amd_card_is_info_only(tmp_path):
    drm = make_card(tmp_path, 0, vendor="0x8086", driver="i915")
    f = gs.collect(sysfs_root=drm)["findings"]
    assert [x["level"] for x in f] == ["info"]
    assert "not an AMD card" in f[0]["msg"]


def test_missing_sensors_are_named(tmp_path):
    drm = make_card(tmp_path, 0, hwmon=False, vram_used=None)
    msgs = [x["msg"] for x in gs.collect(sysfs_root=drm)["findings"]]
    assert any("no temperature reading" in m for m in msgs)
    assert any("no power reading" in m for m in msgs)
    assert any("no VRAM reading" in m for m in msgs)


def test_idle_card_during_watch_is_flagged(tmp_path):
    drm = make_card(tmp_path, 0, busy=0)
    f = gs.collect(sysfs_root=drm, samples=2, interval=1, sleep=lambda s: None)["findings"]
    assert any("busy stayed at 0%" in x["msg"] for x in f)
    one = gs.collect(sysfs_root=drm)["findings"]
    assert not any("busy stayed" in x["msg"] for x in one)


def test_tool_never_writes_to_sysfs(two_cards, tmp_path):
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.rglob("*")
              if p.is_file() and not p.is_symlink()}
    gs.collect(sysfs_root=two_cards, samples=3, interval=1, sleep=lambda s: None)
    after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.rglob("*")
             if p.is_file() and not p.is_symlink()}
    assert before == after


def test_module_runs_no_commands():
    src = Path(gs.__file__).read_text()
    for banned in ("subprocess", "os.system", "Popen", "write_text", '"w"', "'w'", '"a"'):
        assert banned not in src, banned


# --- command line --------------------------------------------------------------------------

def test_cli_prints_json(two_cards, capsys):
    assert gs.main([], sysfs_root=two_cards) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "bnh-gpu-sensors/1"
    assert len(report["cards"]) == 2


def test_cli_writes_file_and_refuses_overwrite(two_cards, tmp_path, capsys):
    out = tmp_path / "sensors.json"
    assert gs.main(["--card", "card1", "--out", str(out)], sysfs_root=two_cards) == 0
    saved = json.loads(out.read_text())
    assert [c["card"] for c in saved["cards"]] == ["card1"]
    printed = capsys.readouterr().out
    assert "saved" in printed and "[ok]" in printed
    out.write_text("keep me")
    assert gs.main(["--out", str(out)], sysfs_root=two_cards) == 2
    assert out.read_text() == "keep me"


def test_cli_watch_prints_progress(two_cards, capsys):
    assert gs.main(["--samples", "3", "--interval", "1"], sysfs_root=two_cards,
                   sleep=lambda s: None) == 0
    err = capsys.readouterr().err
    assert "sample 1/3:" in err and "sample 3/3:" in err and "card0 62°C 95W" in err


@pytest.mark.parametrize("argv", [["--card", "../etc"], ["--card", "renderD128"],
                                  ["--samples", "0"], ["--samples", "99999"],
                                  ["--samples", "2", "--interval", "0.01"]])
def test_cli_rejects_bad_arguments(two_cards, argv):
    with pytest.raises(SystemExit) as exc:
        gs.main(argv, sysfs_root=two_cards)
    assert exc.value.code == 2


def test_cli_refuses_existing_file_before_a_long_watch(two_cards, tmp_path):
    out = tmp_path / "taken.json"
    out.write_text("keep me")
    assert gs.main(["--samples", "120", "--out", str(out)], sysfs_root=two_cards,
                   sleep=lambda s: pytest.fail("watched before refusing")) == 2
    assert out.read_text() == "keep me"
