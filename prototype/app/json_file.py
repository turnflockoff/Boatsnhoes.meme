"""Reading JSON files from strangers safely (`json-file-v1`, code audit fourth pass).

Three of our tools are run by volunteers on JSON files somebody else wrote:

    rewards verify          a signed reward report downloaded from /reports/ (or a mirror,
                            a chat message, a pull request);
    release verify          a worker release's manifest, its signature file and a signer
                            list, downloaded before running the worker;
    publish_report check    the `reports/index.json` that the website shows, re-checked in
                            CI on every pull request, including pull requests from strangers.

They read those files with a plain `json.loads(path.read_text())`, so before this pass:

  * a file nested 100,000 brackets deep crashed them with a RecursionError traceback;
    so did a top-level list or `null` where a report was expected, and bad UTF-8;
  * a named pipe or `/dev/zero` given as "the report" hung or filled memory, and a
    multi-gigabyte file was read whole;
  * `NaN` and `Infinity` (not JSON, but Python accepts them) were taken as numbers;
  * a key written twice (`"payable_points": 1, ..., "payable_points": 9999`) was quietly
    read as the last one. Other programs (browsers, jq, other languages) may pick the
    other one, so two honest readers of the same file could see different numbers.

`load(path, what)` refuses all of that with one plain error (`UnsafeJSONError`, a
`ValueError`) before any of our code looks at the contents:

  1. the path must be a regular file (not a pipe, device or folder) of at most `max_bytes`
     (default `MAX_JSON_BYTES`, 4 MiB; reward reports get `MAX_REPORT_BYTES`, 64 MiB), and
     never more than that many bytes are read, even if the file grows while it is read;
  2. strict UTF-8 (a leading byte-order mark is refused too);
  3. brackets nested at most `MAX_JSON_DEPTH` (32) deep, checked before parsing;
  4. no `NaN`/`Infinity`, no numbers Python refuses to convert (4,300+ digits), and no key
     twice in the same object.

`say(text)` (from `worker_guard`) makes any text taken from such a file safe to print:
terminal control codes and invisible characters become `?`.

This makes *reading* the file safe. Whether its contents are true is still decided by the
signatures, hashes and re-checks in the tools themselves.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from app.input_limits import MAX_JSON_DEPTH, json_depth
from app.worker_guard import say

JSON_FILE_VERSION = "json-file-v1"

# Release manifests (~100 files), signature files, signer lists and the reports index are
# a few KB to a few hundred KB.
MAX_JSON_BYTES = 4 * 1024 * 1024
# A reward report lists one entry (~250 bytes) per accepted result: 64 MiB is roughly
# 250,000 results in one period, far beyond anything the prototype will see.
MAX_REPORT_BYTES = 64 * 1024 * 1024


class UnsafeJSONError(ValueError):
    """The file is not one plain, bounded JSON document; nothing in it was used."""


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"key {say(key, 40)!s} appears twice in one object")
        out[key] = value
    return out


def parse(raw: bytes, what: str = "file", max_depth: int = MAX_JSON_DEPTH) -> Any:
    """Parse bytes already in memory under the same rules as `load`."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raise UnsafeJSONError(f"{what} starts with a byte-order mark; save it as plain UTF-8")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise UnsafeJSONError(f"{what} is not UTF-8 text") from None
    if not text.strip():
        raise UnsafeJSONError(f"{what} is empty")
    if json_depth(raw) > max_depth:
        raise UnsafeJSONError(f"{what} is nested deeper than {max_depth} levels")
    try:
        return json.loads(text, parse_constant=_no_constants,
                          object_pairs_hook=_no_duplicate_keys)
    except RecursionError:
        raise UnsafeJSONError(f"{what} is nested too deeply") from None
    except ValueError as exc:
        # Bad JSON, NaN/Infinity, a duplicated key, or a 4,300+ digit number.
        reason = str(exc).split(":")[0] if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise UnsafeJSONError(f"{what} is not plain JSON ({say(reason, 80)})") from None


def load(path: str | os.PathLike[str], what: str = "file",
         max_bytes: int = MAX_JSON_BYTES, max_depth: int = MAX_JSON_DEPTH) -> Any:
    """Read and parse one JSON file someone else may have written. Raises FileNotFoundError
    if it doesn't exist and UnsafeJSONError for anything else that isn't plain JSON."""
    path = Path(path)
    label = f"{what} {say(str(path), 120)}"
    try:
        # O_NONBLOCK: opening a named pipe with no writer must not hang. Checked with fstat
        # on the open file, so the path can't be swapped for something else in between.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise UnsafeJSONError(f"{label} can't be opened ({exc.strerror or 'error'})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeJSONError(f"{label} is not a regular file")
        if info.st_size > max_bytes:
            raise UnsafeJSONError(f"{label} is too big ({info.st_size} bytes; limit "
                                  f"{max_bytes})")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1 << 20, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:  # it grew while being read
                raise UnsafeJSONError(f"{label} is too big (over {max_bytes} bytes)")
    finally:
        os.close(fd)
    return parse(b"".join(chunks), label, max_depth)
