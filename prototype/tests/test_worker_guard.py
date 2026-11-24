"""A hostile coordinator, or someone in the middle, against the worker (`worker-input-v1`).

The rule for every reply, however broken: the worker refuses it with a plain message or a
clean stop (SystemExit), never a crash with a stack trace, never prints terminal control
codes, never reads more than the size limit, never saves a malformed key, and never runs a
job it hasn't verified. Most tests use an in-memory fake coordinator (httpx.MockTransport).
"""
from __future__ import annotations

import gzip
import json
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx
import pytest

import demo_worker
from app import schemas
from app.security import sha256_text
from app.signing import ManifestSigner
from app.worker_guard import (MAX_JSON_DEPTH, MAX_REFUSAL_WAIT_SECONDS, MAX_RESPONSE_BYTES,
                              CoordinatorError, check_job_id, check_url, check_worker_key,
                              refusal_wait, request_json, say)
from demo_worker import ManifestError, check_manifest

T0 = 1_800_000_000
GOOD_KEY = "bnhw_" + "A" * 43
ESC_TITLE = "\x1b]0;you have been pwned\x07"  # sets the terminal window title
ESC_CLEAR = "\x1b[2J\x1b[H"                   # clears the screen
FAKE_PROMPT = "\r\nroot@rig:~# "               # draws a fake prompt on a new line
RTL = "‮"                                # flips the text direction
SNEAKY = ESC_TITLE + ESC_CLEAR + FAKE_PROMPT + RTL + "\x9b31m" + "​" + "\x7f"


def printable(text: str) -> bool:
    return all(ch.isprintable() or ch == "\n" for ch in text)


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://coord.test")


def always(response: httpx.Response) -> httpx.Client:
    return client_for(lambda request: response)


def signed_job(signer: ManifestSigner, **override: Any) -> dict[str, Any]:
    fields = {"job_id": str(uuid.uuid4()), "task_type": "demo:sha256", "payload": "hi boats",
              "payload_hash": sha256_text("hi boats"), "reward_points": 1,
              "expires_at": T0 + 600, "schema_version": schemas.JOB_SCHEMA_VERSION,
              "model_sha256": None}
    fields.update(override)
    if "payload" in override and "payload_hash" not in override:
        fields["payload_hash"] = sha256_text(override["payload"])
    return fields | {"signature": signer.sign(fields)}


@pytest.fixture
def signer(tmp_path: Path) -> ManifestSigner:
    return ManifestSigner.load_or_create(tmp_path / "coordinator.key")


# --- say(): text from the coordinator is safe to print ------------------------------------

def test_say_turns_every_control_and_invisible_character_into_a_question_mark() -> None:
    out = say("job " + SNEAKY + " done")
    assert printable(out) and "\x1b" not in out and "\r" not in out and "\n" not in out
    assert out.startswith("job ") and out.endswith(" done")
    assert say("plain text, émoji 🚤 ok") == "plain text, émoji 🚤 ok"  # honest text untouched


def test_say_cuts_long_text_and_handles_anything() -> None:
    assert say("x" * 10_000) == "x" * 200 + "..."
    assert say("abc", limit=2) == "ab..."
    assert say(None) == "None" and say(["\x1b"]) == "['\\x1b']"  # repr already escapes
    assert say("\ud800") == "?"  # a lone surrogate can't even be encoded; still safe


# --- check_url(): no worker key over plain http to another machine ------------------------

@pytest.mark.parametrize("url", ["https://coord.example.org", "https://10.0.0.5:8443/",
                                 "http://127.0.0.1:8000", "http://localhost:8000",
                                 "http://[::1]:8000", "http://127.9.9.9"])
def test_https_anywhere_and_plain_http_to_this_machine_are_fine(url: str) -> None:
    assert check_url(url) == url


@pytest.mark.parametrize("url", ["http://coord.example.org", "http://192.168.1.20:8000",
                                 "http://127.0.0.1.evil.example", "http://localhost.evil.example",
                                 "http://0.0.0.0:8000"])
def test_plain_http_to_another_machine_is_refused_unless_the_owner_opts_in(url: str) -> None:
    with pytest.raises(CoordinatorError, match="unencrypted"):
        check_url(url)
    assert check_url(url, allow_plain_http=True) == url


@pytest.mark.parametrize("url", ["ftp://coord.example.org", "file:///etc/passwd", "coord.example",
                                 "https://", "https://user:pw@coord.example.org",
                                 "https://coord.example.org:99999", "javascript:alert(1)"])
def test_odd_urls_are_refused_even_with_the_opt_in(url: str) -> None:
    with pytest.raises(CoordinatorError):
        check_url(url, allow_plain_http=True)


def test_worker_stops_before_connecting_to_a_plain_http_address(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(demo_worker.httpx, "Client", lambda **kw: pytest.fail("connected"))
    with pytest.raises(SystemExit, match="unencrypted"):
        demo_worker.main(["--url", "http://203.0.113.7:8000", "--once"])


# --- request_json(): size, time, compression and JSON shape ------------------------------

class Body:
    """A streamed reply body that records how much of it was read."""

    def __init__(self, chunks: int, size: int = 64 * 1024) -> None:
        self.chunks, self.size, self.served = chunks, size, 0

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(self.chunks):
            self.served += 1
            yield b" " * self.size


def test_reply_that_says_it_is_too_big_is_refused_without_reading_a_byte() -> None:
    body = Body(chunks=200)
    reply = httpx.Response(200, headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)},
                           content=body)
    with pytest.raises(CoordinatorError, match="too big"):
        request_json(always(reply), "GET", "/v1/coordinator/key")
    assert body.served == 0


def test_reply_without_a_size_is_cut_off_at_the_limit() -> None:
    body = Body(chunks=10_000)  # would be 640 MB
    with pytest.raises(CoordinatorError, match="too big"):
        request_json(always(httpx.Response(200, content=body)), "GET", "/x", max_bytes=1 << 20)
    assert body.served == 17  # 16 chunks fit in 1 MiB; the 17th crossed it and reading stopped


def test_compressed_replies_are_refused_so_a_small_zip_cannot_unpack_into_gigabytes() -> None:
    bomb = gzip.compress(b"{" + b" " * 50_000_000 + b"}")  # ~50 KB that unpacks to 50 MB
    reply = httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=bomb)
    with pytest.raises(CoordinatorError, match="compressed"):
        request_json(always(reply), "GET", "/x")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Accept-Encoding"])
        return httpx.Response(200, json={})
    request_json(client_for(handler), "GET", "/x")
    assert seen == ["identity"]  # and it asks for an uncompressed reply up front


def test_a_reply_dripping_in_too_slowly_is_given_up() -> None:
    ticks = iter(range(0, 10_000, 7))  # every chunk arrives 7 "seconds" after the last

    with pytest.raises(CoordinatorError, match="too slow"):
        request_json(always(httpx.Response(200, content=Body(chunks=100, size=1))), "GET", "/x",
                     deadline=60, clock=lambda: next(ticks))


@pytest.mark.parametrize("raw", [
    b"<html>502 Bad Gateway</html>", b'{"job_id":', b"\xff\xfe{}", b"[" * 100_000,
    b'{"a":' * (MAX_JSON_DEPTH + 1) + b"1" + b"}" * (MAX_JSON_DEPTH + 1),
    b'{"n": NaN}', b'{"n": Infinity}', b'{"n": -Infinity}', b'{"n": ' + b"9" * 5000 + b"}",
    b'{"n": 1e400}', b'{"n": [-1e999]}',  # would become infinity (rig-report-v1)
    b"[]", b'"text"', b"42", b"null", b"true", b"",
])
def test_ok_replies_that_are_not_a_plain_json_object_are_refused(raw: bytes) -> None:
    with pytest.raises(CoordinatorError):
        request_json(always(httpx.Response(200, content=raw)), "GET", "/x")


@pytest.mark.parametrize("raw", [b"<html>oops</html>", b"[" * 100_000, b"\xff", b""])
def test_error_replies_with_junk_bodies_are_just_errors(raw: bytes) -> None:
    reply = request_json(always(httpx.Response(502, content=raw)), "GET", "/x")
    assert reply.status_code == 502 and reply.data is None and reply.detail() == "(no details)"


def test_error_details_are_made_safe_to_print() -> None:
    reply = request_json(always(httpx.Response(403, json={"detail": SNEAKY * 50})), "GET", "/x")
    assert printable(reply.detail()) and len(reply.detail()) <= 203
    reply = request_json(always(httpx.Response(422, json={"detail": [{"msg": SNEAKY}]})),
                         "GET", "/x")
    assert printable(reply.detail())


def test_redirects_are_not_followed_and_network_errors_are_plain() -> None:
    reply = request_json(always(httpx.Response(302, headers={"Location": "https://evil.test/"})),
                         "GET", "/x")
    assert reply.status_code == 302

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")
    with pytest.raises(CoordinatorError, match="could not reach"):
        request_json(client_for(unreachable), "GET", "/x")


# --- shapes of what the worker saves or puts in a URL ------------------------------------

def test_only_a_worker_key_in_the_coordinators_exact_shape_is_accepted() -> None:
    assert check_worker_key(GOOD_KEY) == GOOD_KEY
    for bad in [None, 42, "", "bnhw_short", GOOD_KEY + "\n", GOOD_KEY + "\nexport X=1",
                "bnhe_" + "A" * 43, "bnhw_" + "A" * 129, "bnhw_" + "é" * 43, ["bnhw_"]]:
        with pytest.raises(CoordinatorError):
            check_worker_key(bad)


def test_job_ids_that_could_change_the_url_are_refused() -> None:
    assert check_job_id(str(uuid.uuid4()))
    for bad in ["../workers/register", "a/b", "a?b=1", "a#b", "", "x" * 81, ".", "%2e%2e",
                "a b", None, 7]:
        with pytest.raises(CoordinatorError):
            check_job_id(bad)


def test_refused_jobs_make_the_worker_wait_longer_each_time() -> None:
    assert [refusal_wait(n) for n in range(0, 8)] == [0, 5, 10, 20, 40, 60, 60, 60]
    assert refusal_wait(10**9) == MAX_REFUSAL_WAIT_SECONDS


# --- check_manifest(): any malformed job is a ManifestError, never a crash ----------------

HOSTILE_VALUES: list[Any] = [None, True, 0, -1, 2**70, 1.5, float("nan"), "", SNEAKY,
                             "x" * 100_000, "\ud800", [], ["a"], {}, {"a": 1}]


def test_any_job_that_is_not_an_object_is_refused(signer: ManifestSigner) -> None:
    for bad in [None, [], [signed_job(signer)], "job", 7, 1.5, True]:
        with pytest.raises(ManifestError, match="not a job object"):
            check_manifest(bad, signer.public_key_hex, now=T0)


def test_every_field_of_an_unsigned_job_can_be_hostile_without_a_crash(
        signer: ManifestSigner) -> None:
    job = signed_job(signer)
    check_manifest(job, signer.public_key_hex, now=T0)  # the honest one passes
    tried = 0
    for field in list(job):
        for value in HOSTILE_VALUES:
            if value is None and job[field] is None:
                continue  # model_sha256 None is the honest value for a task without a model
            with pytest.raises(ManifestError) as caught:
                check_manifest(job | {field: value}, signer.public_key_hex, now=T0)
            assert printable(demo_worker.say(str(caught.value)))
            tried += 1
    assert tried == len(job) * len(HOSTILE_VALUES) - 1


def test_even_a_correctly_signed_job_must_have_a_sane_shape(signer: ManifestSigner) -> None:
    """The coordinator is trusted, but a stolen or buggy signing key shouldn't be able to
    point the worker's submit at another URL or crash it either."""
    cases = {"job_id": ["../workers/register", "a/b", SNEAKY],
             "payload": [["list"], 5], "task_type": [["demo:sha256"]],
             "expires_at": ["soon", 1.5e12, None, True]}
    for field, values in cases.items():
        for value in values:
            override = {field: value}
            if field == "payload":
                override["payload_hash"] = "x"
            job = signed_job(signer, **override)
            with pytest.raises(ManifestError):
                check_manifest(job, signer.public_key_hex, now=T0)


# --- the worker's own flows against a hostile coordinator --------------------------------

HOSTILE_REPLIES: list[Callable[[], httpx.Response]] = [
    lambda: httpx.Response(200, content=b"<html>hi</html>"),
    lambda: httpx.Response(200, content=b"[" * 200_000),
    lambda: httpx.Response(200, content=b'{"public_key": NaN, "worker_key": NaN}'),
    lambda: httpx.Response(200, content=b'{"x": ' + b"7" * 6000 + b"}"),
    lambda: httpx.Response(200, content=b"\xff\xfe\xfd"),
    lambda: httpx.Response(200, json=[1, 2, 3]),
    lambda: httpx.Response(200, json="just text"),
    lambda: httpx.Response(200, json={}),
    lambda: httpx.Response(200, json={"public_key": SNEAKY, "worker_key": SNEAKY,
                                      "rotations": SNEAKY, "handover_key": SNEAKY,
                                      "job_id": SNEAKY, "detail": SNEAKY}),
    lambda: httpx.Response(200, json={"public_key": ["a"], "worker_key": {"k": 1},
                                      "rotations": {"a": 1}, "handover_key": [None]}),
    lambda: httpx.Response(200, content=Body(chunks=200)),  # 12.5 MB, no size given
    lambda: httpx.Response(200, headers={"Content-Encoding": "gzip"},
                           content=gzip.compress(b"{}")),
    lambda: httpx.Response(403, json={"detail": SNEAKY}),
    lambda: httpx.Response(500, content=b"Internal Server Error " + SNEAKY.encode()),
    lambda: httpx.Response(422, json={"detail": [{"loc": ["body"], "msg": SNEAKY}]}),
    lambda: httpx.Response(503, headers={"Retry-After": "1e999"}, json={"detail": SNEAKY}),
]


def run_quietly(action: Callable[[], Any], capsys: pytest.CaptureFixture[str]) -> str:
    """Run one worker step: it may return or stop cleanly, nothing else. Returns all output."""
    try:
        action()
    except SystemExit as stop:
        assert printable(str(stop.code)), stop.code
        print(stop.code)
    out = capsys.readouterr()
    assert printable(out.out) and printable(out.err), out
    return out.out


@pytest.mark.parametrize("make_reply", HOSTILE_REPLIES)
def test_pinning_the_coordinator_key_survives_any_reply(
        make_reply: Callable[[], httpx.Response], tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    pin = tmp_path / "coordinator.pub"
    run_quietly(lambda: demo_worker.load_or_pin_coordinator_key(always(make_reply()), pin),
                capsys)
    assert not pin.exists()  # nothing hostile is ever pinned


@pytest.mark.parametrize("make_reply", HOSTILE_REPLIES)
def test_enrolling_survives_any_reply_and_saves_no_bad_key(
        make_reply: Callable[[], httpx.Response], tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    key_file = tmp_path / "worker.key"
    run_quietly(lambda: demo_worker.load_or_enroll(always(make_reply()), "rig-1", key_file,
                                                   "bnhe_code", ["demo:sha256"]), capsys)
    assert not key_file.exists()


def test_a_well_formed_key_is_still_saved(tmp_path: Path) -> None:
    key_file = tmp_path / "worker.key"
    reply = httpx.Response(200, json={"worker_id": "rig-1", "worker_key": GOOD_KEY})
    assert demo_worker.load_or_enroll(always(reply), "rig-1", key_file, "bnhe_code",
                                      ["demo:sha256"]) == GOOD_KEY
    assert key_file.read_text().strip() == GOOD_KEY


@pytest.mark.parametrize("make_reply", HOSTILE_REPLIES)
def test_claiming_a_job_survives_any_reply(
        make_reply: Callable[[], httpx.Response], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        signer: ManifestSigner) -> None:
    ran: list[Any] = []
    monkeypatch.setattr(demo_worker, "execute", lambda job: ran.append(job) or "x")
    monkeypatch.setattr(demo_worker, "pause", lambda s: None)
    worker_main(tmp_path, monkeypatch, signer, lambda path: make_reply(), capsys, ["--once"])
    assert ran == []  # nothing hostile is ever run


def worker_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signer: ManifestSigner,
                claim: Callable[[str], httpx.Response], capsys: pytest.CaptureFixture[str],
                extra: list[str]) -> str:
    """Run demo_worker.main() against a fake coordinator whose `/v1/jobs/next` (and submit)
    replies come from `claim`. Registration works; the coordinator key is given up front."""
    key_file = tmp_path / "worker.key"
    key_file.write_text(GOOD_KEY + "\n")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/workers/register":
            return httpx.Response(200, json={"worker_id": "rig-1"})
        return claim(request.url.path)

    real_client = httpx.Client
    monkeypatch.setattr(demo_worker.httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    monkeypatch.chdir(tmp_path)
    return run_quietly(lambda: demo_worker.main([
        "--url", "https://coord.test", "--worker-id", "rig-1", "--key-file", str(key_file),
        "--coordinator-key", signer.public_key_hex, *extra]), capsys)


def test_a_worker_handed_only_bad_jobs_slows_down_instead_of_spinning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        signer: ManifestSigner) -> None:
    waits: list[float] = []
    monkeypatch.setattr(demo_worker, "pause", waits.append)
    other = ManifestSigner.load_or_create(tmp_path / "impostor.key")
    claims = iter([httpx.Response(200, json=signed_job(other, job_id=f"j{i}")) for i in range(6)]
                  + [httpx.Response(404, json={"detail": "No queued jobs"})])
    out = worker_main(tmp_path, monkeypatch, signer, lambda path: next(claims), capsys, [])
    assert waits == [5, 10, 20, 40, 60, 60]
    assert out.count("bad signature") == 6 and "No queued jobs." in out


def test_an_honest_job_still_runs_and_is_submitted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        signer: ManifestSigner) -> None:
    monkeypatch.setattr(demo_worker.time, "time", lambda: T0)
    job = signed_job(signer)
    submitted: list[str] = []

    def claim(path: str) -> httpx.Response:
        if path.endswith("/submit"):
            submitted.append(path)
            return httpx.Response(200, json={"status": "accepted", "note": SNEAKY})
        return httpx.Response(200, json=job)
    out = worker_main(tmp_path, monkeypatch, signer, claim, capsys, ["--once"])
    assert submitted == [f"/v1/jobs/{job['job_id']}/submit"]
    assert out.startswith("200 ") and "accepted" in out
