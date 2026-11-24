"""Fetching and pinning model files safely (`model-fetch-v1`, code audit seventh pass).

Pinning the first model is the next step on the rig, and the file comes from the internet.
Downloads here go through httpx's MockTransport, so every hostile server behaviour can be
played back without a network: oversized and endless replies, lying sizes, redirects to
plain http, compressed replies, slow drips, broken connections. Nothing is downloaded for
real; no model runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import namedtuple
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx
import pytest

from app import model_registry as mr
from app import tasks

MODEL_ID = "tiny-test-model"
URL = "https://models.example/tiny.gguf"
BODY = b"GGUF fake weights for download tests " * 100
SHA = hashlib.sha256(BODY).hexdigest()


def entry_dict(**overrides: Any) -> dict[str, Any]:
    return {"model_id": MODEL_ID, "file_name": "tiny.gguf", "source_url": URL,
            "license": "Apache-2.0", "sha256": None, "size_bytes": None,
            "min_vram_mb": 1} | overrides


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"_notice": "keep me", "models": [entry_dict()]}, indent=2))
    monkeypatch.setenv("MODEL_REGISTRY", str(reg))
    return reg


def write_registry(reg: Path, *models: dict[str, Any], **top: Any) -> None:
    reg.write_text(json.dumps({"models": list(models)} | top))


def one_line(text: str) -> bool:
    return "\n" not in text and all(ch.isprintable() for ch in text)


# ----------------------------------------------------------------------- the registry

def test_shipped_registry_passes_every_check() -> None:
    models = mr.load(mr.DEFAULT_REGISTRY)
    assert models and all(m.source_url.startswith("https://") for m in models.values())


BAD_FIELDS = [
    ("file_name", "../../.bashrc"), ("file_name", "/etc/passwd"), ("file_name", "a/b.gguf"),
    ("file_name", "model.bin"), ("file_name", ".hidden.gguf"), ("file_name", "x\x1b[2J.gguf"),
    ("file_name", "tiny.gguf\n"), ("file_name", "tiny.gguf.part"), ("file_name", 7),
    ("source_url", "http://models.example/t.gguf"), ("source_url", "ftp://models.example/t"),
    ("source_url", "https://user:pw@models.example/t"), ("source_url", "https://"),
    ("source_url", "https://models.example:99999/t"), ("source_url", "https://a b/t"),
    ("source_url", "file:///etc/passwd"), ("source_url", None), ("upstream", "http://x.example"),
    ("license", ""), ("license", "MIT\nfake line"), ("license", ["MIT"]),
    ("model_id", "../x"), ("model_id", "UPPER"), ("model_id", ""),
    ("sha256", "A" * 64), ("sha256", "a" * 63), ("sha256", 5), ("size_bytes", True),
    ("size_bytes", 0), ("size_bytes", "400"), ("size_bytes", mr.MAX_MODEL_BYTES + 1),
    ("min_vram_mb", "2048"), ("min_vram_mb", -1), ("approx_size_mb", 1.5),
    ("notes", "ok\x1b]0;owned\x07"), ("runtime", ""), ("sha265", "a" * 64),
]


@pytest.mark.parametrize("field,value", BAD_FIELDS,
                         ids=[f"{f}={v!r}"[:40] for f, v in BAD_FIELDS])
def test_registry_with_a_bad_field_is_refused_in_one_line(
        registry: Path, field: str, value: Any) -> None:
    write_registry(registry, entry_dict(**{field: value}))
    with pytest.raises(mr.RegistryError) as err:
        mr.load()
    assert one_line(str(err.value))


@pytest.mark.parametrize("models,top", [
    ([entry_dict(sha256="a" * 64)], {}),                         # sha256 without a size
    ([entry_dict(size_bytes=10)], {}),                           # size without a sha256
    ([entry_dict(), entry_dict(file_name="other.gguf")], {}),    # same model id twice
    ([entry_dict(), entry_dict(model_id="b", file_name="TINY.gguf")], {}),  # same file
    ([{k: v for k, v in entry_dict().items() if k != "license"}], {}),       # missing field
    (["not an object"], {}),
    ([entry_dict()], {"extra": 1}),
    ([entry_dict()] * (mr.MAX_MODELS + 1), {}),
], ids=["sha-only", "size-only", "dup-id", "dup-file", "missing", "not-object",
        "top-extra", "too-many"])
def test_registry_shape_problems_are_refused(registry: Path, models: list, top: dict) -> None:
    write_registry(registry, *models, **top)
    with pytest.raises(mr.RegistryError):
        mr.load()


def test_registry_with_a_key_written_twice_is_refused(registry: Path) -> None:
    registry.write_text('{"models": [{"model_id": "a", "model_id": "b", "file_name": '
                        '"t.gguf", "source_url": "https://x.example/t", "license": "MIT"}]}')
    with pytest.raises(mr.RegistryError, match="twice"):
        mr.load()


def test_registry_comment_fields_and_optional_fields_are_fine(registry: Path) -> None:
    write_registry(registry, entry_dict(_comment="why", upstream=None, approx_size_mb=None,
                                        runtime="llama.cpp", notes="fine"), _notice="hi")
    assert MODEL_ID in mr.load()


def test_cli_with_a_broken_registry_says_one_line(
        registry: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_registry(registry, entry_dict(file_name="../../.bashrc"))
    assert mr.main(["list"]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "Traceback" not in err and "file_name" in err


def test_broken_registry_refuses_jobs_instead_of_crashing(registry: Path) -> None:
    write_registry(registry, entry_dict(sha256="a" * 64))
    executor = tasks.get("llm:generate")
    with pytest.raises(tasks.TaskError, match="model registry refused"):
        executor.validate(json.dumps({"model_id": MODEL_ID, "prompt": "hi"}))
    ok, why = executor.available()
    assert not ok


def test_traversal_file_name_never_reaches_the_model_folder(
        registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Before model-fetch-v1, `_model_path` joined MODEL_DIR and this name unchecked.
    write_registry(registry, entry_dict(file_name="../secret.gguf"))
    monkeypatch.setenv("MODEL_DIR", str(tmp_path / "models"))
    with pytest.raises(tasks.TaskError):
        tasks.get("llm:generate").validate(json.dumps({"model_id": MODEL_ID, "prompt": "x"}))


# ------------------------------------------------------------------- hashing and pins

def test_a_pipe_or_folder_given_as_the_model_is_refused_not_hung(
        registry: Path, tmp_path: Path) -> None:
    fifo = tmp_path / "pipe.gguf"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="not a regular file"):
        mr.hash_file(fifo)
    with pytest.raises(ValueError, match="not a regular file"):
        mr.pin(MODEL_ID, fifo)
    entry = mr.ModelEntry(MODEL_ID, "tiny.gguf", URL, "MIT", SHA, len(BODY), 0, {})
    assert mr.verify_file(entry, fifo) == (False, mr.verify_file(entry, fifo)[1])
    assert not mr.verify_file(entry, fifo)[0]
    assert not mr.verify_file(entry, tmp_path)[0]
    assert mr.verify_file(entry, tmp_path / "missing.gguf")[1].startswith("model file not found")


def test_a_linked_model_file_still_works(registry: Path, tmp_path: Path) -> None:
    # Keeping models on a bigger disk behind a link is normal; the hash is what's trusted.
    real = tmp_path / "big-disk.gguf"
    real.write_bytes(BODY)
    link = tmp_path / "tiny.gguf"
    link.symlink_to(real)
    mr.pin(MODEL_ID, link, expect_sha256=SHA)
    assert mr.verify_file(mr.load()[MODEL_ID], link) == (True, "ok")


def test_pin_refuses_a_file_that_is_not_the_expected_one(registry: Path, tmp_path: Path) -> None:
    f = tmp_path / "tiny.gguf"
    f.write_bytes(BODY + b"!")
    before = registry.read_bytes()
    with pytest.raises(ValueError, match="not the expected"):
        mr.pin(MODEL_ID, f, expect_sha256=SHA)
    assert registry.read_bytes() == before


def test_pin_takes_the_hash_as_model_pages_show_it(registry: Path, tmp_path: Path) -> None:
    f = tmp_path / "tiny.gguf"
    f.write_bytes(BODY)
    mr.pin(MODEL_ID, f, expect_sha256=f"  SHA256:{SHA.upper()} ")
    assert mr.load()[MODEL_ID].sha256 == SHA
    with pytest.raises(ValueError, match="64 hex"):
        mr.pin(MODEL_ID, f, expect_sha256="abc")


def test_changing_a_pin_needs_replace(registry: Path, tmp_path: Path) -> None:
    a, b = tmp_path / "a.gguf", tmp_path / "b.gguf"
    a.write_bytes(BODY)
    b.write_bytes(b"other weights")
    mr.pin(MODEL_ID, a)
    mr.pin(MODEL_ID, a)  # the same file again is fine
    before = registry.read_bytes()
    with pytest.raises(ValueError, match="--replace"):
        mr.pin(MODEL_ID, b)
    assert registry.read_bytes() == before
    mr.pin(MODEL_ID, b, replace=True)
    assert mr.load()[MODEL_ID].sha256 == hashlib.sha256(b"other weights").hexdigest()


def test_pin_keeps_the_rest_of_the_registry(registry: Path, tmp_path: Path) -> None:
    f = tmp_path / "tiny.gguf"
    f.write_bytes(BODY)
    mr.pin(MODEL_ID, f)
    data = json.loads(registry.read_text())
    assert data["_notice"] == "keep me" and list(data) == ["_notice", "models"]
    assert list(data["models"][0]) == list(entry_dict())


def test_a_crash_while_pinning_leaves_the_old_registry(
        registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "tiny.gguf"
    f.write_bytes(BODY)
    before = registry.read_bytes()

    def crash(*_a: Any) -> None:
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(mr.os, "replace", crash)
    with pytest.raises(OSError):
        mr.pin(MODEL_ID, f)
    assert registry.read_bytes() == before
    assert sorted(p.name for p in registry.parent.iterdir()) == ["registry.json", "tiny.gguf"]


def test_cli_pin_needs_the_published_hash_or_an_explicit_opt_out(
        registry: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "tiny.gguf"
    f.write_bytes(BODY)
    with pytest.raises(SystemExit):
        mr.main(["pin", MODEL_ID, str(f)])
    capsys.readouterr()
    assert mr.main(["pin", MODEL_ID, str(f), "--expect-sha256", "b" * 64]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "not pinned" in err and not mr.load()[MODEL_ID].pinned
    assert mr.main(["pin", MODEL_ID, str(f), "--no-expected-hash"]) == 0
    assert "not compared" in capsys.readouterr().out
    assert mr.main(["pin", MODEL_ID, str(tmp_path / "nope.gguf"), "--no-expected-hash"]) == 2
    assert mr.main(["pin", "no-such-model", str(f), "--no-expected-hash"]) == 2
    other = tmp_path / "other.gguf"
    other.write_bytes(b"x")
    assert mr.main(["pin", MODEL_ID, str(other), "--no-expected-hash"]) == 2
    assert mr.main(["pin", MODEL_ID, str(other), "--no-expected-hash", "--replace"]) == 0


# ---------------------------------------------------------------------------- download

Seen = namedtuple("Seen", "requests body_read")


def server(routes: dict[str, Callable[[httpx.Request], httpx.Response]]) -> tuple[httpx.Client, list]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        route = routes.get(str(request.url))
        if route is None:
            return httpx.Response(404)
        return route(request)
    return httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False), seen


def ok(body: bytes = BODY, **headers: str) -> Callable[[httpx.Request], httpx.Response]:
    """A plain 200 reply, streamed like a real network reply (bytes content would count as
    already read)."""
    return lambda _r: httpx.Response(200, content=Tracked(body, 1),
                                     headers={"content-length": str(len(body))} | headers)


class Tracked:
    """A streamed body that records how much of it was read."""

    def __init__(self, chunk: bytes, count: int | None) -> None:
        self.chunk, self.count, self.sent = chunk, count, 0

    def __iter__(self) -> Iterator[bytes]:
        i = 0
        while self.count is None or i < self.count:
            self.sent += len(self.chunk)
            yield self.chunk
            i += 1


def entry(pinned: bool = False, **raw: Any) -> mr.ModelEntry:
    return mr.ModelEntry(MODEL_ID, "tiny.gguf", URL, "Apache-2.0",
                         SHA if pinned else None, len(BODY) if pinned else None, 1,
                         {"approx_size_mb": 1} | raw)


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_pinned_download_lands_checked_with_nothing_left_over(tmp_path: Path) -> None:
    client, seen = server({URL: ok()})
    got = mr.download(entry(pinned=True), tmp_path / "m", client=client)
    assert got.checked_against == "pin" and got.sha256 == SHA and got.size_bytes == len(BODY)
    assert (tmp_path / "m" / "tiny.gguf").read_bytes() == BODY
    assert leftovers(tmp_path / "m") == ["tiny.gguf"]
    assert stat.S_IMODE((tmp_path / "m" / "tiny.gguf").stat().st_mode) == 0o644
    assert seen[0].headers["accept-encoding"] == "identity"


def test_unpinned_download_says_what_it_was_checked_against(tmp_path: Path) -> None:
    client, _ = server({URL: ok()})
    assert mr.download(entry(), tmp_path / "a", client=client,
                       expect_sha256=SHA).checked_against == "expected"
    assert mr.download(entry(), tmp_path / "b", client=client).checked_against == "nothing"


def test_report_says_plainly_when_nothing_was_checked(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for how, words in (("nothing", "NOT CHECKED"), ("expected", "matches --expect-sha256"),
                       ("pin", "matches the pin")):
        mr.report_download(mr.Downloaded(tmp_path / "t.gguf", SHA, 1, how))
        assert words in capsys.readouterr().out


@pytest.mark.parametrize("pinned,body,expect", [
    (True, BODY[:-1] + b"?", None),          # same size, other bytes
    (False, BODY + b"!", SHA),               # not what the model page says
])
def test_a_file_with_the_wrong_hash_is_deleted(tmp_path: Path, pinned: bool, body: bytes,
                                               expect: str | None) -> None:
    client, _ = server({URL: ok(body)})
    with pytest.raises(mr.DownloadError, match="does not match"):
        mr.download(entry(pinned=pinned), tmp_path / "m", client=client, expect_sha256=expect)
    assert leftovers(tmp_path / "m") == []


def test_expected_hash_that_contradicts_the_pin_stops_before_asking(tmp_path: Path) -> None:
    client, seen = server({URL: ok()})
    with pytest.raises(mr.DownloadError, match="differs from the pin"):
        mr.download(entry(pinned=True), tmp_path / "m", client=client, expect_sha256="c" * 64)
    assert seen == []


def test_declared_size_over_the_cap_is_refused_before_reading(tmp_path: Path) -> None:
    body = Tracked(b"x" * 1024, 10)
    client, _ = server({URL: lambda _r: httpx.Response(
        200, content=body, headers={"content-length": str(10 * 1024 ** 3)})})
    with pytest.raises(mr.DownloadError, match="over the"):
        mr.download(entry(), tmp_path / "m", client=client)
    assert body.sent == 0 and leftovers(tmp_path / "m") == []


def test_pinned_download_with_another_declared_size_is_refused_before_reading(
        tmp_path: Path) -> None:
    body = Tracked(BODY, 1)
    client, _ = server({URL: lambda _r: httpx.Response(
        200, content=body, headers={"content-length": str(len(BODY) + 1)})})
    with pytest.raises(mr.DownloadError, match="the pin says"):
        mr.download(entry(pinned=True), tmp_path / "m", client=client)
    assert body.sent == 0


@pytest.mark.parametrize("pinned", [False, True])
def test_endless_reply_without_a_size_is_cut_off(tmp_path: Path, pinned: bool) -> None:
    body = Tracked(b"x" * 4096, None)  # never ends
    client, _ = server({URL: lambda _r: httpx.Response(200, content=body)})
    with pytest.raises(mr.DownloadError, match="limit"):
        mr.download(entry(pinned=pinned), tmp_path / "m", client=client, max_bytes=64 * 1024)
    # httpx hands over the body in chunks of up to CHUNK (1 MiB): at most that much past
    # the cap is read into memory, and nothing past the cap is written.
    cap = len(BODY) if pinned else 64 * 1024
    assert body.sent <= cap + mr.CHUNK + 4096
    assert leftovers(tmp_path / "m") == []


def test_unpinned_cap_follows_the_registry_size() -> None:
    assert mr.download_cap(entry(approx_size_mb=400)) == 800 * 1024 ** 2
    assert mr.download_cap(entry(approx_size_mb=1)) == mr.MIN_UNPINNED_CAP_BYTES
    assert mr.download_cap(entry(approx_size_mb=None)) == mr.MAX_MODEL_BYTES
    assert mr.download_cap(entry(approx_size_mb=10 ** 6)) == mr.MAX_MODEL_BYTES
    assert mr.download_cap(entry(), max_bytes=10) == 10
    assert mr.download_cap(entry(pinned=True), max_bytes=10 ** 12) == len(BODY)


def test_a_reply_shorter_than_it_said_is_refused(tmp_path: Path) -> None:
    client, _ = server({URL: lambda _r: httpx.Response(
        200, content=Tracked(BODY[:100], 1), headers={"content-length": str(len(BODY))})})
    with pytest.raises(mr.DownloadError, match="ended early"):
        mr.download(entry(), tmp_path / "m", client=client)
    assert leftovers(tmp_path / "m") == []


@pytest.mark.parametrize("value", ["abc", "-1", "1e9", "9" * 30, " "])
def test_a_bad_content_length_is_refused(tmp_path: Path, value: str) -> None:
    client, _ = server({URL: lambda _r: httpx.Response(
        200, content=Tracked(BODY, 1), headers={"content-length": value})})
    with pytest.raises(mr.DownloadError):
        mr.download(entry(), tmp_path / "m", client=client)


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "GZIP"])
def test_compressed_replies_are_refused(tmp_path: Path, encoding: str) -> None:
    client, _ = server({URL: ok(**{"content-encoding": encoding})})
    with pytest.raises(mr.DownloadError, match="compressed"):
        mr.download(entry(), tmp_path / "m", client=client)


def redirect(to: str, code: int = 302) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(code, headers={"location": to})


def test_https_redirects_are_followed_relative_ones_too(tmp_path: Path) -> None:
    cdn = "https://cdn.example/blob/tiny"
    client, seen = server({URL: redirect("/step"), "https://models.example/step":
                           redirect(cdn, 307), cdn: ok()})
    got = mr.download(entry(pinned=True), tmp_path / "m", client=client)
    assert got.checked_against == "pin" and [str(r.url) for r in seen][-1] == cdn


@pytest.mark.parametrize("to", ["http://cdn.example/tiny", "https://me:pw@cdn.example/t",
                                "ftp://cdn.example/t", "file:///etc/passwd"])
def test_redirects_off_https_are_refused(tmp_path: Path, to: str) -> None:
    client, seen = server({URL: redirect(to), to: ok()})
    with pytest.raises(mr.DownloadError, match="redirect") as err:
        mr.download(entry(), tmp_path / "m", client=client)
    assert one_line(str(err.value)) and len(seen) == 1
    assert leftovers(tmp_path / "m") == []


def test_a_location_with_control_characters_is_one_plain_line(tmp_path: Path) -> None:
    client, seen = server({URL: redirect("https://cdn.example/\x7f\x1b[2Jt")})
    with pytest.raises(mr.DownloadError) as err:
        mr.download(entry(), tmp_path / "m", client=client)
    assert one_line(str(err.value)) and len(seen) == 1 and leftovers(tmp_path / "m") == []


def test_redirect_chains_and_loops_stop(tmp_path: Path) -> None:
    hops = {f"https://h{i}.example/": redirect(f"https://h{i + 1}.example/") for i in range(9)}
    client, seen = server({URL: redirect("https://h0.example/")} | hops)
    with pytest.raises(mr.DownloadError, match="more than 5 redirects"):
        mr.download(entry(), tmp_path / "m", client=client)
    assert len(seen) == mr.MAX_REDIRECTS + 1
    client, _ = server({URL: redirect(URL)})
    with pytest.raises(mr.DownloadError, match="redirects"):
        mr.download(entry(), tmp_path / "m2", client=client)
    client, _ = server({URL: lambda _r: httpx.Response(302)})
    with pytest.raises(mr.DownloadError, match="Location"):
        mr.download(entry(), tmp_path / "m3", client=client)


@pytest.mark.parametrize("status", [404, 403, 500, 206, 204])
def test_anything_but_200_is_refused(tmp_path: Path, status: int) -> None:
    client, _ = server({URL: lambda _r: httpx.Response(status, content=b"x")})
    with pytest.raises(mr.DownloadError, match=str(status)):
        mr.download(entry(), tmp_path / "m", client=client)
    assert leftovers(tmp_path / "m") == []


def test_a_slow_drip_is_stopped_at_the_deadline(tmp_path: Path) -> None:
    now = [0.0]

    def clock() -> float:
        now[0] += 10.0  # every look at the clock is 10 s later
        return now[0]
    client, _ = server({URL: lambda _r: httpx.Response(200, content=Tracked(b"x", None))})
    with pytest.raises(mr.DownloadError, match="longer than 100"):
        mr.download(entry(), tmp_path / "m", client=client, deadline_seconds=100, clock=clock)
    assert leftovers(tmp_path / "m") == []


def test_not_enough_disk_space_stops_before_reading(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = Tracked(BODY, 1)
    client, _ = server({URL: lambda _r: httpx.Response(
        200, content=body, headers={"content-length": str(len(BODY))})})
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(mr.shutil, "disk_usage", lambda _p: usage(10, 9, 1))
    with pytest.raises(mr.DownloadError, match="disk space"):
        mr.download(entry(), tmp_path / "m", client=client)
    assert body.sent == 0 and leftovers(tmp_path / "m") == []


def test_a_broken_connection_is_one_line_and_leaves_nothing(tmp_path: Path) -> None:
    def broken(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset")
    client, _ = server({URL: broken})
    with pytest.raises(mr.DownloadError, match="ConnectError"):
        mr.download(entry(), tmp_path / "m", client=client)
    assert leftovers(tmp_path / "m") == []


def test_a_link_planted_as_the_part_file_is_never_written_through(tmp_path: Path) -> None:
    # The old download opened `<name>.part` by name, following a link planted there.
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "tiny.gguf.part").symlink_to(victim)
    client, _ = server({URL: ok()})
    mr.download(entry(pinned=True), folder, client=client)
    assert victim.read_text() == "precious"
    assert (folder / "tiny.gguf").read_bytes() == BODY


def test_an_existing_file_is_never_replaced(tmp_path: Path) -> None:
    folder = tmp_path / "m"
    folder.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (folder / "tiny.gguf").symlink_to(victim)
    client, seen = server({URL: ok()})
    with pytest.raises(mr.DownloadError, match="already exists"):
        mr.download(entry(pinned=True), folder, client=client)
    assert victim.read_text() == "precious" and seen == []
    (folder / "tiny.gguf").unlink()
    (folder / "tiny.gguf").write_bytes(b"something else")
    with pytest.raises(mr.DownloadError, match="already exists"):
        mr.download(entry(), folder, client=client)
    assert (folder / "tiny.gguf").read_bytes() == b"something else"


def test_a_file_that_already_matches_the_pin_is_not_downloaded_again(tmp_path: Path) -> None:
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "tiny.gguf").write_bytes(BODY)
    client, seen = server({URL: ok()})
    got = mr.download(entry(pinned=True), folder, client=client)
    assert got.already_present and seen == []


def test_a_file_appearing_during_the_download_is_left_alone(
        tmp_path: Path) -> None:
    folder = tmp_path / "m"

    def sneaky(_r: httpx.Request) -> httpx.Response:
        (folder / "tiny.gguf").write_bytes(b"theirs")
        return ok()(_r)
    client, _ = server({URL: sneaky})
    with pytest.raises(mr.DownloadError, match="appeared"):
        mr.download(entry(pinned=True), folder, client=client)
    assert (folder / "tiny.gguf").read_bytes() == b"theirs"
    assert leftovers(folder) == ["tiny.gguf"]


def test_cli_download_options_are_checked(
        registry: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = ["download", MODEL_ID, str(tmp_path / "dl"), "--yes-download"]
    assert mr.main(base + ["--max-mb", "0"]) == 2
    assert mr.main(base + ["--deadline-minutes", "0"]) == 2
    assert mr.main(base + ["--deadline-minutes", "nan"]) == 2
    assert not (tmp_path / "dl").exists()
    assert mr.main(["download", "nope", str(tmp_path / "dl"), "--yes-download"]) == 2
