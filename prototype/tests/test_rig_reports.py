"""Rig reports from strangers (`rig-report-v1`, code audit fifth pass).

`python -m app.phase3_check` reads an inventory and gpu_run reports that come from a rig:
today the owner's, later a volunteer's, sent by chat or email. Before this pass it read
them with a plain `json.loads`, so:

  * the text "false" in `exit_ok` / `target_card_busy` counted as true (Python treats any
    non-empty text as true), so a mistyped or edited run that crashed could pass;
  * a 400-digit number for a temperature or wattage crashed the printout (`:g`);
  * a list where an object belongs, an AMD card without a slot, a `null` slot, ... crashed
    it with a traceback;
  * OS, kernel, card and driver names were printed exactly as written, terminal codes
    included; a named pipe given as a report hung it; `1e400` became infinity.

These tests build HONEST reports with the real recording code (format samples, fake sysfs),
then break every field the checker reads, one at a time, in every way we could think of.
The rule: refuse cleanly (exit 2, one line naming the field) or carry on; never crash, hang
or print a control character, and never let a wrong-type value make a run count.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from app import gpu_run as gr
from app import phase3_check as p3
from tests.test_gpu_run import FAST, work_on
from tests.test_gpu_sensors import make_card
from tests.test_phase3_check import ANSWER
from tests.test_hw_inventory import collect

# ---- honest reports ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def honest(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], list[dict]]:
    base = tmp_path_factory.mktemp("rig")
    (base / "inv").mkdir()
    inventory = collect(base / "inv")[0]
    root = base / "sys"
    make_card(root, 0, slot="0000:01:00.0", busy=0)
    drm = make_card(root, 1, slot="0000:02:00.0", busy=0)
    model = base / "test-model.bin"  # an argument file, so `command.files` isn't empty
    model.write_bytes(b"weights")
    runs = [gr.run(work_on(drm, card, seconds=0.3, out=ANSWER) + [str(model)], card=card,
                   sysfs_root=drm, interval=FAST, expect_text=ANSWER)
            for card in ("card0", "card1")]
    assert runs[0]["command"]["files"]
    return inventory, runs


def spec_paths(spec: Any, prefix: tuple = ()) -> dict[tuple, Any]:
    """Every (path, kind) a spec describes; `[]` = any list item, `*` = any key."""
    out: dict[tuple, Any] = {}
    if isinstance(spec, dict):
        for key, sub in spec.items():
            out[prefix + (key,)] = sub
            out.update(spec_paths(sub, prefix + (key,)))
    elif isinstance(spec, p3.ListOf):
        out[prefix + ("[]",)] = spec.spec
        out.update(spec_paths(spec.spec, prefix + ("[]",)))
    elif isinstance(spec, p3.Each):
        out[prefix + ("*",)] = spec.spec
        out.update(spec_paths(spec.spec, prefix + ("*",)))
    return out


def concrete(data: Any, pattern: tuple, prefix: tuple = ()) -> list[tuple]:
    """The real paths in `data` matching a spec path (first list item / first key only)."""
    if not pattern:
        return [prefix]
    head, rest = pattern[0], pattern[1:]
    if head == "[]":
        return concrete(data[0], rest, prefix + (0,)) if isinstance(data, list) and data else []
    if head == "*":
        if isinstance(data, dict) and data:
            key = next(iter(data))
            return concrete(data[key], rest, prefix + (key,))
        return []
    if isinstance(data, dict) and head in data:
        return concrete(data[head], rest, prefix + (head,))
    return []


def set_at(data: Any, path: tuple, value: Any) -> Any:
    data = copy.deepcopy(data)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


def delete_at(data: Any, path: tuple) -> Any:
    data = copy.deepcopy(data)
    target = data
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    return data


def write(path: Path, obj: Any) -> str:
    path.write_text(json.dumps(obj))
    return str(path)


def run_cli(tmp_path: Path, inventory: Any, runs: list[Any],
            capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    inv = write(tmp_path / "inventory.json", inventory)
    files = [write(tmp_path / f"run{i}.json", r) for i, r in enumerate(runs)]
    code = p3.main(["--inventory", inv, *files])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def assert_printable(text: str) -> None:
    bad = [ch for ch in text if ch != "\n" and not ch.isprintable()]
    assert not bad, f"printed control/invisible characters: {bad!r}"


# Wrong-type values for each kind of field. Every one must be refused (exit 2).
WRONG = {
    p3.TEXT: [1, 1.5, True, [], {}, ["x"], "x" * (p3.MAX_TEXT + 1), "\x1b[2J\x1b]0;pwned\x07",
              "line\nbreak", "tab\there", "ab‮dc", "zero​width", "\x00", "\x9b31m"],
    p3.BOOL: ["false", "true", "", "no", 0, 1, 0.0, [], {}, [False]],
    p3.INT: ["5", 1.5, True, False, [], {}, 10 ** 400, -(10 ** 16)],
    p3.NUM: ["62", True, [], {}, 10 ** 400, 1e300, -1e16],
}
CONTAINER_WRONG = ["x", 1, True, [1], [], 1.5]
LIST_WRONG = ["x", 1, True, {"a": 1}, [None], [1], [[]]]


def kind_name(kind: Any) -> str:
    return kind if isinstance(kind, str) else type(kind).__name__


def hostile_cases() -> list[tuple[str, tuple, Any, Any]]:
    cases = []
    for which, spec in (("inventory", p3.INVENTORY_SPEC), ("run", p3.RUN_SPEC)):
        for path, kind in spec_paths(spec).items():
            values = (WRONG[kind] if isinstance(kind, str)
                      else LIST_WRONG if isinstance(kind, p3.ListOf) else CONTAINER_WRONG)
            for value in values:
                cases.append((which, path, kind, value))
    return cases


CASES = hostile_cases()


# ---- every field, every wrong type --------------------------------------------------------

@pytest.mark.parametrize("which,path,kind,value", CASES,
                         ids=[f"{w}:{'.'.join(map(str, p))}={kind_name(k)}:{i}"
                              for i, (w, p, k, _) in enumerate(CASES)])
def test_every_field_with_a_wrong_type_is_refused_with_one_line(
        honest, tmp_path, capsys, which, path, kind, value) -> None:
    inventory, runs = honest
    original = inventory if which == "inventory" else runs[0]
    hits = concrete(original, path)
    if hits:
        broken = set_at(original, hits[0], value)
    else:
        # The honest sample doesn't have this field (an optional one, or an empty list):
        # add it where its parent is.
        parents = concrete(original, path[:-1])
        if not parents:
            pytest.skip("no parent for this field in the honest sample")
        holder = {"[]": [value], "*": {"k": value}}.get(path[-1])
        broken = (set_at(original, parents[0], holder) if holder is not None
                  else set_at(original, parents[0] + (path[-1],), value))
    if which == "inventory":
        code, out = run_cli(tmp_path, broken, runs, capsys)
    else:
        code, out = run_cli(tmp_path, inventory, [broken, runs[1]], capsys)
    assert code == 2, out
    assert out.startswith("error: ") and out.count("\n") == 1, out
    assert "Traceback" not in out
    assert_printable(out)


def test_the_specs_cover_more_than_a_hundred_hostile_cases() -> None:
    assert len(CASES) > 300


@pytest.mark.parametrize("which", ["inventory", "run"])
def test_any_field_can_be_missing_or_null_without_a_crash(honest, tmp_path, capsys,
                                                          which) -> None:
    """Missing means "no evidence", as before: never a crash, and nothing extra counts."""
    inventory, runs = honest
    spec = p3.INVENTORY_SPEC if which == "inventory" else p3.RUN_SPEC
    original = inventory if which == "inventory" else runs[0]
    tried = 0
    for path in spec_paths(spec):
        for hit in concrete(original, path):
            # A null *item* in a list is refused (the checker would call .get on it);
            # a null or missing *field* just means "no evidence".
            nulls = [] if isinstance(hit[-1], int) else [set_at(original, hit, None)]
            for broken in (delete_at(original, hit), *nulls):
                if which == "inventory":
                    code, out = run_cli(tmp_path, broken, runs, capsys)
                else:
                    code, out = run_cli(tmp_path, inventory, [broken, runs[1]], capsys)
                tried += 1
                assert code in (0, 1), (hit, out)
                assert "Traceback" not in out
                assert_printable(out)
    assert tried > 40


def test_the_honest_reports_still_pass_every_check(honest, tmp_path, capsys) -> None:
    inventory, runs = honest
    p3.vet_inventory(inventory)
    for r in runs:
        p3.vet_run(r)
    code, out = run_cli(tmp_path, inventory, runs, capsys)
    assert code == 0, out
    assert "[x] Repeat on GPU 1" in out


# ---- the holes this pass closed -----------------------------------------------------------

@pytest.mark.parametrize("field", [("evidence", "exit_ok"), ("evidence", "target_card_busy"),
                                   ("expected", "matched")])
def test_the_text_false_no_longer_counts_as_true(honest, tmp_path, capsys, field) -> None:
    """Before: `"exit_ok": "false"` on a run that crashed with exit code 1 counted as clean
    (and `"target_card_busy": "false"` as busy). `matched` was compared with `is True`, so
    it was already safe; it is type-checked now like every other yes/no field."""
    inventory, runs = honest
    failed = set_at(runs[1], field, "false")
    failed = set_at(failed, ("run", "exit_code"), 1)
    assert bool("false") is True  # why it used to pass
    code, out = run_cli(tmp_path, inventory, [runs[0], failed], capsys)
    assert code == 2 and "must be true or false" in out


def test_a_400_digit_temperature_is_refused_not_a_crash(honest, tmp_path, capsys) -> None:
    """Before: OverflowError from `f"{temp:g}"` while printing the sensor line."""
    inventory, runs = honest
    hot = set_at(runs[0], ("sensors", "cards", 0, "during", "temp_c", "max"), 10 ** 400)
    with pytest.raises(OverflowError):
        f"{10 ** 400:g}"  # noqa: B018  (what the old code did with it)
    code, out = run_cli(tmp_path, inventory, [hot, runs[1]], capsys)
    assert code == 2 and "temp_c.max must be a number below" in out


def test_an_amd_card_without_a_slot_is_reported_not_a_crash(honest) -> None:
    """Before: `g["slot"]` KeyError and `", ".join([None])` TypeError."""
    inventory, runs = honest
    amd = [i for i, g in enumerate(inventory["pci_display_devices"]) if g["is_amd"]]
    for broken in (delete_at(inventory, ("pci_display_devices", amd[0], "slot")),
                   set_at(inventory, ("pci_display_devices", amd[0], "slot"), None),
                   set_at(set_at(inventory, ("pci_display_devices", amd[0], "slot"), None),
                          ("pci_display_devices", amd[0], "device_id"), "1234")):
        p3.vet_inventory(broken)
        result = p3.check(broken, runs)
        assert "?" in next(i for i in result["items"] if i["id"] == "pci_drivers")["note"]
    nodes = [i for i, n in enumerate(inventory["dri_nodes"]) if n["kind"] == "render"]
    blocked = set_at(inventory, ("dri_nodes", nodes[0], "user_can_open"), False)
    blocked = delete_at(blocked, ("dri_nodes", nodes[0], "name"))
    assert "?" in next(i for i in p3.check(blocked, runs)["items"] if i["id"] == "dri")["note"]


def test_terminal_codes_in_a_kernel_name_are_refused(honest, tmp_path, capsys) -> None:
    inventory, runs = honest
    evil = set_at(inventory, ("kernel",), "6.8\x1b[2J\x1b[Hroot@rig:~# ")
    code, out = run_cli(tmp_path, evil, runs, capsys)
    assert code == 2 and "kernel must be plain printable text" in out
    assert "\x1b" not in out


@pytest.mark.parametrize("raw", [
    b'{"schema_version": "bnh-gpu-run/1", "x": 1e400}',
    b'{"schema_version": "bnh-gpu-run/1", "x": NaN}',
    b'{"schema_version": "bnh-gpu-run/1", "evidence": {"exit_ok": false, "exit_ok": true}}',
    b"[" * 100_000 + b"]" * 100_000,
    b'{"schema_version": "bnh-gpu-run/1", "x": "\xff"}',
    b"",
])
def test_unsafe_json_in_a_run_file_is_refused(honest, tmp_path, capsys, raw) -> None:
    inventory, _ = honest
    inv = write(tmp_path / "inventory.json", inventory)
    (tmp_path / "run.json").write_bytes(raw)
    assert p3.main(["--inventory", inv, str(tmp_path / "run.json")]) == 2
    out = capsys.readouterr().err
    assert "refusing to read it" in out and "Traceback" not in out


def test_a_report_over_the_size_limit_is_refused_before_parsing(honest, tmp_path,
                                                                 capsys) -> None:
    inventory, runs = honest
    big = dict(inventory, padding="x" * (p3.MAX_REPORT_BYTES + 1))
    code, out = run_cli(tmp_path, big, runs, capsys)
    assert code == 2 and "too big" in out


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_named_pipe_given_as_the_inventory_does_not_hang(tmp_path, capsys) -> None:
    fifo = tmp_path / "inventory.json"
    os.mkfifo(fifo)
    result: list[int] = []
    t = threading.Thread(target=lambda: result.append(p3.main(["--inventory", str(fifo)])),
                         daemon=True)
    t.start()
    t.join(10)
    assert result == [2], "phase3_check hung on a named pipe"
    assert "not a regular file" in capsys.readouterr().err


def test_file_names_with_terminal_codes_are_printed_safely(honest, tmp_path, capsys) -> None:
    inventory, runs = honest
    inv = write(tmp_path / "inventory.json", inventory)
    odd = write(tmp_path / "run\x1b[31m.json", runs[0])
    copy_of = write(tmp_path / "copy\x1b]0;x\x07.json", runs[0])
    out_file = tmp_path / "check.json"
    p3.main(["--inventory", inv, odd, copy_of, "--out", str(out_file)])
    captured = capsys.readouterr()
    assert_printable(captured.out + captured.err)
    assert "run?[31m.json" in captured.out
    saved = json.loads(out_file.read_text())
    assert [i["file"] for i in saved["inputs"]] == ["inventory.json", "run?[31m.json"]


def test_lists_and_objects_are_capped(honest, tmp_path, capsys) -> None:
    inventory, runs = honest
    many = set_at(inventory, ("dri_nodes",), [{"name": f"renderD{i}", "kind": "render",
                                               "user_can_open": True}
                                              for i in range(p3.MAX_ITEMS + 1)])
    code, out = run_cli(tmp_path, many, runs, capsys)
    assert code == 2 and "at most 64 items" in out
    cmds = set_at(inventory, ("commands",), {f"c{i}": {} for i in range(p3.MAX_ITEMS + 1)})
    code, out = run_cli(tmp_path, cmds, runs, capsys)
    assert code == 2 and "at most 64 entries" in out


# ---- the specs cover everything the checker reads ----------------------------------------

class Tracing(dict):
    """A dict that records every key the checker looks up (through .get or [])."""

    def __init__(self, data: dict, path: tuple, seen: set) -> None:
        super().__init__(data)
        self._path, self._seen = path, seen

    def _wrap(self, key: str, value: Any) -> Any:
        path = self._path + (key,)
        self._seen.add(path)
        return trace(value, path, self._seen)

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return self._wrap(key, super().get(key, default))

    def __getitem__(self, key: str) -> Any:
        return self._wrap(key, super().__getitem__(key))


def trace(value: Any, path: tuple, seen: set) -> Any:
    if isinstance(value, dict) and not isinstance(value, Tracing):
        return Tracing(value, path, seen)
    if isinstance(value, list):
        return [trace(v, path + ("[]",), seen) for v in value]
    return value


def normalise(path: tuple, spec: dict) -> tuple:
    """Map concrete free keys (command names, sensor names) to `*` where the spec says so."""
    out: list[str] = []
    node: Any = spec
    for key in path:
        if isinstance(node, p3.Each):
            out.append("*")
            node = node.spec
            continue
        if isinstance(node, p3.ListOf) and key == "[]":
            out.append("[]")
            node = node.spec
            continue
        out.append(key)
        node = node.get(key) if isinstance(node, dict) else None
    return tuple(out)


def test_every_field_the_checker_reads_is_vetted_first(honest) -> None:
    """If someone teaches the checker to read a new field, this fails until the field is
    added to INVENTORY_SPEC or RUN_SPEC (and so gets type-checked)."""
    inventory, runs = honest
    inv_seen: set = set()
    run_seen: set = set()
    p3.check(trace(inventory, (), inv_seen), [trace(r, (), run_seen) for r in runs])
    allowed_inv = set(spec_paths(p3.INVENTORY_SPEC)) | {("schema_version",)}
    allowed_run = set(spec_paths(p3.RUN_SPEC)) | {("schema_version",)}
    unvetted = ({normalise(p, p3.INVENTORY_SPEC) for p in inv_seen} - allowed_inv,
                {normalise(p, p3.RUN_SPEC) for p in run_seen} - allowed_run)
    assert unvetted == (set(), set()), f"read but not vetted: {unvetted}"
    assert len(inv_seen) > 10 and len(run_seen) > 10
