#!/usr/bin/env python3
"""Offload a Nikon camera or card to fw, then replicate and verify to d.

Identity is the file's CONTENT, never its name. Nikon's DSC_#### counter wraps at 9999 and can
be reset in-camera, and the PTP "new file" flag is not durable across camera, card and software
operations. So every candidate is hashed and looked up in a SQLite ledger; a hash already
present is a duplicate no matter what it is called, and a known name with new content is a new
file.

Stages, in order, with nothing destructive at any point:

  1. stage    copy/download into a fresh session directory (never merged with another session)
  2. hash     BLAKE2b-256 over the staged bytes
  3. ledger   skip hashes already archived, after revalidating that the archived copy still
              exists and still matches; a stale row is dropped and the file re-imported
  4. archive  move into ARCHIVE/YYYY/YYYY-MM-DD/
  5. verify   re-hash at the destination; a mismatch aborts that file
  6. replicate rsync to the remote, then re-check with `rsync -aic --dry-run`, honouring its exit
              status as well as its output, so a failed probe can never read as success
  7. report   per-run inventory reconciliation: staged vs accounted vs rejected, and verified
              counts per file extension

This tool does NOT decide whether a card can be cleared, and deliberately emits no
"safe to format" verdict. Authorising a destructive action would require reconciling against the
camera's own PTP inventory per storage slot, which is not implemented or hardware-tested. Videos
are copied and hashed but have no structural integrity check. Inspect the archive yourself before
erasing anything; the card is never written to or erased by this script.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

RAW_EXT = {'.nef', '.nrw'}
JPEG_EXT = {'.jpg', '.jpeg'}
VIDEO_EXT = {'.mov', '.mp4'}
WANTED = RAW_EXT | JPEG_EXT | VIDEO_EXT
CHUNK = 4 << 20


def tiff_extent_ok(path: Path) -> bool:
    """Every strip/tile of every IFD must lie inside the file.

    NEF is TIFF-based, so a transfer cut short leaves offsets pointing past EOF even when the
    header and EXIF still parse. Hashing alone cannot catch this: a truncated file simply has a
    different, perfectly valid-looking hash.
    """
    import struct
    size = path.stat().st_size
    with path.open('rb') as fh:
        head = fh.read(8)
        if len(head) < 8 or head[:2] not in (b'II', b'MM'):
            return False
        end = '<' if head[:2] == b'II' else '>'
        magic, first = struct.unpack(end + 'HI', head[2:8])
        if magic != 42:
            return False

        def ifd(off, depth=0):
            """Yield (tag, type, count, value_or_offset) and follow SubIFDs."""
            seen = 0
            while off and 0 < off < size and depth < 4 and seen < 32:
                seen += 1
                fh.seek(off)
                raw = fh.read(2)
                if len(raw) < 2:
                    return
                n = struct.unpack(end + 'H', raw)[0]
                body = fh.read(12 * n + 4)
                if len(body) < 12 * n:
                    return
                entries = {}
                for i in range(n):
                    tag, typ, cnt = struct.unpack_from(end + 'HHI', body, 12 * i)
                    entries[tag] = (typ, cnt, off + 2 + 12 * i + 8)
                yield entries
                for sub_tag in (0x014A,):                      # SubIFDs
                    if sub_tag in entries:
                        for s in ints(entries[sub_tag]):
                            yield from ifd(s, depth + 1)
                nxt = struct.unpack_from(end + 'I', body, 12 * n)[0] if len(body) >= 12 * n + 4 else 0
                off = nxt

        def ints(entry):
            """Tag values, or None when the array itself lies outside the file (i.e. truncated)."""
            typ, cnt, pos = entry
            width = {1: 1, 3: 2, 4: 4, 13: 4, 16: 8}.get(typ)
            if width is None:
                return []                       # type we do not decode: not evidence of damage
            total = width * cnt
            if total > 4:
                fh.seek(pos)
                base = struct.unpack(end + 'I', fh.read(4))[0]
            else:
                base = pos
            if base + total > size:
                return None                     # the tag data itself is past EOF
            fh.seek(base)
            buf = fh.read(total)
            fmt = {1: 'B', 3: 'H', 4: 'I', 13: 'I', 16: 'Q'}[typ]
            return list(struct.unpack(f'{end}{cnt}{fmt}', buf))

        # Positive proof, not absence of evidence: at least one non-empty strip/tile must be
        # declared AND lie wholly inside the file. Real NEFs legitimately carry SubIFDs with
        # zero-length placeholder strips, so those are skipped rather than treated as damage.
        found = 0
        try:
            for entries in ifd(first):
                for off_tag, len_tag in ((0x0111, 0x0117), (0x0144, 0x0145)):
                    if off_tag not in entries or len_tag not in entries:
                        continue
                    offs, lens = ints(entries[off_tag]), ints(entries[len_tag])
                    if offs is None or lens is None:
                        return False            # offset/length array past EOF: truncated
                    if not offs or len(offs) != len(lens):
                        continue                # odd but carries no data to lose
                    for o, l in zip(offs, lens):
                        if l <= 0:
                            continue            # empty placeholder entry
                        if o + l > size:
                            return False
                        found += 1
        except (struct.error, OSError):
            return False
        return found > 0


def jpeg_ok(path: Path) -> bool:
    size = path.stat().st_size
    if size < 128:
        return False
    with path.open('rb') as fh:
        if fh.read(2) != b'\xff\xd8':
            return False
        fh.seek(max(0, size - 64))
        return b'\xff\xd9' in fh.read()


def integrity_ok(path: Path) -> tuple[bool, str]:
    ext = path.suffix.lower()
    if ext in RAW_EXT:
        return (True, '') if tiff_extent_ok(path) else (False, 'raw data past EOF (truncated?)')
    if ext in JPEG_EXT:
        return (True, '') if jpeg_ok(path) else (False, 'missing JPEG end marker (truncated?)')
    return True, 'unchecked'

def sha(path: Path) -> str:
    h = hashlib.blake2b(digest_size=32)
    with path.open('rb') as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def db_open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute("""CREATE TABLE IF NOT EXISTS asset (
        content_hash TEXT PRIMARY KEY,
        camera_folder TEXT, camera_filename TEXT, size INTEGER,
        captured TEXT, serial TEXT,
        local_path TEXT, verified_local INTEGER DEFAULT 0,
        remote_path TEXT, verified_remote INTEGER DEFAULT 0,
        imported_at TEXT,
        immich_uploaded INTEGER DEFAULT 0)""")
    # Migration for ledgers created before Immich upload existed.
    cols = {r[1] for r in db.execute('PRAGMA table_info(asset)')}
    if 'immich_uploaded' not in cols:
        db.execute('ALTER TABLE asset ADD COLUMN immich_uploaded INTEGER DEFAULT 0')
    db.commit()
    return db


def exif(paths: list[Path]) -> dict[str, dict[str, str]]:
    """DateTimeOriginal and SerialNumber per file, one exiftool call."""
    if not paths:
        return {}
    out = subprocess.run(
        ['exiftool', '-q', '-T', '-p', '$FilePath\t$DateTimeOriginal\t$SerialNumber', *map(str, paths)],
        capture_output=True, text=True).stdout
    meta = {}
    for line in out.strip().splitlines():
        parts = line.split('\t')
        if len(parts) == 3:
            meta[parts[0]] = {'captured': parts[1], 'serial': parts[2]}
    return meta


def canonical(src: Path, digest: str, captured: str, serial: str) -> tuple[str, str]:
    """(YYYY-MM-DD, filename). Capture time plus a hash prefix, so no counter collision."""
    stamp = None
    if captured and captured != '-':
        try:
            stamp = dt.datetime.strptime(captured[:19], '%Y:%m:%d %H:%M:%S')
        except ValueError:
            stamp = None
    if stamp is None:                       # fall back to mtime rather than guessing
        stamp = dt.datetime.fromtimestamp(src.stat().st_mtime)
    sn = (serial if serial and serial != '-' else 'unknown')
    name = f"{stamp:%Y%m%d-%H%M%S}_{sn}_{digest[:8]}{src.suffix.upper()}"
    return f"{stamp:%Y-%m-%d}", name


def stage_from_dir(source: Path, session: Path) -> list[Path]:
    staged = []
    for src in sorted(source.rglob('*')):
        if not src.is_file() or src.suffix.lower() not in WANTED:
            continue
        rel = src.relative_to(source)
        dst = session / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if dst.stat().st_size != src.stat().st_size:
            print(f"  ! short copy, skipping: {rel}", file=sys.stderr)
            dst.unlink()
            continue
        staged.append(dst)
    return staged


def preflight_camera() -> None:
    """Refuse to start if another process owns the camera, and say which one.

    A desktop photo importer or GVfs holding the PTP session is the usual cause of a transfer
    dying part-way with "camera busy" or "could not reattach kernel driver".
    """
    holders = []
    for name in ('gvfsd-gphoto2', 'gvfs-gphoto2-volume-monitor', 'shotwell', 'gthumb',
                 'digikam', 'darktable', 'rapid-photo-downloader'):
        found = subprocess.run(['pgrep', '-x', name], capture_output=True, text=True)
        if found.returncode == 0:
            holders.append(f"{name} (pid {found.stdout.split()[0]})")
    if holders:
        raise SystemExit('these processes may hold the camera; close them first:\n  '
                         + '\n  '.join(holders))

def parse_listing(text: str) -> list[tuple[str, int, str]]:
    """(folder, per-folder index, filename) from `gphoto2 --list-files -R` output.

    The `#N` index is per folder, so it is meaningless without the folder heading that precedes
    it; passing a bare index to --get-file operates on the wrong directory entirely.
    """
    import re
    entries: list[tuple[str, int, str]] = []
    folder = '/'
    head = re.compile(r"in folder '([^']+)'")
    item = re.compile(r'^#(\d+)\s+(\S+)')
    for line in text.splitlines():
        m = head.search(line)
        if m:
            folder = m.group(1)
            continue
        m = item.match(line.strip())
        if m:
            entries.append((folder, int(m.group(1)), m.group(2)))
    return entries


def parse_size_bytes(show_info: str) -> int | None:
    """Size in BYTES from `gphoto2 --show-info`.

    --list-files prints sizes in KB, so comparing that against a byte count false-fails; only
    --show-info states the unit explicitly.
    """
    import re
    m = re.search(r'Size:\s*(\d+)\s*byte', show_info, re.IGNORECASE)
    return int(m.group(1)) if m else None


def probe_one(session: Path) -> int:
    """Download exactly one file and check it round-trips. Touches no ledger, no archive.

    Prefers a JPEG: small, quick, and its end-marker check is a real integrity test. Proves the
    PTP data path moves bytes intact, which a successful --summary does not.
    """
    preflight_camera()
    listing = subprocess.run(['gphoto2', '--list-files', '-R'], capture_output=True, text=True)
    if listing.returncode != 0:
        raise SystemExit(f'could not list files: {listing.stderr.strip().splitlines()[:2]}')
    entries = parse_listing(listing.stdout)
    if not entries:
        raise SystemExit('camera lists no files (card inserted? file-transfer USB mode?)')
    folders = sorted({f for f, _, _ in entries})
    print(f"camera lists {len(entries)} file(s) across {len(folders)} folder(s):")
    for f in folders:
        print(f"  {f}: {sum(1 for g, _, _ in entries if g == f)} file(s)")
    pick = next((e for e in entries if Path(e[2]).suffix.lower() in JPEG_EXT), entries[0])
    folder, index, name = pick
    print(f"probing {folder}/{name} (entry #{index})")

    info = subprocess.run(['gphoto2', '--folder', folder, '--show-info', str(index)],
                          capture_output=True, text=True)
    claimed = parse_size_bytes(info.stdout) if info.returncode == 0 else None

    rc = subprocess.run(['gphoto2', '--folder', folder, '--get-file', str(index),
                         '--filename', 'probe.%C'], cwd=session).returncode
    got = [p for p in session.iterdir() if p.is_file()]
    if rc != 0 or not got:
        raise SystemExit(f'single-file download failed (rc={rc})')
    f = got[0]
    size = f.stat().st_size
    ok, why = integrity_ok(f)
    print(f"downloaded {f.name}: {size} bytes\n  blake2b-256 {sha(f)}")
    print(f"  integrity: {'OK' if ok else 'FAILED - ' + why}"
          + (f" ({why})" if ok and why else ''))
    if claimed is None:
        print('  ! camera did not report a byte size, so size could not be cross-checked',
              file=sys.stderr)
        return 1 if not ok else 0
    if claimed != size:
        print(f"  ! camera reported {claimed} bytes but {size} arrived", file=sys.stderr)
        return 1
    print(f"  size matches the camera's own report ({claimed} bytes)")
    return 0 if ok else 1


def immich_upload(files: list[Path], album: str) -> bool:
    """Upload freshly archived files to Immich via its official CLI.

    Runs only after the files are archived and verified, so Immich is an additional
    destination rather than the only copy. Credentials come from the environment or
    ~/.config/nikon-offload/immich.{url,key}; use an API key scoped to asset upload
    and album management, not an admin key.

    Immich deduplicates by checksum, so re-running is harmless.
    """
    cfg = Path.home() / '.config/nikon-offload'
    url = os.environ.get('IMMICH_INSTANCE_URL') or (
        (cfg / 'immich.url').read_text().strip() if (cfg / 'immich.url').exists() else '')
    key = os.environ.get('IMMICH_API_KEY') or (
        (cfg / 'immich.key').read_text().strip() if (cfg / 'immich.key').exists() else '')
    if not url or not key:
        print('  ! Immich upload skipped: set IMMICH_INSTANCE_URL and IMMICH_API_KEY, or write '
              f'{cfg}/immich.url and immich.key', file=sys.stderr)
        return False
    if not shutil.which('npx'):
        print('  ! Immich upload skipped: npx not found (needs node)', file=sys.stderr)
        return False
    env = {**os.environ, 'IMMICH_INSTANCE_URL': url, 'IMMICH_API_KEY': key}
    proc = subprocess.run(['npx', '-y', '@immich/cli', 'upload', '--no-progress',
                           '--album-name', album, *map(str, files)], env=env)
    if proc.returncode != 0:
        print(f'  ! Immich upload exited {proc.returncode}', file=sys.stderr)
        return False
    return True


def stage_from_camera(session: Path) -> tuple[list[Path], bool]:
    """Full PTP pull into an empty session. Deliberately no --new/--skip-existing: camera-side
    state and filenames are not trustworthy identity, the hash ledger is.

    Returns (files, ok). `ok` is False whenever gphoto2 reported trouble or fewer files arrived
    than the camera listed, so a partial transfer can never look like a clean one.
    """
    preflight_camera()
    detect = subprocess.run(['gphoto2', '--auto-detect'], capture_output=True, text=True).stdout
    if len(detect.strip().splitlines()) <= 2:
        raise SystemExit('no camera detected by gphoto2 (connect USB-C, power on, PTP/MTP mode)')
    print(detect.strip())
    # Liveness probe is --summary, not --storage-info: the Z6III answers -6 "Unsupported
    # operation" to storage-info even when the PTP session is perfectly healthy, so using it
    # as a gate would block a working camera.
    live = subprocess.run(['gphoto2', '--summary'], capture_output=True, text=True)
    if live.returncode != 0:
        raise SystemExit(
            'the camera enumerates but refuses PTP commands. Check on the camera:\n'
            '  - Network menu > USB is set to MTP/PTP, not "USB-LAN" or "USB Streaming (UVC/UAC)"\n'
            '  - no Wi-Fi/FTP/SnapBridge connection is active (only one at a time)\n'
            '  - camera powered on and awake\n'
            f'gphoto2 said: {live.stderr.strip().splitlines()[:2]}')
    for line in live.stdout.splitlines():
        if line.startswith(('Model:', '  Serial Number:', '  Version:')):
            print(f"  {line.strip()}")
    # Require real storage before touching anything. "store_deadbeef" is libgphoto2's placeholder
    # for an unresolved storage ID, which is what an empty slot or a camera that has not exposed
    # its card looks like - indistinguishable from a card full of files unless we check.
    listing = subprocess.run(['gphoto2', '--list-files', '-R'], capture_output=True, text=True)
    files_seen = sum(1 for l in listing.stdout.splitlines() if l.startswith('#'))
    if 'store_deadbeef' in listing.stdout and files_seen == 0:
        raise SystemExit(
            'the camera responds but exposes no storage (placeholder "store_deadbeef", 0 files).\n'
            '  - is a memory card actually inserted?\n'
            '  - Network menu > USB set to the file-transfer mode (MTP/PTP)?\n'
            '  - camera awake and not in a streaming/network mode?\n'
            'Nothing was copied.')
    if files_seen == 0:
        raise SystemExit('camera lists 0 files; nothing to offload. Nothing was copied.')
    print(f"  camera reports {files_seen} file(s)")
    rc = subprocess.run(['gphoto2', '--recurse', '--get-all-files',
                         '--filename', '%f_%n.%C'], cwd=session).returncode
    if rc != 0:
        print(f"  ! gphoto2 exited {rc}: transfer incomplete, treat this run as unreliable",
              file=sys.stderr)
    got = [p for p in sorted(session.rglob('*')) if p.is_file() and p.suffix.lower() in WANTED]
    if len(got) < files_seen:
        print(f"  ! camera listed {files_seen} file(s) but {len(got)} arrived", file=sys.stderr)
        return got, False
    return got, rc == 0


def replicate(archive: Path, remote: str, day: str) -> bool:
    """rsync the day's directory, then prove equality with a checksum-based itemised dry run.

    The probe's exit status is authoritative: an ssh or path failure produces empty output, so
    treating "no differences printed" as success would report a verified copy that does not exist.
    """
    src = f'{archive}/{day[:4]}/{day}/'
    dst = f'{remote}/{day[:4]}/{day}/'
    host, _, rpath = remote.partition(':')
    if subprocess.run(['ssh', host, 'mkdir', '-p',
                       f'{rpath}/{day[:4]}/{day}']).returncode != 0:
        print(f'  ! could not create {dst}', file=sys.stderr)
        return False
    if subprocess.run(['rsync', '-a', '--partial', src, dst]).returncode != 0:
        return False
    probe = subprocess.run(['rsync', '-aic', '--dry-run', src, dst],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        print(f'  ! verification probe failed ({probe.returncode}): '
              f'{probe.stderr.strip().splitlines()[:2]}', file=sys.stderr)
        return False
    diffs = [l for l in probe.stdout.splitlines() if l and not l.startswith('.')]
    if diffs:
        print('  ! remote differs after transfer:', *diffs[:5], sep='\n    ', file=sys.stderr)
        return False
    return True


def seed_ledger(db: sqlite3.Connection, dirs: list[Path], remote_note: str) -> int:
    """Record existing local originals as already-held, without moving or copying anything.

    Without this, the first camera run treats a card full of previously-offloaded files as new and
    re-downloads tens of GB. Hashing is done locally, so the entries are content-identified just
    like imported ones.
    """
    added = skipped = 0
    for d in dirs:
        for p in sorted(d.rglob('*')):
            if not p.is_file() or p.suffix.lower() not in WANTED:
                continue
            digest = sha(p)
            if db.execute('SELECT 1 FROM asset WHERE content_hash=?', (digest,)).fetchone():
                skipped += 1
                continue
            db.execute("""INSERT INTO asset (content_hash, camera_folder, camera_filename, size,
                          captured, serial, local_path, verified_local, remote_path,
                          verified_remote, imported_at)
                          VALUES (?,?,?,?,?,?,?,1,?,?,?)""",
                       (digest, 'seeded', p.name, p.stat().st_size, '', '', str(p),
                        remote_note, 1 if remote_note else 0,
                        dt.datetime.now().isoformat(timespec='seconds')))
            added += 1
            if added % 250 == 0:
                db.commit()
                print(f"  seeded {added}...", flush=True)
    db.commit()
    print(f"seeded {added} file(s); {skipped} already known")
    return 0


def ranges(nums: list[int]) -> list[str]:
    """Contiguous runs as gphoto2 range strings. Comma lists fail with -108 on this body."""
    out, run = [], []
    for n in sorted(nums):
        if run and n == run[-1] + 1:
            run.append(n)
        else:
            if run:
                out.append(f'{run[0]}-{run[-1]}' if len(run) > 1 else str(run[0]))
            run = [n]
    if run:
        out.append(f'{run[0]}-{run[-1]}' if len(run) > 1 else str(run[0]))
    return out


def stage_only_new(db: sqlite3.Connection, session: Path) -> tuple[list[Path], bool]:
    """Download only card entries the ledger has never seen, matched on name + KB-rounded size.

    Weaker identity than hashing, and knowingly so: hashing requires the bytes, which is the thing
    being avoided. A same-name, same-KB file with different content would be skipped. Use the
    default full pull when that risk is unacceptable; this mode exists so a card that was already
    offloaded does not cost another 50 GB of transfer.
    """
    preflight_camera()
    listing = subprocess.run(['gphoto2', '--list-files', '-R'], capture_output=True, text=True)
    if listing.returncode != 0:
        raise SystemExit(f'could not list files: {listing.stderr.strip().splitlines()[:2]}')
    import re
    known = {(n, s) for n, s in db.execute('SELECT camera_filename, size FROM asset')}
    known_kb = {(n, round(s / 1024)) for n, s in known}
    folder, todo, total = '/', {}, 0
    for line in listing.stdout.splitlines():
        m = re.search(r"in folder '([^']+)'", line)
        if m:
            folder = m.group(1)
            continue
        m = re.match(r'^#(\d+)\s+(\S+)\s+(.*)$', line.strip())
        if not m:
            continue
        idx, name, rest = int(m.group(1)), m.group(2), m.group(3)
        if Path(name).suffix.lower() not in WANTED:
            continue
        total += 1
        kb = int(re.search(r'(\d+)\s*KB', rest).group(1)) if re.search(r'(\d+)\s*KB', rest) else -1
        if any((name, kb + d) in known_kb for d in (-1, 0, 1)):
            continue
        todo.setdefault(folder, []).append(idx)
    n_new = sum(len(v) for v in todo.values())
    print(f"camera holds {total} file(s); {total - n_new} already in the ledger; "
          f"{n_new} to fetch")
    if not todo:
        return [], True          # nothing new on the card: a correct outcome, not a failure
    ok = True
    for fol, idxs in todo.items():
        for rng in ranges(idxs):
            rc = subprocess.run(['gphoto2', '--folder', fol, '--get-file', rng,
                                 '--filename', '%f.%C'], cwd=session).returncode
            if rc != 0:
                print(f'  ! fetch of {fol} [{rng}] exited {rc}', file=sys.stderr)
                ok = False
    got = [p for p in sorted(session.rglob('*')) if p.is_file() and p.suffix.lower() in WANTED]
    if len(got) != n_new:
        print(f'  ! expected {n_new} file(s), {len(got)} arrived', file=sys.stderr)
        ok = False
    return got, ok


def main() -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--from-camera', action='store_true', help='pull over USB-C PTP')
    src.add_argument('--from-dir', type=Path, help='a mounted card or an existing session')
    src.add_argument('--probe-one', action='store_true',
                     help='download ONE file and verify it round-trips; no archive, no ledger')
    src.add_argument('--seed-from', type=Path, nargs='+', metavar='DIR',
                     help='record existing local originals as already-held (hashes, moves nothing)')
    ap.add_argument('--only-new', action='store_true',
                    help='with --from-camera, fetch only entries the ledger lacks (name+KB match, '
                         'weaker than hashing - see stage_only_new docstring)')
    ap.add_argument('--seed-remote-note', default='',
                    help='remote path to record for seeded files, if they are already replicated')
    ap.add_argument('--archive', type=Path, default=Path.home() / 'nikon/archive')
    ap.add_argument('--ledger', type=Path, default=Path.home() / 'nikon/offload.sqlite')
    ap.add_argument('--remote', default=os.environ.get('NIKON_REMOTE', ''),
                    help='host:path for the verified second copy, or $NIKON_REMOTE; '
                         'empty means only one copy exists and the run says so')
    ap.add_argument('--immich', nargs='?', const='auto', metavar='ALBUM',
                    help='also upload newly archived files to Immich; ALBUM defaults to '
                         '"Camera YYYY-MM-DD" per capture day')
    ap.add_argument('--keep-session', action='store_true', help='do not delete the staging dir')
    a = ap.parse_args()

    db = db_open(a.ledger)
    if a.probe_one:
        tmp = Path(tempfile.mkdtemp(prefix='probe-'))
        try:
            return probe_one(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if a.seed_from:
        return seed_ledger(db, a.seed_from, a.seed_remote_note)

    session = Path(tempfile.mkdtemp(prefix='offload-', dir=a.archive.parent
                                    if a.archive.parent.exists() else None))
    print(f"staging in {session}")
    keep_for_inspection = False
    try:
        if a.from_camera and a.only_new:
            staged, stage_ok = stage_only_new(db, session)
        elif a.from_camera:
            staged, stage_ok = stage_from_camera(session)
        else:
            staged, stage_ok = stage_from_dir(a.from_dir, session), True
        print(f"staged {len(staged)} candidate files")
        meta = exif(staged)

        new, dupes, failed, days = 0, 0, 0, set()
        unreplicated_dupes = 0
        # Every digest accounted for by this run, new or already-archived. Immich upload works
        # from this set against ledger state, so a file whose upload failed on an earlier run is
        # retried rather than skipped forever as a duplicate.
        seen: list[str] = []
        for path in staged:
            ok, why = integrity_ok(path)
            if not ok:
                print(f"  ! rejected {path.name}: {why}", file=sys.stderr)
                failed += 1
                keep_for_inspection = True
                continue
            digest = sha(path)
            row = db.execute("""SELECT local_path, verified_local, verified_remote
                                FROM asset WHERE content_hash=?""", (digest,)).fetchone()
            if row:
                # A ledger hit is only trustworthy if the archived file is still on disk with
                # matching content. A deleted or altered archive copy, or one whose second copy
                # never verified, must not be silently believed.
                prior = Path(row[0])
                if prior.exists() and prior.stat().st_size == path.stat().st_size \
                        and sha(prior) == digest:
                    dupes += 1
                    seen.append(digest)
                    if not row[2]:
                        unreplicated_dupes += 1
                        days.add(prior.parent.name)
                    continue
                print(f"  ~ ledger row for {path.name} is stale ({prior}); re-importing",
                      file=sys.stderr)
                db.execute('DELETE FROM asset WHERE content_hash=?', (digest,))
                db.commit()
            m = meta.get(str(path), {})
            day, name = canonical(path, digest, m.get('captured', ''), m.get('serial', ''))
            dest_dir = a.archive / day[:4] / day
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / name
            shutil.move(str(path), dest)
            if sha(dest) != digest:                     # re-read after the move
                print(f"  ! verify failed at destination: {dest}", file=sys.stderr)
                failed += 1
                continue
            db.execute("""INSERT INTO asset (content_hash, camera_folder, camera_filename, size,
                          captured, serial, local_path, verified_local, imported_at)
                          VALUES (?,?,?,?,?,?,?,1,?)""",
                       (digest, str(path.parent.relative_to(session)), path.name,
                        dest.stat().st_size, m.get('captured', ''), m.get('serial', ''),
                        str(dest), dt.datetime.now().isoformat(timespec='seconds')))
            db.commit()
            new += 1
            seen.append(digest)
            days.add(day)

        print(f"new {new}   duplicates {dupes}   failed {failed}")

        replicated = True
        if a.remote and days:
            for day in sorted(days):
                ok = replicate(a.archive, a.remote, day)
                print(f"  replicate {day}: {'verified' if ok else 'FAILED'}")
                replicated &= ok
                if ok:
                    db.execute("""UPDATE asset SET verified_remote=1,
                                  remote_path=? WHERE local_path LIKE ?""",
                               (f'{a.remote}/{day[:4]}/{day}', f'{a.archive}/{day[:4]}/{day}/%'))
                    db.commit()
        elif not a.remote:
            replicated = False
            print('  replication skipped (--remote empty): only one copy exists')

        # Immich last, and only for files already archived and verified, so it is an extra
        # destination rather than the sole copy. Which files upload is decided by ledger state
        # over this run's whole inventory, not by what happened to be new this run: an upload
        # that failed earlier is retried on the next offload of the same card. Grouped by
        # capture day so albums stay useful.
        immich_ok = True
        if a.immich:
            todo: dict[str, list[Path]] = {}
            for digest in seen:
                row = db.execute('SELECT local_path FROM asset WHERE content_hash=? '
                                 'AND immich_uploaded=0', (digest,)).fetchone()
                if row and Path(row[0]).exists():
                    todo.setdefault(Path(row[0]).parent.name, []).append(Path(row[0]))
            if not todo:
                print('  immich: already uploaded, nothing to do')
            for day, paths in sorted(todo.items()):
                album = f'Camera {day}' if a.immich == 'auto' else a.immich
                print(f"  immich: uploading {len(paths)} file(s) to '{album}'")
                if immich_upload(paths, album):
                    db.executemany('UPDATE asset SET immich_uploaded=1 WHERE local_path=?',
                                   [(str(p),) for p in paths])
                    db.commit()
                else:
                    immich_ok = False
                    print(f"  ! immich: {len(paths)} file(s) from {day} not uploaded; rerun to "
                          "retry", file=sys.stderr)

        # Per-run reconciliation over THIS card's inventory, every file type, not a global RAW
        # count. Deliberately no "safe to format" verdict: authorising a destructive action needs
        # a hardware-backed inventory comparison against the camera's own file list, which has
        # not been exercised yet.
        accounted = dupes + new
        print(f"\ninventory for this run")
        print(f"  staged from source      : {len(staged)}")
        print(f"  accounted (new+dupes)   : {accounted}")
        print(f"  rejected as damaged     : {failed}")
        by_ext: dict[str, list[int]] = {}
        for ext, vl, vr in db.execute("""SELECT LOWER(SUBSTR(local_path, -4)),
                                         SUM(verified_local), SUM(verified_remote)
                                         FROM asset GROUP BY 1"""):
            by_ext[ext] = [vl or 0, vr or 0]
        for ext, (vl, vr) in sorted(by_ext.items()):
            print(f"  archive {ext:>5}: {vl} local-verified, {vr} remote-verified")
        if a.immich:
            pend = db.execute('SELECT COUNT(*) FROM asset WHERE immich_uploaded=0 AND '
                              'content_hash IN (%s)' % ','.join('?' * len(seen)),
                              seen).fetchone()[0] if seen else 0
            print(f"  immich: {len(seen) - pend}/{len(seen)} of this run uploaded")
        if unreplicated_dupes:
            print(f"  {unreplicated_dupes} previously-imported file(s) still lacked a second copy")
        problems = []
        if not stage_ok:
            problems.append("transfer from the camera did not complete cleanly")
        if not staged and not (a.from_camera and a.only_new):
            problems.append("nothing was staged, so the source was never read")
        if failed:
            problems.append(f"{failed} file(s) rejected as damaged/truncated")
        if not replicated:
            problems.append("replication did not verify")
        if not immich_ok:
            problems.append("Immich upload failed (rerun to retry; the archive is unaffected)")
        if problems:
            print("\nPROBLEMS: " + "; ".join(problems))
        print("\nThis tool does not authorise erasing anything. Keep the card until you have "
              "confirmed the archive yourself.")
        return 0 if (stage_ok and failed == 0 and replicated and immich_ok) else 1
    finally:
        # Anything still staged is either an already-archived duplicate (safe to drop: its hash
        # is in the ledger and verified) or a rejected file worth inspecting. Keep the directory
        # only in the second case, so routine reruns do not litter temp dirs.
        remaining = [p for p in session.rglob('*') if p.is_file()]
        if a.keep_session or (remaining and keep_for_inspection):
            print(f"staging kept for inspection at {session} ({len(remaining)} files)")
        else:
            shutil.rmtree(session, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
