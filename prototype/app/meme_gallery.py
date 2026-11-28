"""Pictures from strangers in the Meme Lab gallery (`meme-gallery-v1`, code audit
twenty-third pass).

The Meme Lab (`/meme-lab`) lets anyone send a picture through a Netlify form. The owner
looks at it, and the good ones are saved into `assets/memes/`, listed in
`assets/memes/gallery.json`, and pushed, so the file is then public on the website *and*
on GitHub, forever (git history keeps it even after it is deleted).

A picture file carries more than the picture. Before this pass the instructions were
"save the picture into `assets/memes/`", so whatever the sender's phone or editor had
tucked inside went public with it:

    camera data (EXIF)   where the photo was taken (GPS, to a few metres), when, which
                         phone (make, model, sometimes its serial number), and often a
                         small preview copy of the *uncropped* original;
    XMP / IPTC notes     editing history, the author's name, file paths from their
                         computer (`C:\\Users\\<real name>\\...`), keywords;
    text notes           PNG `tEXt`/`iTXt`/`zTXt`, GIF comments, JPEG comments: any text;
    extra data at the    phones put more pictures (depth maps, the "live"/"motion" video)
    end of the file      after the end of a JPEG; anything can be hidden there, including
                         a whole zip file, and every viewer ignores it.

The meme maker itself is safe (it redraws the picture on a canvas, which keeps none of
that), but a sender can also pick a file straight from their phone in the form.

What this module does (no image library; no image decoder ever runs on a stranger's file,
only a careful walk over the file's labelled parts):

  * `scan(data)` reads PNG, JPEG, GIF and WebP part by part, refuses anything cut off,
    malformed, of an unknown required kind, or too big to show safely (over
    `MAX_SIDE` pixels on a side or `MAX_PIXELS` in all), and returns the picture with
    every hidden part taken out, plus a plain list of what was found. Only the parts a
    viewer needs to draw the picture (and its colours and animation) are kept. It also
    notes when the camera had marked a photo as turned, because without that note it will
    show turned.
  * `python -m app.meme_gallery add FILE --name NAME --alt "..." [--credit "..."]` writes
    the cleaned copy into `assets/memes/` and adds its line to `gallery.json`.
  * `python -m app.meme_gallery check` (CI, and a test on the real folder) refuses the push
    if any picture in the folder still has hidden data, isn't listed, is listed twice, is
    a link, or if `gallery.json` isn't exactly what the page expects.
  * `python -m app.meme_gallery inspect FILE` shows what a picture carries, changing
    nothing.

Honest limits: it can't see what is *in* the picture (a street sign, a face, a
reflection, a username in a screenshot); that is still the owner's look. It doesn't
re-check pictures already in git history. A cleaned JPEG that the camera had marked as
turned shows turned until it is rotated in an editor.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import tempfile
import unicodedata
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import json_file
from app.screen import say

GALLERY_VERSION = "meme-gallery-v1"
# Same limit the form states ("up to 8 MB").
MAX_MEME_BYTES = 8 * 1024 * 1024
MAX_SIDE = 8192
MAX_PIXELS = 40_000_000
MAX_GALLERY_JSON_BYTES = 256 * 1024
# The page shows the first 60 entries (assets/meme-lab.js).
MAX_SHOWN = 60
MAX_ALT_CHARS = 300
MAX_CREDIT_CHARS = 60
MAX_GIF_FRAMES = 2000
MAX_PARTS = 100_000

SITE_ROOT = Path(__file__).resolve().parents[2]
GALLERY_DIR = Path("assets") / "memes"
GALLERY_FILE = "gallery.json"
# Stricter than the page's own filter (which it must stay inside): starts with a letter or
# number, no "..", lower-case extension.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*\.(png|jpg|jpeg|gif|webp)\Z")
STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
KIND_OF_EXT = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "gif": "gif", "webp": "webp"}
EXT_OF_KIND = {"png": "png", "jpeg": "jpg", "gif": "gif", "webp": "webp"}


class BadMeme(ValueError):
    """The file isn't a picture we can safely read, clean and show."""


@dataclass
class Picture:
    kind: str
    width: int
    height: int
    clean: bytes
    found: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    frames: int = 1

    @property
    def is_clean(self) -> bool:
        return not self.found

    def summary(self) -> dict[str, Any]:
        return {"kind": self.kind, "width": self.width, "height": self.height,
                "frames": self.frames, "clean_bytes": len(self.clean),
                "hidden_data": list(self.found), "warnings": list(self.warnings),
                "is_clean": self.is_clean}


def _note(found: list[str], label: str) -> None:
    if label not in found:
        found.append(label)


def _check_size(kind: str, width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise BadMeme(f"{kind} picture says it is {width} x {height} pixels")
    if width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
        raise BadMeme(f"{kind} picture is too big to show safely ({width} x {height} pixels; "
                      f"limit {MAX_SIDE} on a side, {MAX_PIXELS:,} in all)")


def _trailing(found: list[str], extra: int) -> None:
    if extra > 0:
        _note(found, f"extra data after the end of the picture ({extra} bytes; phones put "
                     "other pictures or a short video there, and anything can hide there)")


# --- camera data (EXIF): only read to say what was in it ---------------------------------

def _exif_details(tiff: bytes) -> tuple[list[str], int | None]:
    """What a TIFF/EXIF block holds that people care about, and the orientation tag.
    Never raises: a damaged block is still reported as camera data by the caller."""
    labels: list[str] = []
    orientation: int | None = None
    try:
        if tiff[:2] == b"II":
            end = "<"
        elif tiff[:2] == b"MM":
            end = ">"
        else:
            return labels, None
        if struct.unpack(end + "H", tiff[2:4])[0] != 42:
            return labels, None
        ifd = struct.unpack(end + "I", tiff[4:8])[0]
        count = struct.unpack(end + "H", tiff[ifd:ifd + 2])[0]
        for i in range(min(count, 500)):
            at = ifd + 2 + 12 * i
            tag, typ = struct.unpack(end + "HH", tiff[at:at + 4])
            if tag == 0x8825:
                labels.append("where it was taken (GPS location)")
            elif tag in (0x010F, 0x0110):
                if "which camera or phone" not in labels:
                    labels.append("which camera or phone")
            elif tag == 0x0132:
                labels.append("when it was taken")
            elif tag == 0x013B:
                labels.append("the author's name")
            elif tag == 0x0112 and typ == 3:
                orientation = struct.unpack(end + "H", tiff[at + 8:at + 10])[0]
        nxt_at = ifd + 2 + 12 * count
        nxt = struct.unpack(end + "I", tiff[nxt_at:nxt_at + 4])[0]
        if nxt:
            labels.append("a small preview copy (it can show the photo before it was cropped)")
    except (struct.error, IndexError):
        pass
    return labels, orientation


def _camera_data(found: list[str], warnings: list[str], tiff: bytes) -> None:
    labels, orientation = _exif_details(tiff)
    detail = f" with {', '.join(labels)}" if labels else ""
    _note(found, f"camera data (EXIF){detail}")
    if orientation not in (None, 1):
        warnings.append(f"the camera marked this photo as turned (orientation {orientation}); "
                        "without that note it will show turned, so rotate it in an editor "
                        "or the Meme Lab and add it again")


# --- PNG ----------------------------------------------------------------------------------

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"cHRM", b"gAMA", b"iCCP", b"sBIT",
             b"sRGB", b"cICP", b"bKGD", b"pHYs", b"acTL", b"fcTL", b"fdAT"}
_PNG_LABEL = {b"tEXt": "text notes (tEXt)", b"zTXt": "text notes (zTXt)",
              b"iTXt": "text notes (iTXt, often XMP editing notes)",
              b"tIME": "when it was last saved (tIME)"}


def _scan_png(data: bytes) -> Picture:
    found: list[str] = []
    warnings: list[str] = []
    out = [_PNG_SIG]
    pos = len(_PNG_SIG)
    width = height = 0
    frames = 1
    seen_idat = False
    parts = 0
    while True:
        parts += 1
        if parts > MAX_PARTS:
            raise BadMeme("PNG has too many parts")
        if pos + 12 > len(data):
            raise BadMeme("PNG is cut off (no end marker)")
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        if length > 0x7FFFFFFF or pos + 12 + length > len(data):
            raise BadMeme("PNG is cut off or has a part longer than the file")
        if not all(65 <= c <= 90 or 97 <= c <= 122 for c in ctype):
            raise BadMeme("PNG has a part with an invalid name")
        body = data[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        if zlib.crc32(ctype + body) & 0xFFFFFFFF != crc:
            raise BadMeme(f"PNG part {ctype.decode()} is damaged (checksum)")
        whole = data[pos:pos + 12 + length]
        pos += 12 + length
        if parts == 1:
            if ctype != b"IHDR" or length != 13:
                raise BadMeme("PNG doesn't start with its header part")
            width, height = struct.unpack(">II", body[:8])
            _check_size("PNG", width, height)
        elif ctype == b"IHDR":
            raise BadMeme("PNG has a second header part")
        if ctype == b"acTL" and length >= 4:
            frames = max(1, struct.unpack(">I", body[:4])[0])
            if frames > MAX_GIF_FRAMES:
                raise BadMeme(f"PNG animation has too many frames ({frames})")
        if ctype == b"IDAT":
            seen_idat = True
        if ctype in _PNG_KEEP:
            out.append(whole)
        elif ctype == b"eXIf":
            _camera_data(found, warnings, body)
        elif ctype in _PNG_LABEL:
            _note(found, _PNG_LABEL[ctype])
        elif 65 <= ctype[0] <= 90:  # an unknown *required* part: viewers must refuse it
            raise BadMeme(f"PNG has an unknown required part ({say(ctype.decode(), 8)})")
        else:
            _note(found, f"unknown extra part ({say(ctype.decode(), 8)})")
        if ctype == b"IEND":
            break
    if not seen_idat:
        raise BadMeme("PNG has no picture data")
    _trailing(found, len(data) - pos)
    return Picture("png", width, height, b"".join(out), found, warnings, frames)


# --- JPEG ---------------------------------------------------------------------------------

_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_JPEG_TABLES = {0xC4, 0xCC, 0xDB, 0xDD, 0xDC}
_XMP_ID = b"http://ns.adobe.com/xap/1.0/\x00"
_XMP_EXT_ID = b"http://ns.adobe.com/xmp/extension/\x00"


def _scan_jpeg(data: bytes) -> Picture:
    found: list[str] = []
    warnings: list[str] = []
    out = [b"\xff\xd8"]
    pos = 2
    width = height = 0
    seen_sof = seen_sos = False
    parts = 0
    while True:
        parts += 1
        if parts > MAX_PARTS:
            raise BadMeme("JPEG has too many parts")
        if pos >= len(data) or data[pos] != 0xFF:
            raise BadMeme("JPEG is cut off or damaged (expected a part marker)")
        while pos < len(data) and data[pos] == 0xFF:  # fill bytes
            pos += 1
        if pos >= len(data):
            raise BadMeme("JPEG is cut off")
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # end of picture
            if not seen_sos:
                raise BadMeme("JPEG ends before any picture data")
            out.append(b"\xff\xd9")
            break
        if marker == 0x00 or 0xD0 <= marker <= 0xD8 or marker == 0x01:
            raise BadMeme("JPEG has a marker in the wrong place")
        if pos + 2 > len(data):
            raise BadMeme("JPEG is cut off")
        length = struct.unpack(">H", data[pos:pos + 2])[0]
        if length < 2 or pos + length > len(data):
            raise BadMeme("JPEG is cut off or has a part longer than the file")
        body = data[pos + 2:pos + length]
        whole = b"\xff" + bytes([marker]) + data[pos:pos + length]
        pos += length
        if marker in _SOF:
            if seen_sof:
                raise BadMeme("JPEG has two frame headers")
            if len(body) < 6:
                raise BadMeme("JPEG frame header is too short")
            height, width = struct.unpack(">HH", body[1:5])
            _check_size("JPEG", width, height)
            seen_sof = True
            out.append(whole)
        elif marker in _JPEG_TABLES:
            out.append(whole)
        elif marker == 0xDA:  # start of scan: picture data follows until the next marker
            if not seen_sof:
                raise BadMeme("JPEG picture data comes before its frame header")
            seen_sos = True
            out.append(whole)
            start = pos
            while True:
                nxt = data.find(b"\xff", pos)
                if nxt < 0 or nxt + 1 >= len(data):
                    raise BadMeme("JPEG is cut off (picture data never ends)")
                follow = data[nxt + 1]
                if follow == 0x00 or 0xD0 <= follow <= 0xD7:
                    pos = nxt + 2
                    continue
                if follow == 0xFF:
                    pos = nxt + 1
                    continue
                pos = nxt
                break
            out.append(data[start:pos])
        elif marker == 0xE0:
            if body.startswith(b"JFIF\x00") and len(body) >= 14:
                # Keep the header but drop any preview copy inside it.
                if body[12] or body[13]:
                    _note(found, "a small preview copy in the JFIF header")
                head = body[:12] + b"\x00\x00"
                out.append(b"\xff\xe0" + struct.pack(">H", len(head) + 2) + head)
            elif body.startswith(b"JFXX\x00"):
                _note(found, "a small preview copy (JFXX)")
            else:
                _note(found, "extra app data (APP0)")
        elif marker == 0xE1:
            if body.startswith(b"Exif\x00\x00"):
                _camera_data(found, warnings, body[6:])
            elif body.startswith(_XMP_ID) or body.startswith(_XMP_EXT_ID):
                _note(found, "XMP notes (editing history, names, file paths)")
            else:
                _note(found, "extra app data (APP1)")
        elif marker == 0xE2 and body.startswith(b"ICC_PROFILE\x00"):
            out.append(whole)  # colour profile: needed for the right colours
        elif marker == 0xE2 and body.startswith(b"MPF\x00"):
            _note(found, "a list of more pictures stored in the same file (MPF)")
        elif marker == 0xEE and body.startswith(b"Adobe"):
            out.append(whole)  # needed to decode some CMYK/RGB pictures correctly
        elif marker == 0xED:
            _note(found, "IPTC / Photoshop notes (names, captions, keywords)")
        elif 0xE0 <= marker <= 0xEF:
            _note(found, f"extra app data (APP{marker - 0xE0})")
        elif marker == 0xFE:
            _note(found, "a comment")
        else:
            raise BadMeme(f"JPEG has an unusual part (marker {marker:#04x})")
    if not seen_sof:
        raise BadMeme("JPEG has no frame header")
    _trailing(found, len(data) - pos)
    return Picture("jpeg", width, height, b"".join(out), found, warnings)


# --- GIF ----------------------------------------------------------------------------------

def _gif_blocks(data: bytes, pos: int) -> int:
    """Position just after a run of GIF sub-blocks (ending with a zero-length block)."""
    while True:
        if pos >= len(data):
            raise BadMeme("GIF is cut off")
        size = data[pos]
        pos += 1
        if size == 0:
            return pos
        pos += size
        if pos > len(data):
            raise BadMeme("GIF is cut off")


def _scan_gif(data: bytes) -> Picture:
    found: list[str] = []
    if len(data) < 13:
        raise BadMeme("GIF is cut off")
    width, height, packed = struct.unpack("<HHB", data[6:11])
    _check_size("GIF", width, height)
    pos = 13
    if packed & 0x80:
        pos += 3 * (2 ** ((packed & 7) + 1))
    if pos > len(data):
        raise BadMeme("GIF is cut off")
    out = [data[:pos]]
    frames = 0
    parts = 0
    while True:
        parts += 1
        if parts > MAX_PARTS:
            raise BadMeme("GIF has too many parts")
        if pos >= len(data):
            raise BadMeme("GIF is cut off (no end marker)")
        kind = data[pos]
        if kind == 0x3B:
            out.append(b"\x3b")
            pos += 1
            break
        if kind == 0x2C:
            if pos + 10 > len(data):
                raise BadMeme("GIF is cut off")
            local = data[pos + 9]
            end = pos + 10
            if local & 0x80:
                end += 3 * (2 ** ((local & 7) + 1))
            end += 1  # LZW code size
            if end > len(data):
                raise BadMeme("GIF is cut off")
            end = _gif_blocks(data, end)
            out.append(data[pos:end])
            frames += 1
            if frames > MAX_GIF_FRAMES:
                raise BadMeme(f"GIF has too many frames (over {MAX_GIF_FRAMES})")
            pos = end
        elif kind == 0x21:
            if pos + 2 > len(data):
                raise BadMeme("GIF is cut off")
            label = data[pos + 1]
            end = _gif_blocks(data, pos + 2)
            block = data[pos:end]
            if label in (0xF9, 0x01):  # frame timing / transparency; plain-text frame
                out.append(block)
            elif label == 0xFE:
                _note(found, "a comment")
            elif label == 0xFF:
                app_id = data[pos + 3:pos + 14] if data[pos + 2] == 11 else b""
                if app_id in (b"NETSCAPE2.0", b"ANIMEXTS1.0"):
                    out.append(block)  # how many times the animation loops
                elif app_id == b"XMP DataXMP":
                    _note(found, "XMP notes (editing history, names, file paths)")
                else:
                    _note(found, "extra app data")
            else:
                _note(found, "an unknown extra part")
            pos = end
        else:
            raise BadMeme("GIF is damaged (unknown part)")
    if not frames:
        raise BadMeme("GIF has no picture data")
    _trailing(found, len(data) - pos)
    return Picture("gif", width, height, b"".join(out), found, [], frames)


# --- WebP ---------------------------------------------------------------------------------

_WEBP_KEEP = {b"VP8X", b"VP8 ", b"VP8L", b"ALPH", b"ANIM", b"ANMF", b"ICCP"}


def _scan_webp(data: bytes) -> Picture:
    found: list[str] = []
    warnings: list[str] = []
    if len(data) < 20:
        raise BadMeme("WebP is cut off")
    riff_size = struct.unpack("<I", data[4:8])[0]
    end = 8 + riff_size
    if riff_size < 4 or end > len(data):
        raise BadMeme("WebP is cut off")
    pos = 12
    chunks: list[bytes] = []
    width = height = 0
    first = True
    frames = 0
    has_image = False
    while pos < end:
        if len(chunks) > MAX_PARTS:
            raise BadMeme("WebP has too many parts")
        if pos + 8 > end:
            raise BadMeme("WebP is damaged (part header cut off)")
        fourcc = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        padded = size + (size & 1)
        if pos + 8 + size > end:
            raise BadMeme("WebP is cut off or has a part longer than the file")
        body = data[pos + 8:pos + 8 + size]
        whole = data[pos:min(pos + 8 + padded, end)]
        pos += 8 + padded
        if first:
            first = False
            if fourcc == b"VP8X":
                if size < 10:
                    raise BadMeme("WebP header part is too short")
                width = 1 + int.from_bytes(body[4:7], "little")
                height = 1 + int.from_bytes(body[7:10], "little")
            elif fourcc == b"VP8 ":
                if size < 10 or body[3:6] != b"\x9d\x01\x2a":
                    raise BadMeme("WebP picture data is damaged")
                width = struct.unpack("<H", body[6:8])[0] & 0x3FFF
                height = struct.unpack("<H", body[8:10])[0] & 0x3FFF
            elif fourcc == b"VP8L":
                if size < 5 or body[0] != 0x2F:
                    raise BadMeme("WebP picture data is damaged")
                bits = int.from_bytes(body[1:5], "little")
                width = (bits & 0x3FFF) + 1
                height = ((bits >> 14) & 0x3FFF) + 1
            else:
                raise BadMeme("WebP doesn't start with picture data or its header")
            _check_size("WebP", width, height)
        if fourcc in (b"VP8 ", b"VP8L"):
            has_image = True
        if fourcc == b"ANMF":
            frames += 1
            has_image = True
            if frames > MAX_GIF_FRAMES:
                raise BadMeme(f"WebP has too many frames (over {MAX_GIF_FRAMES})")
        if fourcc == b"VP8X":
            if len(chunks):
                raise BadMeme("WebP has its header part in the wrong place")
            # Clear the "has EXIF" (0x08) and "has XMP" (0x04) flags: those parts go.
            flags = body[0] & ~0x0C
            whole = whole[:8] + bytes([flags]) + whole[9:]
            chunks.append(whole)
        elif fourcc in _WEBP_KEEP:
            chunks.append(whole if len(whole) == 8 + padded else whole + b"\x00")
        elif fourcc == b"EXIF":
            tiff = body[6:] if body.startswith(b"Exif\x00\x00") else body
            _camera_data(found, warnings, tiff)
        elif fourcc == b"XMP ":
            _note(found, "XMP notes (editing history, names, file paths)")
        else:
            _note(found, f"unknown extra part ({say(fourcc.decode('latin-1'), 8)})")
    if not has_image:
        raise BadMeme("WebP has no picture data")
    _trailing(found, len(data) - end)
    body = b"WEBP" + b"".join(chunks)
    clean = b"RIFF" + struct.pack("<I", len(body)) + body
    return Picture("webp", width, height, clean, found, warnings, max(frames, 1))


# --- the public functions -----------------------------------------------------------------

def kind_of(data: bytes) -> str | None:
    if data.startswith(_PNG_SIG):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def scan(data: bytes) -> Picture:
    """Read a PNG, JPEG, GIF or WebP and return it with every hidden part removed.
    Raises BadMeme for anything else, anything damaged, and anything too big."""
    if len(data) > MAX_MEME_BYTES:
        raise BadMeme(f"picture is too big ({len(data)} bytes; limit {MAX_MEME_BYTES})")
    kind = kind_of(data)
    if kind is None:
        raise BadMeme("not a PNG, JPEG, GIF or WebP picture")
    return {"png": _scan_png, "jpeg": _scan_jpeg, "gif": _scan_gif, "webp": _scan_webp}[kind](data)


def read_picture(path: str | os.PathLike[str]) -> bytes:
    """A regular file of at most MAX_MEME_BYTES (never reads more; no pipes or devices)."""
    try:
        return json_file.read_bytes(path, "picture", MAX_MEME_BYTES)
    except json_file.UnsafeJSONError as exc:
        raise BadMeme(str(exc)) from None


def _text_problem(value: Any, what: str, limit: int, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        return f"{what} must be text"
    if required and not value.strip():
        return f"{what} is empty"
    if len(value) > limit:
        return f"{what} is longer than {limit} characters (the page cuts it)"
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in value):
        return f"{what} has control or invisible characters"
    return None


def entry_problems(entry: Any, index: int) -> list[str]:
    where = f"gallery.json entry {index + 1}"
    if not isinstance(entry, dict):
        return [f"{where} is not an object"]
    problems = []
    extra = sorted(set(entry) - {"file", "alt", "credit"})
    if extra:
        problems.append(f"{where} has unknown fields: {say(', '.join(extra), 80)}")
    file = entry.get("file")
    prefix = "/" + GALLERY_DIR.as_posix() + "/"
    if not isinstance(file, str) or not file.startswith(prefix) or not NAME_RE.match(file[len(prefix):]):
        problems.append(f"{where}: file must look like {prefix}name.png (letters, numbers, "
                        "dash, underscore; png, jpg, gif or webp)")
    for key, limit, required in (("alt", MAX_ALT_CHARS, True), ("credit", MAX_CREDIT_CHARS, False)):
        p = _text_problem(entry.get(key), f"{where} {key}", limit, required)
        if p:
            problems.append(p)
    return problems


def check(root: Path = SITE_ROOT) -> dict[str, Any]:
    """Every problem with the gallery folder and its list, as plain sentences."""
    folder = root / GALLERY_DIR
    problems: list[str] = []
    pictures: list[dict[str, Any]] = []
    listed: set[str] = set()
    gallery_path = folder / GALLERY_FILE
    try:
        data = json_file.load(gallery_path, "gallery list", MAX_GALLERY_JSON_BYTES)
    except FileNotFoundError:
        data = None
        problems.append(f"{GALLERY_DIR.as_posix()}/{GALLERY_FILE} is missing")
    except json_file.UnsafeJSONError as exc:
        data = None
        problems.append(str(exc))
    entries: list[Any] = []
    if data is not None:
        if not isinstance(data, dict) or not isinstance(data.get("memes"), list):
            problems.append('gallery.json must be an object with a "memes" list')
        else:
            extra = sorted(set(data) - {"_notice", "memes"})
            if extra:
                problems.append(f"gallery.json has unknown fields: {say(', '.join(extra), 80)}")
            entries = data["memes"]
            if len(entries) > MAX_SHOWN:
                problems.append(f"gallery.json lists {len(entries)} memes; the page only "
                                f"shows the first {MAX_SHOWN}")
    for i, entry in enumerate(entries):
        bad = entry_problems(entry, i)
        problems.extend(bad)
        if bad:
            continue
        name = entry["file"].rsplit("/", 1)[1]
        if name in listed:
            problems.append(f"{say(name, 100)} is listed twice")
            continue
        listed.add(name)
        path = folder / name
        if path.is_symlink() or not path.is_file():
            problems.append(f"{say(name, 100)} is listed but isn't a file in the folder")
            continue
        try:
            pic = scan(read_picture(path))
        except BadMeme as exc:
            problems.append(f"{say(name, 100)}: {exc}")
            continue
        if KIND_OF_EXT[name.rsplit(".", 1)[1]] != pic.kind:
            problems.append(f"{say(name, 100)} is really a {pic.kind.upper()}; rename it")
        if not pic.is_clean:
            problems.append(f"{say(name, 100)} still has hidden data: {'; '.join(pic.found)}. "
                            "Add it with `python -m app.meme_gallery add`, which removes it")
        pictures.append({"file": name, **pic.summary()})
    if folder.is_dir():
        for item in sorted(folder.iterdir()):
            if item.name == GALLERY_FILE:
                continue
            if item.name not in listed:
                problems.append(f"{say(item.name, 100)} is in {GALLERY_DIR.as_posix()}/ but not "
                                "in gallery.json (it would still be public on the site and "
                                "GitHub); list it or remove it")
    elif data is not None:
        problems.append(f"{GALLERY_DIR.as_posix()}/ is not a folder")
    return {"version": GALLERY_VERSION, "ok": not problems, "problems": problems,
            "pictures": pictures}


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # a public picture
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def add(source: Path, stem: str, alt: str, credit: str | None = None,
        root: Path = SITE_ROOT) -> dict[str, Any]:
    """Clean `source` and add it to the gallery as `<stem>.<ext>`. Raises BadMeme with a
    plain reason if anything is wrong; changes nothing in that case."""
    if not STEM_RE.match(stem):
        raise BadMeme("--name must be 1-80 letters, numbers, dashes or underscores, "
                      "starting with a letter or number (no extension)")
    for value, what, limit, required in ((alt, "--alt", MAX_ALT_CHARS, True),
                                         (credit, "--credit", MAX_CREDIT_CHARS, False)):
        p = _text_problem(value, what, limit, required)
        if p:
            raise BadMeme(p)
    before = check(root)
    if not before["ok"]:
        raise BadMeme("fix the gallery first: " + " | ".join(before["problems"]))
    original = read_picture(source)
    pic = scan(original)
    name = f"{stem}.{EXT_OF_KIND[pic.kind]}"
    folder = root / GALLERY_DIR
    target = folder / name
    if target.exists() or target.is_symlink():
        raise BadMeme(f"{name} is already in the gallery folder; pick another --name")
    if scan(pic.clean).found:  # never expected; cleaning must be complete in one pass
        raise BadMeme("the cleaned copy still has hidden data; not added")
    gallery_path = folder / GALLERY_FILE
    data = json_file.load(gallery_path, "gallery list", MAX_GALLERY_JSON_BYTES)
    entry: dict[str, str] = {"file": f"/{GALLERY_DIR.as_posix()}/{name}", "alt": alt.strip()}
    if credit and credit.strip():
        entry["credit"] = credit.strip()
    if len(data["memes"]) >= MAX_SHOWN:
        raise BadMeme(f"the gallery already has {MAX_SHOWN} memes, the most the page shows; "
                      "remove one first")
    data["memes"].append(entry)
    _write_atomic(target, pic.clean)
    try:
        _write_atomic(gallery_path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    after = check(root)
    return {"version": GALLERY_VERSION, "added": name, "removed": pic.found,
            "warnings": pic.warnings, "bytes_before": len(original),
            "bytes_after": len(pic.clean), "gallery_ok": after["ok"],
            "problems": after["problems"]}


def _print_pic(name: str, pic: Picture) -> None:
    print(f"{say(name, 120)}: {pic.kind.upper()}, {pic.width} x {pic.height} px"
          + (f", {pic.frames} frames" if pic.frames > 1 else ""))
    if pic.found:
        print("  hidden data (removed when added with `add`):")
        for f in pic.found:
            print(f"    - {f}")
    else:
        print("  no hidden data")
    for w in pic.warnings:
        print(f"  note: {w}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.meme_gallery",
                                 description="Meme Lab gallery pictures (meme-gallery-v1)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_in = sub.add_parser("inspect", help="show what hidden data a picture carries")
    p_in.add_argument("file")
    p_in.add_argument("--json", action="store_true")
    p_add = sub.add_parser("add", help="clean a picture and add it to the gallery")
    p_add.add_argument("file")
    p_add.add_argument("--name", required=True, help="file name without extension, e.g. boat-sunset")
    p_add.add_argument("--alt", required=True, help="what the meme shows and says")
    p_add.add_argument("--credit", help="name or handle the sender gave")
    p_add.add_argument("--root", default=str(SITE_ROOT))
    p_ck = sub.add_parser("check", help="check the gallery folder and list (CI)")
    p_ck.add_argument("--root", default=str(SITE_ROOT))
    p_ck.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "inspect":
        try:
            pic = scan(read_picture(args.file))
        except (BadMeme, FileNotFoundError) as exc:
            print(f"refused: {say(str(exc), 300)}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({"version": GALLERY_VERSION, **pic.summary()}, indent=2))
        else:
            _print_pic(args.file, pic)
        return 0
    if args.cmd == "add":
        try:
            result = add(Path(args.file), args.name, args.alt, args.credit, Path(args.root))
        except (BadMeme, FileNotFoundError, json_file.UnsafeJSONError) as exc:
            print(f"not added: {say(str(exc), 600)}", file=sys.stderr)
            return 2
        print(f"added {result['added']} ({result['bytes_before']} -> {result['bytes_after']} bytes)")
        for f in result["removed"]:
            print(f"  removed: {f}")
        for w in result["warnings"]:
            print(f"  note: {w}")
        print("  gallery check: " + ("ok" if result["gallery_ok"] else "PROBLEMS"))
        for p in result["problems"]:
            print(f"    - {say(p, 400)}")
        print("Look at the picture once more, then commit both files and push.")
        return 0 if result["gallery_ok"] else 1
    result = check(Path(args.root))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for p in result["problems"]:
            print(f"FAIL {say(p, 400)}")
        print(f"meme gallery: {len(result['pictures'])} picture(s), "
              + ("ok" if result["ok"] else f"{len(result['problems'])} problem(s)"))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
