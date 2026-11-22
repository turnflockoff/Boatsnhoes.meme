"""Pinned model registry: which AI models workers may run, with provenance and licenses.

A model is usable only once it is *pinned*: its exact sha256 and byte size are recorded
from a file the operator downloaded. Workers re-hash the file before every run, so a
swapped or corrupted model is refused.

CLI:
    python -m app.model_registry list
    python -m app.model_registry pin <model_id> <path>        # record sha256 + size
    python -m app.model_registry verify <model_id> <path>     # check a file against the pin
    python -m app.model_registry download <model_id> <dest-dir> --yes-download
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY = Path(__file__).resolve().parent.parent / "models" / "registry.json"


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


def load(path: Path | None = None) -> dict[str, ModelEntry]:
    data = json.loads((path or registry_path()).read_text())
    out: dict[str, ModelEntry] = {}
    for m in data.get("models", []):
        out[m["model_id"]] = ModelEntry(
            model_id=m["model_id"], file_name=m["file_name"], source_url=m["source_url"],
            license=m["license"], sha256=m.get("sha256"), size_bytes=m.get("size_bytes"),
            min_vram_mb=int(m.get("min_vram_mb", 0)), raw=m)
    return out


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def verify_file(entry: ModelEntry, path: Path) -> tuple[bool, str]:
    if not entry.pinned:
        return False, f"model {entry.model_id} is not pinned yet"
    if not path.is_file():
        return False, f"model file not found: {path}"
    if path.stat().st_size != entry.size_bytes:
        return False, "model file size does not match the pin"
    if file_sha256(path) != entry.sha256:
        return False, "model file sha256 does not match the pin"
    return True, "ok"


def pin(model_id: str, file: Path, path: Path | None = None) -> dict[str, Any]:
    reg_path = path or registry_path()
    data = json.loads(reg_path.read_text())
    for m in data["models"]:
        if m["model_id"] == model_id:
            m["sha256"] = file_sha256(file)
            m["size_bytes"] = file.stat().st_size
            reg_path.write_text(json.dumps(data, indent=2) + "\n")
            return m
    raise KeyError(f"unknown model_id {model_id}")


def download(entry: ModelEntry, dest_dir: Path) -> Path:  # pragma: no cover - network
    """Opt-in download. Verifies against the pin if one exists."""
    import httpx

    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / entry.file_name
    tmp = final.with_suffix(final.suffix + ".part")
    with httpx.stream("GET", entry.source_url, follow_redirects=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for block in r.iter_bytes(1 << 20):
                f.write(block)
    if entry.pinned:
        ok, why = verify_file(entry, tmp)
        if not ok:
            tmp.unlink(missing_ok=True)
            raise ValueError(why)
    tmp.rename(final)
    return final


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.model_registry")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("pin", "verify"):
        s = sub.add_parser(name)
        s.add_argument("model_id")
        s.add_argument("file", type=Path)
    d = sub.add_parser("download")
    d.add_argument("model_id")
    d.add_argument("dest", type=Path)
    d.add_argument("--yes-download", action="store_true",
                   help="required: confirms you want to download a large model file")
    args = p.parse_args(argv)
    models = load()
    if args.cmd == "list":
        for m in models.values():
            state = "PINNED" if m.pinned else "unpinned"
            print(f"{m.model_id:40} {state:9} {m.license:12} {m.raw.get('approx_size_mb', '?')} MB")
        return 0
    if args.model_id not in models:
        print(f"unknown model_id {args.model_id}", file=sys.stderr)
        return 2
    entry = models[args.model_id]
    if args.cmd == "pin":
        m = pin(args.model_id, args.file)
        print(f"pinned {args.model_id}: sha256={m['sha256']} size={m['size_bytes']}")
        return 0
    if args.cmd == "verify":
        ok, why = verify_file(entry, args.file)
        print(why)
        return 0 if ok else 1
    if not args.yes_download:
        print(f"Refusing to download ~{entry.raw.get('approx_size_mb', '?')} MB without "
              f"--yes-download. Source: {entry.source_url}", file=sys.stderr)
        return 2
    print(download(entry, args.dest))  # pragma: no cover
    return 0  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
