#!/usr/bin/env python3
"""Structurally validate DNG files: prints "OK <path>" / "BAD <path> <reason>" per argument.

Checks, without decoding pixels:
  - TIFF header + DNGVersion (tag 0xC612) in IFD0
  - a SubIFD with PhotometricInterpretation 32803 (CFA) or 34892 (LinearRaw)
  - that every tile/strip's [offset, offset+bytecount) lies inside the file, i.e. the
    raw payload is fully written (catches truncated / interrupted conversions)
Exit status is 1 if any file is BAD.
"""
import mmap
import struct
import sys

TAG_SUBIFDS = 0x014A
TAG_PHOTOMETRIC = 0x0106
TAG_STRIP_OFFSETS = 0x0111
TAG_STRIP_COUNTS = 0x0117
TAG_TILE_OFFSETS = 0x0144
TAG_TILE_COUNTS = 0x0145
TAG_DNG_VERSION = 0xC612

TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4,
             16: 8, 17: 8, 18: 8}
INT_TYPES = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 13: "I", 16: "Q", 17: "q", 18: "Q"}


class Bad(Exception):
    pass


def read_ifd(buf, endian, off):
    """Return {tag: (type, count, value_offset_or_inline)} and the next-IFD offset."""
    if off + 2 > len(buf):
        raise Bad("ifd offset past eof")
    (n,) = struct.unpack_from(endian + "H", buf, off)
    entries = {}
    base = off + 2
    if base + 12 * n + 4 > len(buf):
        raise Bad("ifd truncated")
    for i in range(n):
        tag, typ, cnt = struct.unpack_from(endian + "HHI", buf, base + 12 * i)
        entries[tag] = (typ, cnt, base + 12 * i + 8)
    (nxt,) = struct.unpack_from(endian + "I", buf, base + 12 * n)
    return entries, nxt


def values(buf, endian, entry):
    typ, cnt, voff = entry
    if typ not in INT_TYPES:
        raise Bad(f"unsupported tag type {typ}")
    isize = TYPE_SIZE[typ]
    total = isize * cnt
    if total > 4:
        (doff,) = struct.unpack_from(endian + "I", buf, voff)
    else:
        doff = voff
    if doff + total > len(buf):
        raise Bad("tag data past eof")
    return list(struct.unpack_from(f"{endian}{cnt}{INT_TYPES[typ]}", buf, doff))


def check(path):
    with open(path, "rb") as fh:
        if fh.seek(0, 2) < 16:
            raise Bad("file too small")
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as buf:
            _check(buf)


def _check(buf):
    size = len(buf)
    if size < 16:
        raise Bad("file too small")
    magic = buf[:2]
    if magic == b"II":
        endian = "<"
    elif magic == b"MM":
        endian = ">"
    else:
        raise Bad("not TIFF")
    ver, ifd0 = struct.unpack_from(endian + "HI", buf, 2)
    if ver != 42:
        raise Bad(f"unexpected TIFF version {ver}")

    ifd, _ = read_ifd(buf, endian, ifd0)
    if TAG_DNG_VERSION not in ifd:
        raise Bad("no DNGVersion tag")

    # Collect IFD0 plus its SubIFDs, find the raw one.
    candidates = [ifd]
    if TAG_SUBIFDS in ifd:
        for sub in values(buf, endian, ifd[TAG_SUBIFDS]):
            candidates.append(read_ifd(buf, endian, sub)[0])

    raw = None
    for cand in candidates:
        if TAG_PHOTOMETRIC in cand and values(buf, endian, cand[TAG_PHOTOMETRIC])[0] in (32803, 34892):
            raw = cand
            break
    if raw is None:
        raise Bad("no CFA/LinearRaw SubIFD")

    if TAG_TILE_OFFSETS in raw:
        offs = values(buf, endian, raw[TAG_TILE_OFFSETS])
        cnts = values(buf, endian, raw[TAG_TILE_COUNTS])
    elif TAG_STRIP_OFFSETS in raw:
        offs = values(buf, endian, raw[TAG_STRIP_OFFSETS])
        cnts = values(buf, endian, raw[TAG_STRIP_COUNTS])
    else:
        raise Bad("raw IFD has no tile/strip offsets")
    if not offs or len(offs) != len(cnts):
        raise Bad("tile offset/count mismatch")
    if any(c == 0 for c in cnts):
        raise Bad("zero-length tile")
    end = max(o + c for o, c in zip(offs, cnts))
    if end > size:
        raise Bad(f"raw data truncated: needs {end} bytes, file is {size}")


def main(argv):
    rc = 0
    for path in argv:
        try:
            check(path)
        except FileNotFoundError:
            print(f"BAD {path} missing")
            rc = 1
        except Bad as exc:
            print(f"BAD {path} {exc}")
            rc = 1
        except Exception as exc:  # malformed beyond the checks above
            print(f"BAD {path} parse error: {exc}")
            rc = 1
        else:
            print(f"OK {path}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
