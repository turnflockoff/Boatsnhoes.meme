"""JSON files from strangers (`json-file-v1`, code audit fourth pass).

Signed reward reports, release manifests/signatures/signer lists and the public
`reports/index.json` are written by somebody else and read by volunteers' tools (and CI).
They must be refused cleanly when hostile, never crash, hang or print control codes, and
nothing a file merely *claims* (a fingerprint label, a point total shown on the page) may
be shown as checked unless it was.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

import pytest

from app import json_file, rewards
from app import publish_report as pub
from app import release as rel
from app.observability import _PLAIN_TEXT
from app.signing import ManifestSigner
from tests.test_audits import T0, make, pinned  # noqa: F401  (pytest fixture)
from tests.test_publish_report import add, two_periods
from tests.test_report_signing import closed_period

APP = Path(__file__).resolve().parents[1] / "app"

HOSTILE = {
    "not json": b"hello",
    "deep": b"[" * 100_000 + b"]" * 100_000,
    "deep object": b'{"a":' * 40 + b"1" + b"}" * 40,
    "NaN": b'{"report_type": NaN}',
    "Infinity": b'{"x": -Infinity}',
    "duplicate key": b'{"report_type": "x", "report_type": "y"}',
    "huge number": b'{"x": ' + b"9" * 5000 + b"}",
    "bad utf-8": b'{"x": "\xff"}',
    "byte-order mark": b'\xef\xbb\xbf{"x": 1}',
    "empty": b"",
    "whitespace": b"  \n ",
}


# ---- the loader --------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_loader_refuses_hostile_json(tmp_path: Path, name: str) -> None:
    f = tmp_path / "x.json"
    f.write_bytes(HOSTILE[name])
    with pytest.raises(json_file.UnsafeJSONError):
        json_file.load(f, "test file")


def test_loader_reads_plain_json_and_nested_within_the_limit(tmp_path: Path) -> None:
    f = tmp_path / "ok.json"
    f.write_text('{"a": [1, 2.5, "\\u00e9", null, true], "b": {"c": {}}}')
    assert json_file.load(f) == {"a": [1, 2.5, "é", None, True], "b": {"c": {}}}
    f.write_bytes(b"[" * 32 + b"]" * 32)
    json_file.load(f)  # exactly the limit is fine
    f.write_bytes(b"[" * 33 + b"]" * 33)
    with pytest.raises(json_file.UnsafeJSONError, match="deeper than 32"):
        json_file.load(f)
    # Brackets inside strings don't count.
    f.write_text(json.dumps({"s": "[" * 1000}))
    assert json_file.load(f) == {"s": "[" * 1000}


def test_duplicate_keys_are_refused_at_any_depth(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_bytes(b'{"totals": {"payable_points": 1, "payable_points": 9999}}')
    with pytest.raises(json_file.UnsafeJSONError, match="appears twice"):
        json_file.load(f)
    f.write_bytes(b'[{"a": 1}, {"a": 2}]')  # same key in different objects is fine
    assert json_file.load(f) == [{"a": 1}, {"a": 2}]


def test_size_limit_is_checked_before_reading(tmp_path: Path) -> None:
    f = tmp_path / "big.json"
    f.write_bytes(b'{"x": "' + b"a" * 2000 + b'"}')
    with pytest.raises(json_file.UnsafeJSONError, match="too big"):
        json_file.load(f, max_bytes=1000)
    assert json_file.load(f, max_bytes=3000)["x"] == "a" * 2000
    # A sparse 5 GB file is refused from its size alone, without reading it.
    sparse = tmp_path / "sparse.json"
    with open(sparse, "wb") as fh:
        fh.truncate(5 * 1024 ** 3)
    with pytest.raises(json_file.UnsafeJSONError, match="too big"):
        json_file.load(sparse)


def test_oversized_file_is_refused_without_reading_a_byte(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "big.json"
    f.write_bytes(b"[" + b"1," * 1000 + b"1]")

    def no_read(*_a, **_k):
        raise AssertionError("read an oversized file")

    monkeypatch.setattr(json_file.os, "read", no_read)
    with pytest.raises(json_file.UnsafeJSONError, match=r"too big \(\d+ bytes"):
        json_file.load(f, max_bytes=100)


def test_a_file_that_grows_while_being_read_is_cut_off(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """The size is checked up front, but a file can grow after that (a log, a pipe-like
    mount): reading stops one byte past the limit."""
    f = tmp_path / "grows.json"
    f.write_bytes(b'"' + b"a" * 5000 + b'"')
    real_fstat = os.fstat

    class Small:
        def __init__(self, st):
            self.st_mode, self.st_size = st.st_mode, 10

    monkeypatch.setattr(json_file.os, "fstat", lambda fd: Small(real_fstat(fd)))
    reads: list[int] = []
    real_read = os.read
    monkeypatch.setattr(json_file.os, "read",
                        lambda fd, n: reads.append(n) or real_read(fd, n))
    with pytest.raises(json_file.UnsafeJSONError, match="over 1000 bytes"):
        json_file.load(f, max_bytes=1000)
    assert sum(reads) <= 1001


def test_byte_order_mark_has_its_own_message(tmp_path: Path) -> None:
    f = tmp_path / "bom.json"
    f.write_bytes(HOSTILE["byte-order mark"])
    with pytest.raises(json_file.UnsafeJSONError, match="byte-order mark"):
        json_file.load(f)


def test_missing_file_is_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        json_file.load(tmp_path / "nope.json")


def test_folders_devices_and_pipes_are_refused_without_hanging(tmp_path: Path) -> None:
    with pytest.raises(json_file.UnsafeJSONError, match="not a regular file"):
        json_file.load(tmp_path)
    if os.path.exists("/dev/zero"):
        with pytest.raises(json_file.UnsafeJSONError, match="not a regular file"):
            json_file.load("/dev/zero")
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "pipe.json"
        os.mkfifo(fifo)
        result: list[BaseException | None] = []

        def attempt() -> None:
            try:
                json_file.load(fifo)
                result.append(None)
            except BaseException as exc:  # noqa: BLE001
                result.append(exc)

        t = threading.Thread(target=attempt, daemon=True)
        t.start()
        t.join(5)
        assert not t.is_alive(), "reading a named pipe with no writer hung"
        assert isinstance(result[0], json_file.UnsafeJSONError)


def test_error_messages_are_safe_to_print(tmp_path: Path) -> None:
    f = tmp_path / "evil\x1b]0;pwned\x07.json"
    f.write_bytes(b"nope")
    with pytest.raises(json_file.UnsafeJSONError) as info:
        json_file.load(f, "report")
    assert "\x1b" not in str(info.value) and "\x07" not in str(info.value)
    f.write_bytes(b'{"\x1b[2J": 1, "\x1b[2J": 2}')
    with pytest.raises(json_file.UnsafeJSONError) as info:
        json_file.load(f, "report")
    assert "\x1b" not in str(info.value)


# ---- signed reward reports ---------------------------------------------------------------

def forged_report(tmp_path: Path) -> tuple[dict, ManifestSigner, ManifestSigner]:
    """A report with changed numbers, re-hashed and signed by an attacker's key, with the
    signature block's (unsigned) fingerprint label copied from the real coordinator."""
    client, clock, _ = make(tmp_path)
    real_report = closed_period(client, clock)
    real = client.app.state.signer
    attacker = ManifestSigner.load_or_create(tmp_path / "attacker.key")
    forged = {k: v for k, v in real_report.items() if k != "signature"}
    forged["totals"] = {**forged["totals"], "payable_points": 1_000_000}
    forged["report_sha256"] = rewards.report_hash(forged)
    forged = rewards.sign_report(forged, attacker, now=T0 + 10_000)
    forged["signature"]["fingerprint"] = real.fingerprint
    return forged, real, attacker


def test_a_borrowed_fingerprint_label_is_caught(tmp_path: Path) -> None:
    """Before json-file-v1, `verify --no-db` printed the file's own fingerprint label, so a
    report signed by anyone could show the coordinator's fingerprint and pass."""
    forged, real, attacker = forged_report(tmp_path)
    problems = rewards.check_signature(forged)
    assert any("fingerprint label doesn't belong" in p for p in problems)
    # With the real key it was always refused, and still is.
    assert any("different key" in p for p in rewards.check_signature(forged,
                                                                     real.public_key_hex))
    # With an honest label it is a validly self-signed report *by the attacker*: that's
    # why the fingerprint must be compared with the coordinator's, or --public-key given.
    forged["signature"]["fingerprint"] = attacker.fingerprint
    assert rewards.check_signature(forged) == []
    assert rewards.check_signature(forged, real.public_key_hex) != []


def test_cli_prints_the_computed_fingerprint_never_the_label(tmp_path: Path, capsys) -> None:
    forged, real, attacker = forged_report(tmp_path)
    f = tmp_path / "forged.json"
    f.write_text(json.dumps(forged))
    assert rewards.main(["verify", str(f), "--no-db"]) == 1
    out = capsys.readouterr().out
    assert real.fingerprint not in out and "MISMATCH" in out
    # Label fixed to match the attacker's key: it verifies as *the attacker's* report, and
    # the printed fingerprint is the attacker's, which a person comparing would reject.
    forged["signature"]["fingerprint"] = attacker.fingerprint
    f.write_text(json.dumps(forged))
    assert rewards.main(["verify", str(f), "--no-db"]) == 0
    out = capsys.readouterr().out
    assert f"signed by {attacker.fingerprint}" in out and real.fingerprint not in out


def test_control_codes_in_the_label_are_never_printed(tmp_path: Path, capsys) -> None:
    client, clock, _ = make(tmp_path)
    rep = closed_period(client, clock)
    rep["signature"]["fingerprint"] = "\x1b]0;pwned\x07\x1b[2J"
    f = tmp_path / "r.json"
    f.write_text(json.dumps(rep))
    assert rewards.main(["verify", str(f), "--no-db"]) == 1
    captured = capsys.readouterr()
    assert "\x1b" not in captured.out + captured.err


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_verify_refuses_hostile_files_in_one_line(tmp_path: Path, name: str, capsys) -> None:
    f = tmp_path / "r.json"
    f.write_bytes(HOSTILE[name])
    for extra in (["--no-db"], ["--db", str(tmp_path / "none.sqlite3")]):
        assert rewards.main(["verify", str(f), *extra]) == 2
        err = capsys.readouterr().err
        assert err.startswith("refusing to read it:") and "Traceback" not in err
        assert err.count("\n") == 1


@pytest.mark.parametrize("doc", [[1], None, "text", 5, {"report_type": "other"}])
def test_verify_of_a_non_report_is_a_mismatch_not_a_crash(tmp_path: Path, doc, capsys) -> None:
    f = tmp_path / "r.json"
    f.write_text(json.dumps(doc))
    assert rewards.main(["verify", str(f), "--no-db"]) == 1
    assert "not a reward report" in capsys.readouterr().out
    assert rewards.check_signature(doc) == ["not a reward report"]


def test_wrong_shapes_inside_a_report_never_crash(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    rep = closed_period(client, clock)
    for field, value in (("scoring", [1]), ("scoring", "v1"), ("period", "x"),
                         ("period", {"since": 10 ** 30, "until": 10 ** 31}),
                         ("entries", "x"), ("entries", [1, None, {"job_id": "\x1b[2J"}]),
                         ("signature", {"public_key": 5, "value": [1]})):
        bad = {**rep, field: value}
        bad["report_sha256"] = rewards.report_hash(bad)  # re-hashed, so entries get compared
        problems = rewards.verify_report(bad, ledger)
        assert problems, field
        assert all("\x1b" not in p for p in problems)
        assert rewards.check_signature(bad)


def test_missing_report_file_is_one_line(tmp_path: Path, capsys) -> None:
    assert rewards.main(["verify", str(tmp_path / "nope.json"), "--no-db"]) == 2
    assert capsys.readouterr().err.startswith("report not found")


# ---- the public reports index ------------------------------------------------------------

def published(tmp_path: Path) -> tuple[dict, Path, dict, dict]:
    s = two_periods(tmp_path)
    first, second = add(s, 1), add(s, 2)
    d = s["dir"]
    assert pub.check_folder(d, s["key"]) == []
    return s, d / "index.json", first, second


def edit_index(index_path: Path, change) -> None:
    index = json.loads(index_path.read_text())
    change(index)
    index_path.write_text(json.dumps(index, indent=2))


@pytest.mark.usefixtures("pinned")
def test_numbers_shown_on_the_page_must_match_the_signed_report(tmp_path: Path) -> None:
    """The page shows rigs, results, points and scoring from index.json. Before
    json-file-v1, `check` only compared file hashes, so an edited number passed CI."""
    s, index_path, first, second = published(tmp_path)
    original = index_path.read_bytes()
    for field, value in (("payable_points", 1_000_000), ("accepted_results", 999),
                         ("rigs", 50), ("scoring_version", "sim-points-v9")):
        edit_index(index_path, lambda i: i["periods"][0].__setitem__(field, value))
        problems = pub.check_folder(s["dir"], s["key"])
        assert any(p.startswith(f"{first['id']}: the index shows {field}") for p in problems), \
            (field, problems)
        index_path.write_bytes(original)
    assert pub.check_folder(s["dir"], s["key"]) == []


@pytest.mark.usefixtures("pinned")
def test_fingerprints_and_the_coordinator_line_must_be_real(tmp_path: Path) -> None:
    s, index_path, first, second = published(tmp_path)
    original = index_path.read_bytes()
    other = ManifestSigner.load_or_create(tmp_path / "other.key")
    edits = {
        "coordinator fingerprint": lambda i: i["coordinator"].__setitem__("fingerprint",
                                                                          "0000-0000-0000-0000"),
        "coordinator other key": lambda i: i.__setitem__(
            "coordinator", {"public_key": other.public_key_hex,
                            "fingerprint": other.fingerprint}),
        "period fingerprint": lambda i: i["periods"][1]["signed_by"].__setitem__(
            "fingerprint", other.fingerprint),
        "older period fingerprint": lambda i: i["periods"][0]["signed_by"].__setitem__(
            "fingerprint", other.fingerprint),
    }
    for name, change in edits.items():
        edit_index(index_path, change)
        problems = pub.check_folder(s["dir"], s["key"])
        assert problems, name
        if name == "older period fingerprint":
            assert any("signed_by is not a valid key with its own fingerprint" in p
                       for p in problems)
        index_path.write_bytes(original)
    edit_index(index_path, lambda i: i.__setitem__("periods", []))
    assert any("'coordinator' is set" in p for p in pub.check_folder(s["dir"]))


@pytest.mark.usefixtures("pinned")
def test_index_paths_stay_inside_their_period_folder(tmp_path: Path) -> None:
    s, index_path, first, second = published(tmp_path)
    original = index_path.read_bytes()
    secret = tmp_path / "secret.json"
    secret.write_text("{}")
    for path in ("../secret.json", "/etc/passwd", f"{second['id']}/report.json",
                 f"{first['id']}/../{first['id']}/report.json"):
        edit_index(index_path, lambda i: i["periods"][0]["files"]["report"].__setitem__(
            "path", path))
        problems = pub.check_folder(s["dir"], s["key"])
        assert f"{first['id']}: index entry points at files outside its own period folder" \
            in problems, path
        index_path.write_bytes(original)


@pytest.mark.usefixtures("pinned")
def test_malformed_index_entries_are_problems_not_crashes(tmp_path: Path) -> None:
    s, index_path, first, second = published(tmp_path)
    original = index_path.read_bytes()
    edits = [
        lambda i: i["periods"].append("x"),
        lambda i: i["periods"].append(None),
        lambda i: i["periods"].append({"id": "\x1b[2J", "until": "soon"}),
        lambda i: i["periods"][0].__setitem__("until", "soon"),
        lambda i: i["periods"][0].__setitem__("rigs", True),
        lambda i: i["periods"][0].__setitem__("payable_points", 1.5),
        lambda i: i["periods"][0].__setitem__("files", "x"),
        lambda i: i["periods"][0]["files"].__setitem__("report", "x"),
        lambda i: i["periods"][0].__setitem__("signed_by", None),
        lambda i: i["periods"][0].__setitem__("extra", "<script>"),
        lambda i: i["periods"][0].pop("previous"),
        lambda i: i["periods"].append(dict(i["periods"][0])),  # listed twice
        lambda i: i.__setitem__("extra", 1),
        lambda i: i.__setitem__("notice", "Guaranteed returns!"),
        lambda i: i["periods"][1].__setitem__("previous", None),
        lambda i: i["periods"][0].__setitem__("published_at", 1),
        lambda i: i["periods"][0]["files"]["report"].__setitem__("bytes", 1),
    ]
    for n, change in enumerate(edits):
        edit_index(index_path, change)
        problems = pub.check_folder(s["dir"], s["key"])
        assert problems, n
        assert all("\x1b" not in p for p in problems), n
        index_path.write_bytes(original)
    edit_index(index_path, lambda i: i["periods"].append(dict(i["periods"][0])))
    assert f"{first['id']}: listed twice" in pub.check_folder(s["dir"], s["key"])
    index_path.write_bytes(original)
    # `add` refuses to build on a damaged index instead of crashing.
    edit_index(index_path, lambda i: i["periods"].append("x"))
    with pytest.raises(pub.PublishError, match="damaged"):
        pub.plan(s["r2"], s["c2"], s["dir"], s["key"], T0 + 10 ** 7)


@pytest.mark.usefixtures("pinned")
@pytest.mark.parametrize("name", ["deep", "duplicate key", "NaN", "bad utf-8", "not json"])
def test_hostile_index_file_is_one_problem(tmp_path: Path, name: str) -> None:
    s, index_path, _, _ = published(tmp_path)
    index_path.write_bytes(HOSTILE[name])
    problems = pub.check_folder(s["dir"], s["key"])
    assert len(problems) == 1 and problems[0].startswith("index.json is not readable")


@pytest.mark.usefixtures("pinned")
def test_a_symlinked_published_file_is_refused(tmp_path: Path) -> None:
    s, index_path, first, _ = published(tmp_path)
    report = s["dir"] / first["id"] / "report.json"
    real = tmp_path / "elsewhere.json"
    real.write_bytes(report.read_bytes())
    report.unlink()
    report.symlink_to(real)
    assert f"{first['id']}: a published file or folder is a symlink" in \
        pub.check_folder(s["dir"], s["key"])


@pytest.mark.usefixtures("pinned")
def test_a_duplicate_key_in_a_published_report_is_caught(tmp_path: Path) -> None:
    """Even with matching hashes in the index (an attacker can update those too)."""
    s, index_path, first, _ = published(tmp_path)
    report = s["dir"] / first["id"] / "report.json"
    text = report.read_text()
    doubled = text.replace('"report_type":', '"report_type": "x", "report_type":', 1)
    report.write_text(doubled)
    digest = pub._sha256(report)
    edit_index(index_path, lambda i: i["periods"][0]["files"]["report"].update(
        sha256=digest, bytes=report.stat().st_size))
    (s["dir"] / first["id"] / "SHA256SUMS").write_text(
        f"{digest}  report.json\n"
        f"{first['files']['ledger_copy']['sha256']}  ledger-copy.sqlite3\n")
    problems = pub.check_folder(s["dir"], s["key"])
    assert any("appears twice" in p for p in problems), problems


# ---- release files -----------------------------------------------------------------------

@pytest.mark.parametrize("name", ["deep", "duplicate key", "NaN", "bad utf-8"])
def test_release_files_refuse_hostile_json(tmp_path: Path, name: str) -> None:
    f = tmp_path / "signers.json"
    f.write_bytes(HOSTILE[name])
    with pytest.raises(rel.ReleaseError):
        rel.load_signers(f)
    m = tmp_path / "release.json"
    m.write_bytes(HOSTILE[name])
    with pytest.raises(rel.ReleaseError):
        rel.verify_release(m, f)


def test_release_names_with_a_trailing_newline_are_refused(tmp_path: Path) -> None:
    """`$` in a Python regex also matches before a final newline; `\\Z` doesn't."""
    assert rel.NAME_RE.match("alice")
    assert not rel.NAME_RE.match("alice\n")
    assert not rel.COMMIT_RE.match("a" * 40 + "\n")
    assert not rel.HEX64.match("a" * 64 + "\n")
    keys = [ManifestSigner.load_or_create(tmp_path / f"k{n}").public_key_hex for n in (1, 2)]
    for name in ("alice\n", "bob"):
        signers = {"format": rel.SIGNERS_FORMAT, "threshold": 2,
                   "signers": [{"name": name, "public_key": keys[0]},
                               {"name": "carol", "public_key": keys[1]}]}
        problems = rel.check_signers(signers)
        assert (any("name" in p for p in problems)) == (name == "alice\n"), (name, problems)


def test_no_dollar_anchored_patterns_in_app_code() -> None:
    """Every hand-written `^...$` pattern now ends in `\\Z`. Guard so a new one can't
    quietly accept a trailing newline (a log line break, a name, a hash)."""
    offenders = []
    for f in sorted(APP.glob("*.py")) + [APP.parent / "demo_worker.py"]:
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r're\.(compile|match|fullmatch|search)\(r?"\^[^"]*\$"', line):
                offenders.append(f"{f.name}:{n}")
    assert offenders == []


def test_log_values_with_a_line_break_are_quoted() -> None:
    assert _PLAIN_TEXT.match("abc")
    assert not _PLAIN_TEXT.match("abc\n")

