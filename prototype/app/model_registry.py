"""Pinned model registry: which AI models workers may run, with provenance and licenses.

A model is usable only once it is *pinned*: its exact sha256 and byte size are recorded
from a file the operator downloaded. Workers re-hash the file before every run, so a
swapped or corrupted model is refused.

CLI:
    python -m app.model_registry list
    python -m app.model_registry download <model_id> <dest-dir> --yes-download \
        [--expect-sha256 HEX] [--max-mb N] [--deadline-minutes N]
    python -m app.model_registry pin <model_id> <path> (--expect-sha256 HEX | --no-expected-hash) [--replace]
    python -m app.model_registry verify <model_id> <path>     # check a file against the pin

Safe fetching and pinning (`model-fetch-v1`, code audit seventh pass). Pinning the first
model is the next step on the rig, and the download comes from the internet, so before
this pass:
  * the download had no size limit (a broken or hostile mirror could fill the disk), no
    overall time limit (a server sending one byte a minute kept it going for ever),
    followed redirects to any address including plain http://, and let httpx unpack
    compressed replies;
  * the first download of an unpinned model was checked against nothing, and `pin` then
    recorded whatever had arrived as the trusted hash (trust on first use);
  * the `.part` file was opened by name, so a link planted under that name made the
    download write into another file; a finished download silently replaced a file
    already there;
  * a registry entry's `file_name` was joined to the model folder unchecked
    (`../../.bashrc` would do), and a registry with a missing field crashed with a
    traceback; `pin` re-pinned to a different file without a word and rewrote the
    registry in place (a crash midway left half a file);
  * hashing a named pipe given as "the model" hung for ever.

Now:
  * `load` reads the registry through `json_file` (1 MiB, no duplicate keys) and `vet`
    checks every field: plain file names ending `.gguf`, https URLs, lowercase hex hashes,
    whole-number sizes, sha256 and size both set or both empty, no unknown fields (a typo
    like `sha265` is refused instead of silently leaving the model unpinned), no model id
    or file name twice;
  * `download` follows at most 5 redirects, each https with no user name or password;
    asks for and accepts only uncompressed replies; refuses a reply whose declared size
    is wrong (pinned) or over the cap before reading it, and stops while reading once it
    passes; hashes while it writes; stops after `DOWNLOAD_DEADLINE_SECONDS` in total;
    checks free disk space first; writes to a fresh temporary file (never follows a
    link) and moves it into place only if every check passed and no file of that name
    exists. An unpinned model is checked against `--expect-sha256` (the sha256 the
    model page shows) when given; otherwise the CLI says plainly that nothing was
    checked;
  * `pin` from the CLI needs `--expect-sha256` (or an explicit `--no-expected-hash`),
    refuses to change an existing pin without `--replace`, and rewrites the registry
    atomically;
  * files are hashed through one open file handle: regular files only, never a pipe, and
    the bytes hashed must equal the size seen when it was opened.

What it can't do: tell whether the publisher's own file is honest (the sha256 on the model
page only shows the download wasn't changed on the way), or stop someone with write access
to the model folder from swapping the file between the worker's check and llama-cli
opening it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from app.json_file import UnsafeJSONError
from app.json_file import load as load_json
from app.worker_guard import say

MODEL_FETCH_VERSION = "model-fetch-v1"
DEFAULT_REGISTRY = Path(__file__).resolve().parent.parent / "models" / "registry.json"

# The registry lists a handful of models; 1 MiB is far more than it will ever need.
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_MODELS = 64
# The biggest card in the project has 8 GB of VRAM, so a model file bigger than that can't
# be run on it anyway. Also the ceiling for any download.
MAX_MODEL_BYTES = 8 * 1024 ** 3
# Unpinned download without --max-mb: twice the registry's approximate size, at least
# 64 MiB, at most MAX_MODEL_BYTES.
MIN_UNPINNED_CAP_BYTES = 64 * 1024 ** 2
# Leave this much disk free after the download.
DISK_MARGIN_BYTES = 512 * 1024 ** 2
MAX_REDIRECTS = 5
# Whole download, first request to last byte. httpx's own timeout is per read.
DOWNLOAD_DEADLINE_SECONDS = 3600.0
READ_TIMEOUT_SECONDS = 60.0
CHUNK = 1 << 20

MODEL_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\Z")
FILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

REQUIRED_FIELDS = ("model_id", "file_name", "source_url", "license")
OPTIONAL_FIELDS = ("sha256", "size_bytes", "min_vram_mb", "approx_size_mb", "upstream",
                   "runtime", "notes")


class RegistryError(ValueError):
    """The registry file is damaged or holds something the tools won't use."""


class DownloadError(Exception):
    """The download was refused or failed; nothing was put in place."""


def registry_path() -> Path:
    return Path(os.getenv("MODEL_REGISTRY", str(DEFAULT_REGISTRY)))


@dataclass(frozen=True)
class ModelEntry:
    model_id: str
    file_name: str
    source_url: str
    license: str
    sha256: str | None
    size_bytes: int | None
    min_vram_mb: int
    raw: dict[str, Any]

    @property
    def pinned(self) -> bool:
        return bool(self.sha256) and bool(self.size_bytes)


# ---------------------------------------------------------------------------- registry

def check_url(url: Any, what: str = "URL") -> str:
    """`url` if it is a plain https address (host, no user name or password), else
    RegistryError. Used for registry URLs and for every redirect a download follows."""
    if not isinstance(url, str) or not 1 <= len(url) <= 2000 or not url.isprintable() \
            or not url.isascii() or " " in url:
        raise RegistryError(f"{what} must be a short printable https:// address")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError:
        raise RegistryError(f"{what} is not a valid address") from None
    if parts.scheme != "https" or not host:
        raise RegistryError(f"{what} must start with https:// and name a host "
                            f"(got {say(url, 80)})")
    if parts.username is not None or parts.password is not None:
        raise RegistryError(f"{what} must not contain a user name or password")
    return url


def _int(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _text(value: Any, high: int) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= high and value.isprintable()


def vet(data: Any) -> dict[str, ModelEntry]:
    """Every field checked; RegistryError (one line, safe to print) on the first problem."""
    if not isinstance(data, dict):
        raise RegistryError("registry must be a JSON object")
    for key in data:
        if key != "models" and not key.startswith("_"):
            raise RegistryError(f"registry has an unknown field {say(key, 40)!r}")
    models = data.get("models")
    if not isinstance(models, list) or len(models) > MAX_MODELS:
        raise RegistryError(f"registry needs a \"models\" list of at most {MAX_MODELS} entries")
    out: dict[str, ModelEntry] = {}
    files: set[str] = set()
    for i, m in enumerate(models):
        where = f"model #{i + 1}"
        if not isinstance(m, dict):
            raise RegistryError(f"{where} must be an object")
        if isinstance(m.get("model_id"), str) and MODEL_ID_RE.match(m["model_id"]):
            where = f"model {m['model_id']}"
        for key in m:
            if key not in REQUIRED_FIELDS + OPTIONAL_FIELDS and not key.startswith("_"):
                raise RegistryError(f"{where} has an unknown field {say(key, 40)!r}")
        for key in REQUIRED_FIELDS:
            if key not in m:
                raise RegistryError(f"{where} is missing {key}")
        if not isinstance(m["model_id"], str) or not MODEL_ID_RE.match(m["model_id"]):
            raise RegistryError(f"{where}: model_id must be 1-80 of a-z 0-9 . _ - "
                                "(starting with a letter or digit)")
        name = m["file_name"]
        if not isinstance(name, str) or not FILE_NAME_RE.match(name):
            raise RegistryError(f"{where}: file_name must be a plain file name ending .gguf "
                                "(letters, digits, . _ -; no folders)")
        check_url(m["source_url"], f"{where}: source_url")
        if m.get("upstream") is not None:
            check_url(m["upstream"], f"{where}: upstream")
        if not _text(m["license"], 64):
            raise RegistryError(f"{where}: license must be printable text (1-64 characters)")
        sha, size = m.get("sha256"), m.get("size_bytes")
        if sha is not None and not (isinstance(sha, str) and SHA256_RE.match(sha)):
            raise RegistryError(f"{where}: sha256 must be 64 lowercase hex characters or null")
        if size is not None and not _int(size, 1, MAX_MODEL_BYTES):
            raise RegistryError(f"{where}: size_bytes must be a whole number from 1 to "
                                f"{MAX_MODEL_BYTES} or null")
        if (sha is None) != (size is None):
            raise RegistryError(f"{where}: sha256 and size_bytes must both be set (pinned) "
                                "or both be null (unpinned)")
        for key in ("min_vram_mb", "approx_size_mb"):
            if m.get(key) is not None and not _int(m[key], 0, 1_000_000):
                raise RegistryError(f"{where}: {key} must be a whole number from 0 to 1000000")
        for key in ("runtime", "notes"):
            if m.get(key) is not None and not _text(m[key], 2000):
                raise RegistryError(f"{where}: {key} must be printable text (one line, at "
                                    "most 2000 characters)")
        if m["model_id"] in out:
            raise RegistryError(f"{where} is listed twice")
        if name.lower() in files:
            raise RegistryError(f"{where}: file_name {name} is used by another model")
        files.add(name.lower())
        out[m["model_id"]] = ModelEntry(
            model_id=m["model_id"], file_name=name, source_url=m["source_url"],
            license=m["license"], sha256=sha, size_bytes=size,
            min_vram_mb=int(m.get("min_vram_mb") or 0), raw=m)
    return out


def _read_registry(path: Path) -> Any:
    try:
        return load_json(path, "model registry", MAX_REGISTRY_BYTES)
    except FileNotFoundError:
        raise RegistryError(f"model registry {say(str(path), 120)} not found") from None
    except UnsafeJSONError as exc:
        raise RegistryError(str(exc)) from None


def load(path: Path | None = None) -> dict[str, ModelEntry]:
    return vet(_read_registry(path or registry_path()))


# ------------------------------------------------------------------------------ files

def hash_file(path: Path, max_bytes: int = MAX_MODEL_BYTES,
              expect_size: int | None = None) -> tuple[str, int]:
    """(sha256, size) of one regular file, read through a single open handle so the size
    checked and the bytes hashed belong to the same file. Never blocks on a pipe. With
    `expect_size`, a file of another size is refused before any hashing."""
    label = f"model file {say(str(path), 120)}"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError(f"{label} can't be opened ({exc.strerror or 'error'})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{label} is not a regular file")
        if expect_size is not None and info.st_size != expect_size:
            raise ValueError("model file size does not match the pin")
        if info.st_size > max_bytes:
            raise ValueError(f"{label} is too big ({info.st_size} bytes; limit {max_bytes})")
        h, total = hashlib.sha256(), 0
        while block := os.read(fd, CHUNK):
            h.update(block)
            total += len(block)
            if total > info.st_size:
                break
    finally:
        os.close(fd)
    if total != info.st_size:
        raise ValueError(f"{label} changed while it was being read")
    return h.hexdigest(), total


def file_sha256(path: Path, chunk: int = CHUNK) -> str:  # noqa: ARG001 (old signature)
    return hash_file(path)[0]


def verify_file(entry: ModelEntry, path: Path) -> tuple[bool, str]:
    if not entry.pinned:
        return False, f"model {entry.model_id} is not pinned yet"
    try:
        digest, _ = hash_file(path, expect_size=entry.size_bytes)
    except FileNotFoundError:
        return False, f"model file not found: {say(str(path), 120)}"
    except ValueError as exc:
        return False, str(exc)
    if digest != entry.sha256:
        return False, "model file sha256 does not match the pin"
    return True, "ok"


def _normal_hex(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().lower()
    if value.startswith("sha256:"):
        value = value[len("sha256:"):]
    if not SHA256_RE.match(value):
        raise ValueError("expected sha256 must be 64 hex characters")
    return value


def _write_registry(reg_path: Path, data: Any) -> None:
    """Write next to the registry, then swap in one step: a crash leaves the old file."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=reg_path.parent, prefix=".registry.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, stat.S_IMODE(os.stat(reg_path).st_mode))
        except OSError:
            pass
        os.replace(tmp, reg_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def pin(model_id: str, file: Path, path: Path | None = None, *,
        expect_sha256: str | None = None, replace: bool = False) -> dict[str, Any]:
    """Record `file`'s sha256 and size as the pin for `model_id`. Refuses a file whose
    hash isn't `expect_sha256` (when given) and, unless `replace`, a file that differs
    from an existing pin. Raises KeyError for an unknown model, ValueError otherwise."""
    reg_path = path or registry_path()
    data = _read_registry(reg_path)
    models = vet(data)
    if model_id not in models:
        raise KeyError(f"unknown model_id {say(model_id, 80)}")
    expected = _normal_hex(expect_sha256)
    digest, size = hash_file(file)
    if expected is not None and digest != expected:
        raise ValueError(f"file sha256 {digest} is not the expected {expected}; not pinned")
    entry = models[model_id]
    if entry.pinned and (entry.sha256, entry.size_bytes) != (digest, size) and not replace:
        raise ValueError(f"{model_id} is already pinned to a different file "
                         f"(sha256 {entry.sha256}); use --replace to change the pin")
    for m in data["models"]:
        if m["model_id"] == model_id:
            m["sha256"], m["size_bytes"] = digest, size
            _write_registry(reg_path, data)
            return m
    raise KeyError(f"unknown model_id {say(model_id, 80)}")  # pragma: no cover


# --------------------------------------------------------------------------- download

@dataclass(frozen=True)
class Downloaded:
    path: Path
    sha256: str
    size_bytes: int
    checked_against: str  # "pin", "expected", or "nothing"
    already_present: bool = False


def download_cap(entry: ModelEntry, max_bytes: int | None = None) -> int:
    """Most bytes a download of `entry` may bring."""
    if entry.pinned:
        return int(entry.size_bytes)  # type: ignore[arg-type]
    if max_bytes is not None:
        return max(1, min(int(max_bytes), MAX_MODEL_BYTES))
    approx = entry.raw.get("approx_size_mb")
    if not approx:
        return MAX_MODEL_BYTES
    return max(MIN_UNPINNED_CAP_BYTES, min(2 * int(approx) * 1024 ** 2, MAX_MODEL_BYTES))


def _client(timeout: float):  # pragma: no cover - real network
    import httpx
    return httpx.Client(timeout=timeout, follow_redirects=False, trust_env=True)


def download(entry: ModelEntry, dest_dir: Path, *, expect_sha256: str | None = None,
             max_bytes: int | None = None,
             deadline_seconds: float = DOWNLOAD_DEADLINE_SECONDS,
             client: Any = None, clock: Callable[[], float] = time.monotonic,
             progress: Callable[[int, int | None], None] | None = None) -> Downloaded:
    """Opt-in download of one registry model into `dest_dir` (see the module notes).
    Raises DownloadError with a one-line reason; nothing is left behind on failure."""
    try:
        expected = _normal_hex(expect_sha256)
    except ValueError as exc:
        raise DownloadError(str(exc)) from None
    if entry.pinned and expected is not None and expected != entry.sha256:
        raise DownloadError(f"--expect-sha256 {expected} differs from the pin "
                            f"{entry.sha256}; fix one of them first")
    want_sha = entry.sha256 if entry.pinned else expected
    checked = "pin" if entry.pinned else ("expected" if expected else "nothing")
    cap = download_cap(entry, max_bytes)
    try:
        check_url(entry.source_url, "source_url")
    except RegistryError as exc:
        raise DownloadError(str(exc)) from None
    if not FILE_NAME_RE.match(entry.file_name):
        raise DownloadError("file_name must be a plain file name ending .gguf")

    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / entry.file_name
    if os.path.lexists(final):
        if entry.pinned and verify_file(entry, final)[0]:
            return Downloaded(final, entry.sha256, int(entry.size_bytes), "pin",  # type: ignore[arg-type]
                              already_present=True)
        raise DownloadError(f"{say(str(final), 120)} already exists; move it away (or "
                            "check it with `verify`) instead of downloading over it")

    started = clock()

    def time_left() -> float:
        left = deadline_seconds - (clock() - started)
        if left <= 0:
            raise DownloadError(f"download took longer than {deadline_seconds:g} seconds; "
                                "stopped (use --deadline-minutes for a slow connection)")
        return left

    own_client = client is None
    if own_client:  # pragma: no cover - real network
        client = _client(READ_TIMEOUT_SECONDS)
    fd, tmp_name = tempfile.mkstemp(dir=dest_dir, prefix="." + entry.file_name + ".",
                                    suffix=".part")
    tmp = Path(tmp_name)
    try:
        h, total, length = hashlib.sha256(), 0, None
        url = entry.source_url
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), 0o644)
            for hop in range(MAX_REDIRECTS + 1):
                time_left()
                with client.stream("GET", url, headers={
                        "Accept-Encoding": "identity",
                        "User-Agent": f"bnh-model-registry/{MODEL_FETCH_VERSION}"}) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        location = r.headers.get("location")
                        if not location:
                            raise DownloadError("redirect without a Location header")
                        if hop == MAX_REDIRECTS:
                            raise DownloadError(f"more than {MAX_REDIRECTS} redirects")
                        try:
                            url = check_url(urljoin(url, location), "redirect")
                        except RegistryError as exc:
                            raise DownloadError(f"refused {exc}") from None
                        continue
                    if r.status_code != 200:
                        raise DownloadError(f"server answered {r.status_code} for "
                                            f"{say(urlsplit(url).hostname or '?', 80)}")
                    encoding = r.headers.get("content-encoding", "identity").strip().lower()
                    if encoding not in ("", "identity"):
                        raise DownloadError(f"server sent a compressed reply "
                                            f"({say(encoding, 20)}); refused")
                    declared = r.headers.get("content-length")
                    if declared is not None:
                        if not declared.strip().isdigit() or len(declared.strip()) > 18:
                            raise DownloadError("server sent a bad Content-Length")
                        length = int(declared)
                        if entry.pinned and length != entry.size_bytes:
                            raise DownloadError(f"server says the file is {length} bytes; "
                                                f"the pin says {entry.size_bytes}")
                        if length > cap:
                            raise DownloadError(f"server says the file is {length} bytes, "
                                                f"over the {cap}-byte limit")
                    need = (length if length is not None else cap) + DISK_MARGIN_BYTES
                    free = shutil.disk_usage(dest_dir).free
                    if free < need:
                        raise DownloadError(f"not enough disk space in {say(str(dest_dir), 80)}"
                                            f" ({free} bytes free, {need} needed)")
                    for block in r.iter_raw(CHUNK):
                        time_left()
                        total += len(block)
                        if total > cap:
                            raise DownloadError(f"download passed the {cap}-byte limit; "
                                                "stopped")
                        h.update(block)
                        out.write(block)
                        if progress:
                            progress(total, length)
                    break
            out.flush()
            os.fsync(out.fileno())
        if length is not None and total != length:
            raise DownloadError(f"download ended early ({total} of {length} bytes)")
        if entry.pinned and total != entry.size_bytes:
            raise DownloadError(f"got {total} bytes; the pin says {entry.size_bytes}")
        digest = h.hexdigest()
        if want_sha is not None and digest != want_sha:
            raise DownloadError(f"downloaded file's sha256 {digest} does not match the "
                                f"{'pin' if entry.pinned else 'expected'} {want_sha}; "
                                "deleted")
        try:
            os.link(tmp, final)  # never replaces an existing file
        except FileExistsError:
            raise DownloadError(f"{say(str(final), 120)} appeared while downloading; "
                                "left it alone") from None
        return Downloaded(final, digest, total, checked)
    except DownloadError:
        raise
    except OSError as exc:
        raise DownloadError(f"download failed ({exc.strerror or type(exc).__name__})") from None
    except Exception as exc:  # httpx errors: timeouts, TLS, connection resets
        raise DownloadError(f"download failed ({type(exc).__name__})") from None
    finally:
        tmp.unlink(missing_ok=True)
        if own_client:  # pragma: no cover - real network
            client.close()


# -------------------------------------------------------------------------------- CLI

def _progress_printer() -> Callable[[int, int | None], None]:
    state = {"next": 0}

    def show(done: int, total: int | None) -> None:
        step = max(total // 10, 1) if total else 64 * 1024 ** 2
        if done >= state["next"]:
            share = f" ({100 * done // total}%)" if total else ""
            print(f"  {done // 1024 ** 2} MiB{share}", file=sys.stderr, flush=True)
            state["next"] = done + step
    return show


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.model_registry")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s = sub.add_parser("pin", help="record a downloaded file's sha256 and size")
    s.add_argument("model_id")
    s.add_argument("file", type=Path)
    how = s.add_mutually_exclusive_group(required=True)
    how.add_argument("--expect-sha256", metavar="HEX",
                     help="the sha256 shown on the model's page; the file must match it")
    how.add_argument("--no-expected-hash", action="store_true",
                     help="pin without comparing to a published sha256 (you checked "
                          "another way)")
    s.add_argument("--replace", action="store_true",
                   help="allowed to change an existing pin to a different file")
    v = sub.add_parser("verify")
    v.add_argument("model_id")
    v.add_argument("file", type=Path)
    d = sub.add_parser("download")
    d.add_argument("model_id")
    d.add_argument("dest", type=Path)
    d.add_argument("--yes-download", action="store_true",
                   help="required: confirms you want to download a large model file")
    d.add_argument("--expect-sha256", metavar="HEX",
                   help="the sha256 shown on the model's page (checked before keeping it)")
    d.add_argument("--max-mb", type=int, metavar="N",
                   help="size limit for an unpinned model (default: twice approx_size_mb)")
    d.add_argument("--deadline-minutes", type=float, default=DOWNLOAD_DEADLINE_SECONDS / 60,
                   metavar="N", help="give up after this long (default 60)")
    args = p.parse_args(argv)
    try:
        models = load()
    except RegistryError as exc:
        print(f"model registry refused: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "list":
        for m in models.values():
            state = "PINNED" if m.pinned else "unpinned"
            print(f"{m.model_id:40} {state:9} {say(m.license, 12):12} "
                  f"{m.raw.get('approx_size_mb', '?')} MB")
        return 0
    if args.model_id not in models:
        print(f"unknown model_id {say(args.model_id, 80)}", file=sys.stderr)
        return 2
    entry = models[args.model_id]
    if args.cmd == "pin":
        try:
            m = pin(args.model_id, args.file, expect_sha256=args.expect_sha256,
                    replace=args.replace)
        except FileNotFoundError:
            print(f"file not found: {say(str(args.file), 120)}", file=sys.stderr)
            return 2
        except (KeyError, ValueError) as exc:
            print(f"not pinned: {say(str(exc).strip(chr(39)), 300)}", file=sys.stderr)
            return 2
        print(f"pinned {args.model_id}: sha256={m['sha256']} size={m['size_bytes']}")
        if args.no_expected_hash:
            print("note: not compared with a published sha256 (--no-expected-hash)")
        return 0
    if args.cmd == "verify":
        ok, why = verify_file(entry, args.file)
        print(why)
        return 0 if ok else 1
    if not args.yes_download:
        print(f"Refusing to download ~{entry.raw.get('approx_size_mb', '?')} MB without "
              f"--yes-download. Source: {say(entry.source_url, 300)}", file=sys.stderr)
        return 2
    if args.max_mb is not None and not 1 <= args.max_mb <= MAX_MODEL_BYTES // 1024 ** 2:
        print(f"--max-mb must be 1-{MAX_MODEL_BYTES // 1024 ** 2}", file=sys.stderr)
        return 2
    if not 0 < args.deadline_minutes <= 24 * 60:
        print("--deadline-minutes must be more than 0 and at most 1440", file=sys.stderr)
        return 2
    try:  # pragma: no cover - real network
        got = download(entry, args.dest, expect_sha256=args.expect_sha256,
                       max_bytes=None if args.max_mb is None else args.max_mb * 1024 ** 2,
                       deadline_seconds=args.deadline_minutes * 60,
                       progress=_progress_printer())
    except DownloadError as exc:  # pragma: no cover - real network
        print(f"download refused: {say(str(exc), 300)}", file=sys.stderr)
        return 2
    return report_download(got)  # pragma: no cover - real network


def report_download(got: Downloaded) -> int:
    where = say(str(got.path), 200)
    if got.already_present:
        print(f"{where} is already here and matches the pin; nothing downloaded")
        return 0
    print(f"saved {where}")
    print(f"sha256={got.sha256} size={got.size_bytes}")
    if got.checked_against == "pin":
        print("matches the pin")
    elif got.checked_against == "expected":
        print("matches --expect-sha256. Next: pin it with the same --expect-sha256")
    else:
        print("NOT CHECKED: nothing to compare with. Before pinning, compare the sha256 "
              "above with the one on the model's page, then pin with --expect-sha256 <it>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
