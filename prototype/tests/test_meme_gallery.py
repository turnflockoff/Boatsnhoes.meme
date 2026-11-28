"""Meme Lab gallery pictures (`meme-gallery-v1`, code audit twenty-third pass).

Pictures from strangers become public on the site and GitHub. These tests build small real
pictures (made once with an image library, stored here as base64) and add the hidden data
phones and editors add, by hand, so every byte the tests rely on is visible here.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import struct
import zlib
from pathlib import Path

import pytest

from app import meme_gallery as mg

SITE = Path(__file__).resolve().parents[2]

JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERX"
    "RTc4UG1RV19iZ2hnPk1xeXBkeFxlZ2P/2wBDARESEhgVGC8aGi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2Nj"
    "Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2P/wAARCAADAAQDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAEC"
    "AwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2Jy"
    "ggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWW"
    "l5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMi"
    "MoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0"
    "dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn"
    "6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwBKKKK8U+rP/9k=")
JPEG_PROGRESSIVE = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERX"
    "RTc4UG1RV19iZ2hnPk1xeXBkeFxlZ2P/2wBDARESEhgVGC8aGi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2Nj"
    "Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2P/wgARCAADAAQDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAA"
    "AAH/xAAVAQEBAAAAAAAAAAAAAAAAAAAEBf/aAAwDAQACEAMQAAABgFV//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/a"
    "AAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/a"
    "AAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/a"
    "AAgBAQABPyF//9oADAMBAAIAAwAAABD3/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPxB//8QAFBEBAAAA"
    "AAAAAAAAAAAAAAAAAP/aAAgBAgEBPxB//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxB//9k=")
WEBP_LOSSY = base64.b64decode(
    "UklGRjoAAABXRUJQVlA4IC4AAADwAQCdASoEAAMAAsBMJaACdLoB+AAFQIAA/Mr/4s5GI7PN/9CT8En4JP3AAAAA")
WEBP_LOSSLESS = base64.b64decode("UklGRh4AAABXRUJQVlA4TBEAAAAvA4AAAAdQqP7Xo/+BiOh/AAA=")
GIF = base64.b64decode(
    "R0lGODlhBAADAIEAAP9QHgAAAAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQACgAAACwAAAAABAADAAAICAAB"
    "CBxIUGBAACH5BAEKAAEALAAAAAAEAAMAgQAAAAAAAAAAAAAAAAgIAAEIHEhQYEAAOw==")


def png_chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body))


def make_png(w: int = 4, h: int = 3, extra: list[bytes] | None = None) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x50\x1e" * w for _ in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr) + b"".join(extra or [])
            + png_chunk(b"IDAT", zlib.compress(raw)) + png_chunk(b"IEND", b""))


def tiff_exif(gps: bool = True, orientation: int = 1, thumb: bool = True) -> bytes:
    """A little-endian TIFF block like a phone writes: make, orientation, date, GPS pointer,
    and a second directory (the preview copy)."""
    entries = [(0x010F, 2, 6, b"Phone\x00"), (0x0112, 3, 1, struct.pack("<H", orientation) + b"\x00\x00"),
               (0x0132, 2, 20, b"2026:11:28 10:00:00\x00")]
    if gps:
        entries.append((0x8825, 4, 1, None))
    entries.sort()
    ifd0 = 8
    data_at = ifd0 + 2 + 12 * len(entries) + 4
    blob = b""
    out = b""
    gps_at = None
    for tag, typ, count, value in entries:
        if value is None:
            gps_at = data_at + len(blob)
            blob += struct.pack("<H", 1) + struct.pack("<HHII", 0x0002, 5, 3, 0) + b"\x00" * 4  # GPSLatitude
            out += struct.pack("<HHI", tag, typ, count) + struct.pack("<I", gps_at)
        elif len(value) <= 4:
            out += struct.pack("<HHI", tag, typ, count) + value.ljust(4, b"\x00")
        else:
            out += struct.pack("<HHI", tag, typ, count) + struct.pack("<I", data_at + len(blob))
            blob += value
    ifd1 = data_at + len(blob) if thumb else 0
    tail = (struct.pack("<H", 0) + struct.pack("<I", 0)) if thumb else b""
    return b"II*\x00" + struct.pack("<I", ifd0) + struct.pack("<H", len(entries)) + out + \
        struct.pack("<I", ifd1) + blob + tail


def jpeg_segment(marker: int, body: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


def with_jpeg_parts(base: bytes, parts: list[bytes], tail: bytes = b"") -> bytes:
    return base[:2] + b"".join(parts) + base[2:] + tail


PHONE_PARTS = [
    jpeg_segment(0xE1, b"Exif\x00\x00" + tiff_exif()),
    jpeg_segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>C:\\Users\\Real Name\\</x:xmpmeta>"),
    jpeg_segment(0xE2, b"MPF\x00" + b"\x00" * 8),
    jpeg_segment(0xED, b"Photoshop 3.0\x00" + b"8BIM author"),
    jpeg_segment(0xFE, b"secret comment"),
]


def riff(chunks: list[bytes]) -> bytes:
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_chunk(fourcc: bytes, body: bytes) -> bytes:
    return fourcc + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) & 1 else b"")


def webp_with_metadata(base: bytes = WEBP_LOSSY, w: int = 4, h: int = 3) -> bytes:
    image = base[12:]
    vp8x = bytes([0x08 | 0x04, 0, 0, 0]) + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    return riff([webp_chunk(b"VP8X", vp8x), image, webp_chunk(b"EXIF", tiff_exif()),
                 webp_chunk(b"XMP ", b"<x:xmpmeta>me</x:xmpmeta>")])


def gif_with_metadata() -> bytes:
    head = 13 + 3 * 2 ** ((GIF[10] & 7) + 1)
    comment = b"\x21\xfe" + bytes([9]) + b"my secret" + b"\x00"
    xmp = b"\x21\xff\x0bXMP DataXMP" + bytes([5]) + b"<xmp>" + b"\x00"
    return GIF[:head] + comment + xmp + GIF[head:] + b"PK\x03\x04hidden zip"


# --- clean pictures pass through untouched ---------------------------------------------------

@pytest.mark.parametrize("data,kind", [(make_png(), "png"), (JPEG, "jpeg"), (JPEG_PROGRESSIVE, "jpeg"),
                                       (WEBP_LOSSY, "webp"), (WEBP_LOSSLESS, "webp"), (GIF, "gif")])
def test_a_clean_picture_is_kept_byte_for_byte(data, kind):
    pic = mg.scan(data)
    assert pic.kind == kind and (pic.width, pic.height) == (4, 3)
    assert pic.is_clean and pic.clean == data


def test_gif_frames_are_counted():
    assert mg.scan(GIF).frames == 2


# --- hidden data is found and removed ---------------------------------------------------------

def test_phone_jpeg_loses_location_camera_notes_and_the_extra_video():
    data = with_jpeg_parts(JPEG, PHONE_PARTS, tail=b"\x00\x00\x00\x18ftypmp42 motion photo video")
    pic = mg.scan(data)
    found = " | ".join(pic.found)
    for words in ("GPS location", "which camera or phone", "when it was taken", "preview copy",
                  "XMP", "MPF", "IPTC", "comment", "extra data after the end"):
        assert words in found, words
    assert pic.clean == JPEG
    assert b"Real Name" not in pic.clean and b"Phone" not in pic.clean
    assert mg.scan(pic.clean).is_clean


def test_jfif_preview_copy_is_dropped_but_the_header_kept():
    thumb = bytes([1, 1]) + b"\x10\x20\x30"
    jfif = jpeg_segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01" + thumb)
    data = JPEG[:2] + jfif + JPEG[20:]  # replace the 16-byte-body JFIF header
    pic = mg.scan(data)
    assert any("JFIF" in f for f in pic.found)
    assert pic.clean[2:20] == JPEG[2:20] and pic.clean == JPEG


def test_turned_photo_is_noted():
    data = with_jpeg_parts(JPEG, [jpeg_segment(0xE1, b"Exif\x00\x00" + tiff_exif(orientation=6))])
    pic = mg.scan(data)
    assert pic.warnings and "orientation 6" in pic.warnings[0]
    assert not mg.scan(with_jpeg_parts(JPEG, PHONE_PARTS[:1])).warnings


def test_colour_profile_and_adobe_parts_are_kept_in_jpeg():
    icc = jpeg_segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + b"profile")
    adobe = jpeg_segment(0xEE, b"Adobe\x00\x64\x00\x00\x00\x00\x01")
    data = with_jpeg_parts(JPEG, [icc, adobe])
    pic = mg.scan(data)
    assert pic.is_clean and pic.clean == data


def test_png_text_camera_data_time_and_trailer_are_removed():
    extra = [png_chunk(b"tEXt", b"Author\x00Real Name"), png_chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00x"),
             png_chunk(b"zTXt", b"c\x00\x00" + zlib.compress(b"x")), png_chunk(b"eXIf", tiff_exif()),
             png_chunk(b"tIME", b"\x07\xea\x0b\x1c\x0a\x00\x00"), png_chunk(b"prVt", b"private")]
    keep = [png_chunk(b"sRGB", b"\x00"), png_chunk(b"pHYs", b"\x00\x00\x0b\x13\x00\x00\x0b\x13\x01")]
    data = make_png(extra=keep + extra) + b"trailing"
    pic = mg.scan(data)
    found = " | ".join(pic.found)
    for words in ("tEXt", "iTXt", "zTXt", "GPS location", "tIME", "prVt", "extra data after"):
        assert words in found, words
    assert pic.clean == make_png(extra=keep)


def test_webp_loses_exif_and_xmp_and_its_flags():
    data = webp_with_metadata()
    pic = mg.scan(data)
    assert any("GPS" in f for f in pic.found) and any("XMP" in f for f in pic.found)
    assert b"EXIF" not in pic.clean and b"XMP " not in pic.clean
    assert pic.clean[20] & 0x0C == 0  # VP8X flags say there is no EXIF or XMP any more
    again = mg.scan(pic.clean)
    assert again.is_clean and (again.width, again.height) == (4, 3)
    assert struct.unpack("<I", pic.clean[4:8])[0] == len(pic.clean) - 8


def test_webp_trailing_data_is_removed():
    pic = mg.scan(WEBP_LOSSLESS + b"PK\x03\x04zip")
    assert any("extra data" in f for f in pic.found) and pic.clean == WEBP_LOSSLESS


def test_gif_comment_xmp_and_hidden_zip_are_removed_but_the_loop_kept():
    pic = mg.scan(gif_with_metadata())
    found = " | ".join(pic.found)
    assert "comment" in found and "XMP" in found and "extra data" in found
    assert pic.clean == GIF and b"NETSCAPE2.0" in pic.clean


# --- broken, odd and oversized files are refused ----------------------------------------------

@pytest.mark.parametrize("data", [
    b"", b"not a picture", b"<svg onload=alert(1)>", b"%PDF-1.7", b"PK\x03\x04",
    JPEG[:-2], JPEG[:300], make_png()[:-12], make_png()[:40], GIF[:-1], GIF[:20], WEBP_LOSSY[:30],
])
def test_damaged_or_other_files_are_refused(data):
    with pytest.raises(mg.BadMeme):
        mg.scan(data)


def test_png_checksum_and_unknown_required_part_are_refused():
    bad = bytearray(make_png())
    bad[-20] ^= 0xFF  # inside the IDAT data
    with pytest.raises(mg.BadMeme, match="checksum"):
        mg.scan(bytes(bad))
    with pytest.raises(mg.BadMeme, match="required"):
        mg.scan(make_png(extra=[png_chunk(b"ZZZZ", b"x")]))


@pytest.mark.parametrize("w,h", [(9000, 10), (10, 9000), (7000, 7000), (0, 5)])
def test_pictures_too_big_to_show_safely_are_refused(w, h):
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    data = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr) + png_chunk(b"IDAT", zlib.compress(b""))
            + png_chunk(b"IEND", b""))
    with pytest.raises(mg.BadMeme):
        mg.scan(data)


def test_jpeg_claiming_a_huge_size_is_refused():
    sof = JPEG.index(b"\xff\xc0")
    data = JPEG[:sof + 5] + struct.pack(">HH", 60000, 60000) + JPEG[sof + 9:]
    with pytest.raises(mg.BadMeme, match="too big"):
        mg.scan(data)


def test_file_over_the_form_limit_is_refused():
    with pytest.raises(mg.BadMeme, match="too big"):
        mg.scan(JPEG + b"\x00" * mg.MAX_MEME_BYTES)


def test_read_picture_refuses_a_pipe_and_a_folder(tmp_path):
    import os
    fifo = tmp_path / "pipe.png"
    os.mkfifo(fifo)
    with pytest.raises(mg.BadMeme):
        mg.read_picture(fifo)
    with pytest.raises(mg.BadMeme):
        mg.read_picture(tmp_path)


# --- the gallery folder check ----------------------------------------------------------------

def site(tmp_path: Path, memes: list[dict] | None = None, files: dict[str, bytes] | None = None) -> Path:
    folder = tmp_path / "assets" / "memes"
    folder.mkdir(parents=True)
    (folder / "gallery.json").write_text(json.dumps({"_notice": "x", "memes": memes or []}))
    for name, data in (files or {}).items():
        (folder / name).write_bytes(data)
    return tmp_path


def test_the_real_gallery_passes():
    result = mg.check(SITE)
    assert result["ok"], result["problems"]


def test_empty_gallery_passes(tmp_path):
    assert mg.check(site(tmp_path))["ok"]


def test_a_photo_with_location_saved_straight_into_the_folder_is_refused(tmp_path):
    data = with_jpeg_parts(JPEG, PHONE_PARTS)
    root = site(tmp_path, [{"file": "/assets/memes/a.jpg", "alt": "boat"}], {"a.jpg": data})
    result = mg.check(root)
    assert not result["ok"]
    assert any("hidden data" in p and "GPS" in p for p in result["problems"])


@pytest.mark.parametrize("memes,files,words", [
    ([], {"a.png": make_png()}, "not in gallery.json"),
    ([], {".DS_Store": b"junk"}, "not in gallery.json"),
    ([{"file": "/assets/memes/a.png", "alt": "x"}], {}, "isn't a file"),
    ([{"file": "/assets/memes/a.png", "alt": "x"}] * 2, {"a.png": make_png()}, "listed twice"),
    ([{"file": "/assets/memes/a.png", "alt": " "}], {"a.png": make_png()}, "alt is empty"),
    ([{"file": "/assets/memes/a.png"}], {"a.png": make_png()}, "alt must be text"),
    ([{"file": "/assets/memes/a.png", "alt": "x" * 301}], {"a.png": make_png()}, "longer than 300"),
    ([{"file": "/assets/memes/a.png", "alt": "x", "credit": "y" * 61}], {"a.png": make_png()}, "longer than 60"),
    ([{"file": "/assets/memes/a.png", "alt": "x\u202eevil"}], {"a.png": make_png()}, "invisible"),
    ([{"file": "/assets/memes/a.png", "alt": "x", "onclick": "y"}], {"a.png": make_png()}, "unknown fields"),
    ([{"file": "/assets/memes/../a.png", "alt": "x"}], {}, "file must look like"),
    ([{"file": "https://evil.example/a.png", "alt": "x"}], {}, "file must look like"),
    ([{"file": "/assets/memes/a.PNG", "alt": "x"}], {"a.PNG": make_png()}, "file must look like"),
    ([{"file": "/assets/memes/a.png\n", "alt": "x"}], {}, "file must look like"),
    ([{"file": "/assets/memes/a.jpg", "alt": "x"}], {"a.jpg": make_png()}, "really a PNG"),
    ([{"file": "/assets/memes/a.png", "alt": "x"}], {"a.png": b"<svg/>"}, "not a PNG, JPEG"),
])
def test_gallery_problems_are_named(tmp_path, memes, files, words):
    result = mg.check(site(tmp_path, memes, files))
    assert not result["ok"]
    assert any(words in p for p in result["problems"]), result["problems"]


def test_a_link_in_the_folder_is_refused(tmp_path):
    root = site(tmp_path, [{"file": "/assets/memes/a.png", "alt": "x"}])
    outside = tmp_path / "secret.png"
    outside.write_bytes(make_png())
    (root / "assets" / "memes" / "a.png").symlink_to(outside)
    assert not mg.check(root)["ok"]


def test_more_memes_than_the_page_shows_is_a_problem(tmp_path):
    memes = [{"file": f"/assets/memes/m{i}.png", "alt": "x"} for i in range(61)]
    files = {f"m{i}.png": make_png() for i in range(61)}
    result = mg.check(site(tmp_path, memes, files))
    assert any("only shows the first 60" in p for p in result["problems"])


@pytest.mark.parametrize("raw", [b"[]", b'{"memes": {}}', b'{"memes": [], "memes": []}', b"{" * 100, b"\xff"])
def test_odd_gallery_lists_are_refused(tmp_path, raw):
    root = site(tmp_path)
    (root / "assets" / "memes" / "gallery.json").write_bytes(raw)
    assert not mg.check(root)["ok"]


def test_the_page_filter_accepts_every_name_the_check_accepts():
    # The page (assets/meme-lab.js) hides anything its own pattern doesn't match; the check
    # must never pass a name the page would quietly hide.
    js = (SITE / "assets" / "meme-lab.js").read_text()
    m = re.search(r"\.test\(m\.file\)", js)
    assert m, "gallery filter not found in meme-lab.js"
    page = re.compile(r"^/assets/memes/[A-Za-z0-9._-]+\.(png|jpe?g|gif|webp)$")
    for name in ("a.png", "boat-sunset_2.jpg", "x.y.jpeg", "A1.gif", "z.webp"):
        assert mg.NAME_RE.match(name) and page.match("/assets/memes/" + name)
    assert "/^\\/assets\\/memes\\/[A-Za-z0-9._-]+\\.(png|jpe?g|gif|webp)$/" in js


# --- adding a picture ------------------------------------------------------------------------

def test_add_cleans_the_picture_and_lists_it(tmp_path):
    root = site(tmp_path)
    src = tmp_path / "from-the-form.jpg"
    src.write_bytes(with_jpeg_parts(JPEG, PHONE_PARTS, tail=b"video"))
    result = mg.add(src, "boat-sunset", "The boat at sunset, top text: BOATS", "sailor", root)
    assert result["added"] == "boat-sunset.jpg" and result["gallery_ok"]
    assert any("GPS" in r for r in result["removed"])
    saved = (root / "assets" / "memes" / "boat-sunset.jpg").read_bytes()
    assert saved == JPEG
    listed = json.loads((root / "assets" / "memes" / "gallery.json").read_text())
    assert listed["memes"] == [{"file": "/assets/memes/boat-sunset.jpg",
                                "alt": "The boat at sunset, top text: BOATS", "credit": "sailor"}]
    assert listed["_notice"] == "x"
    assert mg.check(root)["ok"]


def test_add_names_the_file_by_what_it_really_is(tmp_path):
    root = site(tmp_path)
    src = tmp_path / "meme.jpg"
    src.write_bytes(make_png())  # a PNG with the wrong extension
    assert mg.add(src, "m", "alt", None, root)["added"] == "m.png"


@pytest.mark.parametrize("stem,alt,credit,words", [
    ("../x", "a", None, "--name"), ("x.png", "a", None, "--name"), ("x\n", "a", None, "--name"), ("", "a", None, "--name"),
    ("ok", "", None, "--alt is empty"), ("ok", "a" * 301, None, "--alt"), ("ok", "a", "c" * 61, "--credit"),
    ("ok", "a\x1b[2J", None, "control"),
])
def test_add_refuses_bad_names_and_text(tmp_path, stem, alt, credit, words):
    root = site(tmp_path)
    src = tmp_path / "s.png"
    src.write_bytes(make_png())
    with pytest.raises(mg.BadMeme, match=re.escape(words)):
        mg.add(src, stem, alt, credit, root)
    assert sorted(p.name for p in (root / "assets" / "memes").iterdir()) == ["gallery.json"]


def test_add_never_overwrites_and_refuses_damaged_files(tmp_path):
    root = site(tmp_path)
    src = tmp_path / "s.png"
    src.write_bytes(make_png())
    mg.add(src, "one", "alt", None, root)
    with pytest.raises(mg.BadMeme, match="already"):
        mg.add(src, "one", "alt", None, root)
    src.write_bytes(make_png()[:50])
    with pytest.raises(mg.BadMeme):
        mg.add(src, "two", "alt", None, root)
    assert len(json.loads((root / "assets" / "memes" / "gallery.json").read_text())["memes"]) == 1


def test_add_refuses_to_build_on_a_broken_gallery(tmp_path):
    root = site(tmp_path, [], {"stray.jpg": with_jpeg_parts(JPEG, PHONE_PARTS)})
    src = tmp_path / "s.png"
    src.write_bytes(make_png())
    with pytest.raises(mg.BadMeme, match="fix the gallery first"):
        mg.add(src, "new", "alt", None, root)


# --- command line ----------------------------------------------------------------------------

def test_cli_inspect_check_and_add(tmp_path, capsys):
    root = site(tmp_path)
    src = tmp_path / "s.jpg"
    src.write_bytes(with_jpeg_parts(JPEG, PHONE_PARTS))
    assert mg.main(["inspect", str(src)]) == 0
    out = capsys.readouterr().out
    assert "GPS location" in out and "JPEG, 4 x 3" in out
    assert mg.main(["inspect", "--json", str(src)]) == 0
    assert json.loads(capsys.readouterr().out)["is_clean"] is False
    assert mg.main(["add", str(src), "--name", "n", "--alt", "a boat", "--root", str(root)]) == 0
    added = capsys.readouterr().out
    assert "added n.jpg" in added and "GPS location" in added
    assert mg.main(["check", "--root", str(root)]) == 0
    shutil.copy(src, root / "assets" / "memes" / "raw.jpg")
    assert mg.main(["check", "--root", str(root)]) == 1
    assert "FAIL" in capsys.readouterr().out
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x1b]0;owned\x07 not a picture")
    assert mg.main(["inspect", str(bad)]) == 2


def test_ci_runs_the_gallery_check():
    wf = (SITE / ".github" / "workflows" / "prototype-ci.yml").read_text()
    assert "python -m app.meme_gallery check" in wf


def test_the_meme_lab_page_says_hidden_data_is_removed():
    page = (SITE / "meme-lab.html").read_text()
    assert "location" in page and "removed" in page.lower()


def test_damaged_copies_are_refused_or_cleaned_never_crash():
    # Seeded random damage to every sample; each must be refused with BadMeme or come back
    # with a copy that is itself clean (cleaning twice changes nothing).
    import random
    rng = random.Random(23)
    seeds = [make_png(), JPEG, JPEG_PROGRESSIVE, WEBP_LOSSY, WEBP_LOSSLESS, GIF,
             with_jpeg_parts(JPEG, PHONE_PARTS, b"tail"), webp_with_metadata(), gif_with_metadata()]
    for _ in range(3000):
        d = bytearray(rng.choice(seeds))
        for _ in range(rng.randint(1, 6)):
            pos = rng.randrange(len(d))
            op = rng.random()
            if op < 0.5:
                d[pos] = rng.randrange(256)
            elif op < 0.75:
                del d[pos:pos + rng.randint(1, 32)]
            else:
                d[pos:pos] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 8)))
            if not d:
                d = bytearray(b"\x89")
        try:
            pic = mg.scan(bytes(d))
        except mg.BadMeme:
            continue
        again = mg.scan(pic.clean)
        assert again.is_clean and again.clean == pic.clean
