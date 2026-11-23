"""Tests for the Phase 3 evidence check.

The inventory comes from the real inventory code fed FORMAT SAMPLES, and the runs come from
the real run recorder driving a FAKE sysfs tree. Passing these tests says nothing about
whether an RX 470 works; it says the checker reads those reports the way it should.
"""
from __future__ import annotations

import copy
import json

import pytest

from app import gpu_run as gr
from app import phase3_check as p3
from tests.test_gpu_run import FAST, work_on
from tests.test_gpu_sensors import make_card
from tests.test_hw_inventory import LSPCI_TWO_AMD, VULKAN_CPU_ONLY, VULKAN_TWO_AMD, collect

ANSWER = "hello world\n"


@pytest.fixture
def inventory(tmp_path):
    d = tmp_path / "inv"
    d.mkdir()
    return collect(d)[0]


@pytest.fixture
def rig(tmp_path):
    root = tmp_path / "sys"
    make_card(root, 0, slot="0000:01:00.0", busy=0)
    return make_card(root, 1, slot="0000:02:00.0", busy=0)


def run_on(drm, card, *, out=ANSWER, expect=ANSWER, **kw):
    return gr.run(work_on(drm, card, seconds=0.3, out=out), card=card, sysfs_root=drm,
                  interval=FAST, expect_text=expect, **kw)


def status(result, key):
    return next(i for i in result["items"] if i["id"] == key)


def inv_with(tmp_path, **outputs):
    d = tmp_path / "inv2"
    d.mkdir()
    base = {"uname": "6.8.0-45-generic\n", "lspci": LSPCI_TWO_AMD, "vulkaninfo": VULKAN_TWO_AMD}
    base.update(outputs)
    return collect(d, outputs=base)[0]


# --- the happy path ------------------------------------------------------------------------

def test_inventory_plus_same_test_on_both_cards_evidences_every_required_item(inventory, rig):
    result = p3.check(inventory, [run_on(rig, "card0"), run_on(rig, "card1")])
    assert result["schema_version"] == "bnh-phase3-check/1"
    assert result["proves_inference"] is False
    assert [i["id"] for i in result["items"]] == [k for k, _ in p3.ITEMS]
    assert result["all_required_evidenced"] is True
    assert set(result["evidenced"]) == p3.REQUIRED_FOR_PASS
    assert "the same test passed on slot 01:00.0 and slot 02:00.0" in \
        status(result, "smoke_second")["note"]
    assert "Ubuntu 24.04.1 LTS" in status(result, "os")["note"]
    assert "the card names say RX 470" in status(result, "two_cards")["note"]


def test_sensors_and_versions_never_claim_more_than_partial(inventory, rig):
    result = p3.check(inventory, [run_on(rig, "card0"), run_on(rig, "card1")])
    sensors, versions = status(result, "sensors"), status(result, "versions")
    assert sensors["status"] == "partial"
    assert "62 °C" in sensors["note"] and "95 W" in sensors["note"]
    assert "stability" in sensors["note"]
    assert versions["status"] == "partial"
    assert "Mesa 24.0.9" in versions["note"] and "6.8.0-45-generic" in versions["note"]
    assert "write down its version" in versions["note"]


def test_inventory_alone_covers_the_first_six_items_only(inventory):
    result = p3.check(inventory, [])
    assert set(result["evidenced"]) == {"os", "kernel", "pci_drivers", "vulkan", "dri",
                                        "two_cards"}
    assert status(result, "smoke_first") == {
        "id": "smoke_first", "roadmap_item": "Run a minimal Vulkan compute smoke test on GPU 0",
        "status": "missing", "note": "no gpu_run reports given"}
    assert status(result, "sensors")["status"] == "missing"
    assert "nothing is shown working" in status(result, "versions")["note"]
    assert result["all_required_evidenced"] is False


# --- the second card -----------------------------------------------------------------------

def test_one_passing_card_leaves_the_repeat_missing_and_names_the_other_slot(inventory, rig):
    result = p3.check(inventory, [run_on(rig, "card0")])
    assert status(result, "smoke_first")["status"] == "evidenced"
    second = status(result, "smoke_second")
    assert second["status"] == "missing" and "02:00.0" in second["note"]


def test_two_runs_on_the_same_card_are_not_a_repeat(inventory, rig):
    result = p3.check(inventory, [run_on(rig, "card1"), run_on(rig, "card1")])
    assert status(result, "smoke_second")["status"] == "missing"


def test_a_different_test_on_each_card_is_only_partial(inventory, rig):
    other = "goodbye\n"
    result = p3.check(inventory, [run_on(rig, "card0"),
                                  run_on(rig, "card1", out=other, expect=other)])
    assert status(result, "smoke_second")["status"] == "partial"
    assert result["all_required_evidenced"] is False


def test_cards_are_matched_by_slot_not_by_card_name(inventory, tmp_path):
    # After a reboot the same name can point at another card: two "card0" runs in two slots.
    a = make_card(tmp_path / "boot1", 0, slot="0000:01:00.0", busy=0)
    b = make_card(tmp_path / "boot2", 0, slot="0000:02:00.0", busy=0)
    result = p3.check(inventory, [run_on(a, "card0"), run_on(b, "card0")])
    assert status(result, "smoke_second")["status"] == "evidenced"


# --- runs that don't count -----------------------------------------------------------------

def test_wrong_answer_does_not_count(inventory, rig):
    r = p3.check(inventory, [run_on(rig, "card0", out="nope\n")])["runs"][0]
    assert r["passes"] is False
    assert "output did NOT match the expected answer" in r["reasons"]


def test_no_expected_answer_does_not_count(inventory, rig):
    r = p3.check(inventory, [run_on(rig, "card0", expect=None)])["runs"][0]
    assert "had no expected answer, so nobody checked the output" in r["reasons"]


def test_work_on_the_wrong_card_does_not_count(inventory, rig):
    run = gr.run(work_on(rig, "card1", seconds=0.3), card="card0", sysfs_root=rig,
                 interval=FAST, expect_text=ANSWER)
    r = p3.check(inventory, [run])["runs"][0]
    assert r["passes"] is False
    assert any("card0 never got busy" in x for x in r["reasons"])
    assert "other card(s) got busy too: card1" in r["reasons"]


def test_failed_and_timed_out_runs_do_not_count(inventory, rig):
    run = run_on(rig, "card0")
    failed = copy.deepcopy(run)
    failed["evidence"]["exit_ok"] = False
    failed["run"]["exit_code"] = 3
    timed = copy.deepcopy(run)
    timed["evidence"]["exit_ok"] = False
    timed["run"]["timed_out"] = True
    runs = p3.check(inventory, [failed, timed])["runs"]
    assert "exited with code 3" in runs[0]["reasons"]
    assert "timed out" in runs[1]["reasons"]


def test_a_run_from_a_slot_the_inventory_never_saw_is_a_problem(inventory, tmp_path):
    drm = make_card(tmp_path / "other", 0, slot="0000:05:00.0", busy=0)
    r = p3.check(inventory, [run_on(drm, "card0")])["runs"][0]
    assert r["passes"] is False
    assert "05:00.0" in r["problems"][0] and "re-run the inventory" in r["problems"][0]


def test_a_run_claiming_proof_of_inference_is_treated_as_edited(inventory, rig):
    run = run_on(rig, "card0")
    run["proves_inference"] = True
    result = p3.check(inventory, [run])
    assert result["runs"][0]["passes"] is False
    assert "may have been edited" in result["runs"][0]["problems"][0]
    assert result["proves_inference"] is False


def test_wrong_driver_on_the_target_card_does_not_count(inventory, tmp_path):
    drm = make_card(tmp_path / "r", 0, slot="0000:01:00.0", busy=0, driver="radeon")
    r = p3.check(inventory, [run_on(drm, "card0")])["runs"][0]
    assert any("not amdgpu" in x for x in r["reasons"])


def test_root_is_a_note_not_a_failure(inventory, rig, monkeypatch):
    monkeypatch.setattr(gr.os, "geteuid", lambda: 0)
    r = p3.check(inventory, [run_on(rig, "card0")])["runs"][0]
    assert r["passes"] is True and r["notes"]


# --- inventory problems --------------------------------------------------------------------

def test_cpu_only_vulkan_is_a_problem_and_the_counts_disagree(tmp_path):
    result = p3.check(inv_with(tmp_path, vulkaninfo=VULKAN_CPU_ONLY), [])
    assert status(result, "vulkan")["status"] == "problem"
    two = status(result, "two_cards")
    assert two["status"] == "problem" and "0 in Vulkan" in two["note"]


def test_blocked_render_node_is_a_problem(tmp_path):
    d = tmp_path / "inv3"
    d.mkdir()
    inv = collect(d, access=lambda p: not p.endswith("renderD129"))[0]
    dri = status(p3.check(inv, []), "dri")
    assert dri["status"] == "problem" and "renderD129" in dri["note"]


def test_card_not_on_amdgpu_is_a_problem(tmp_path):
    lspci = LSPCI_TWO_AMD.replace("\tKernel driver in use: amdgpu\n\tKernel modules: radeon",
                                  "\tKernel driver in use: radeon\n\tKernel modules: radeon")
    pci = status(p3.check(inv_with(tmp_path, lspci=lspci), []), "pci_drivers")
    assert pci["status"] == "problem" and "02:00.0 (radeon)" in pci["note"]


def test_cards_that_are_not_polaris_10_are_only_partial(tmp_path):
    lspci = LSPCI_TWO_AMD.replace("[1002:67df]", "[1002:67ef]")
    two = status(p3.check(inv_with(tmp_path, lspci=lspci), []), "two_cards")
    assert two["status"] == "partial" and "not RX 470s" in two["note"]


def test_missing_commands_leave_items_missing(tmp_path):
    d = tmp_path / "inv4"
    d.mkdir()
    inv = collect(d, missing=("lspci", "vulkaninfo"))[0]
    result = p3.check(inv, [])
    assert status(result, "pci_drivers")["status"] == "missing"
    assert status(result, "vulkan")["status"] == "missing"


def test_normalize_slot():
    assert p3.normalize_slot("0000:01:00.0") == "01:00.0"
    assert p3.normalize_slot("01:00.0") == "01:00.0"
    assert p3.normalize_slot("0001:01:00.0") == "0001:01:00.0"
    assert p3.normalize_slot(None) is None


# --- command line --------------------------------------------------------------------------

def write(path, obj):
    path.write_text(json.dumps(obj))
    return str(path)


def test_cli_passes_saves_and_never_overwrites(inventory, rig, tmp_path, capsys):
    inv = write(tmp_path / "inventory.json", inventory)
    r0 = write(tmp_path / "run-card0.json", run_on(rig, "card0"))
    r1 = write(tmp_path / "run-card1.json", run_on(rig, "card1"))
    out = tmp_path / "check.json"
    assert p3.main(["--inventory", inv, r0, r1, "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "[x] Repeat on GPU 1" in printed and "Not proof of inference" in printed
    saved = json.loads(out.read_text())
    assert saved["proves_inference"] is False
    assert [i["file"] for i in saved["inputs"]] == ["inventory.json", "run-card0.json",
                                                    "run-card1.json"]
    assert str(tmp_path) not in out.read_text()  # only file names, never full paths
    assert p3.main(["--inventory", inv, r0, r1, "--out", str(out)]) == 2


def test_cli_counts_a_copied_file_once(inventory, rig, tmp_path, capsys):
    inv = write(tmp_path / "inventory.json", inventory)
    run = run_on(rig, "card0")
    a = write(tmp_path / "a.json", run)
    b = write(tmp_path / "b.json", run)
    assert p3.main(["--inventory", inv, a, b]) == 1
    captured = capsys.readouterr()
    assert "b.json is a copy" in captured.err
    assert "[ ] Repeat on GPU 1" in captured.out


def test_cli_rejects_bad_files(inventory, rig, tmp_path, capsys):
    inv = write(tmp_path / "inventory.json", inventory)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert p3.main(["--inventory", inv, str(bad)]) == 2
    run = write(tmp_path / "run.json", run_on(rig, "card0"))
    assert p3.main(["--inventory", run]) == 2  # a run file given as the inventory
    assert p3.main(["--inventory", inv, inv]) == 2  # an inventory given as a run
    assert p3.main(["--inventory", str(tmp_path / "nope.json")]) == 2
    err = capsys.readouterr().err
    assert "not valid JSON" in err and "not a bnh-hw-inventory/1 report" in err


def test_one_card_is_missing_not_a_problem(tmp_path):
    lspci = LSPCI_TWO_AMD.split("02:00.0 VGA")[0]
    vulkan = VULKAN_TWO_AMD.split("GPU1:")[0]
    d = tmp_path / "inv5"
    d.mkdir()
    inv = collect(d, outputs={"uname": "6.8.0\n", "lspci": lspci, "vulkaninfo": vulkan})[0]
    inv["dri_nodes"] = [n for n in inv["dri_nodes"] if n["name"] in ("card0", "renderD128")]
    assert status(p3.check(inv, []), "two_cards") == {
        "id": "two_cards", "roadmap_item": "Confirm both RX 470 cards enumerate independently",
        "status": "missing", "note": "only 1 AMD card found"}
