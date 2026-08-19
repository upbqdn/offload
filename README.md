# offload

Offload, verification and eclipse-processing tools for a Nikon Z6III and a ZWO Seestar.

Two unrelated jobs live here: getting files off a camera or telescope safely, and turning
bracketed totality frames into something viewable.

## Offload

`offload.py` imports from the camera over USB PTP or from a mounted card, identifying every
file by **content hash**, never by name. Nikon's `DSC_####` counter wraps at 9999 and can be reset
in-camera, and the PTP "new file" flag does not survive card, camera and software changes, so
names and camera-side state are not identity.

```
--probe-one              download one file, hash it, cross-check its size. Proves the data path.
--from-camera            full pull; hash → integrity → archive → replicate → verify
--from-dir DIR           same pipeline from a mounted card or directory
--seed-from DIR...       record files already held locally, so they are not re-fetched
--from-camera --only-new fetch only entries the ledger lacks (see the caveat below)
--immich [ALBUM]         additionally upload archived files to Immich
--to-immich DIR          copy DIR to Immich and prove it arrived; writes no archive
```

## To Immich, with no archive

`--to-immich DIR` is for when Immich is the destination of record rather than a third copy. It
copies to a temporary directory, re-hashes to confirm the copy, runs the integrity gate, uploads,
then asks Immich by SHA-1 whether it holds each file, and deletes the temporary copy only for
those it confirms. No archive tree, and no ledger: Immich answers "already have this?" itself,
so the mode keeps no state and re-running costs one checksum comparison per file.

A checksum matching a **trashed** asset counts as absent. Immich blocks re-upload of a trashed
duplicate, so treating it as present would lose the file when the trash empties.

Staging survives for anything unconfirmed: integrity failures, upload failures, and formats
Immich cannot render (FITS). Those are reported with their paths rather than discarded, since
where they belong is not this tool's decision. The source is only ever read.

A Seestar mounts as a plain exFAT disk, so it needs no protocol of its own:

```
udisksctl mount -b /dev/sda --options ro
./offload.py --to-immich /run/media/$USER/Seestar/MyWorks --album "Seestar 2026-08-12"
```

With `--immich`, upload happens last and only for files already archived and verified, so Immich
is a third destination rather than the only copy. Files group into `Camera YYYY-MM-DD` albums by
capture day unless `ALBUM` names one. Credentials come from `IMMICH_INSTANCE_URL` and
`IMMICH_API_KEY`, or from `~/.config/offload/immich.{url,key}`; an API key scoped to asset
upload and album management is enough, and preferable to an admin key.

Which files upload is decided by ledger state over the run's whole inventory, not by what was new
in that run, so an upload that failed earlier is retried on the next offload of the same card
rather than being skipped forever as a duplicate. A failed upload makes the run exit nonzero and
leaves the archive untouched. Immich deduplicates by checksum, so re-running uploads nothing
twice.

Paths and the backup target come from the environment, so nothing host-specific is baked in:
`NIKON_REMOTE` (or `--remote`) is the `host:path` for the verified second copy, `--archive` and
`--ledger` default under `~/nikon`, and `replicate-originals.sh` requires `DST_HOST`. With no
remote configured, the run states that only one copy exists.

A SQLite ledger records hash, camera folder/filename, size, capture time, body serial, and the
local and remote paths with their verification flags. Rows are revalidated on use: a hash hit
whose archived copy has vanished or changed is dropped and the file re-imported.

Archived names are capture time, body serial and a hash prefix, so a counter reset cannot collide:

```
20260818-185506_1234567_abbdcb2c.NEF
```

**It does not tell you a card is safe to erase, by design.** Authorising a destructive action
would need reconciliation against the camera's own per-slot inventory, which is not implemented.
Inspect the archive yourself. The card is never written to or erased.

### Integrity

Hashing cannot detect truncation — a short file simply has a different, valid-looking hash. So
every RAW must positively declare at least one non-empty strip/tile lying wholly inside the file
(NEF is TIFF-based), and JPEGs must carry their end marker. Real NEFs contain SubIFDs with
zero-length placeholder strips; those are skipped rather than treated as damage. Video is copied
and hashed but has no structural check.

### `--only-new` caveat

It decides what to skip from the camera's listing, matching filename and KB-rounded size, because
hashing needs the bytes it is trying not to transfer. A same-name, same-size file with different
content would be skipped, and the skip happens before stale-ledger revalidation. Use it only for
legacy cards that were never cleared; prefer the full pull otherwise.

### Replication

`replicate-originals.sh` mirrors originals to a second host and verifies with `rsync -aic`,
honouring the probe's exit status as well as its output — an ssh or path failure prints nothing,
so treating silence as success would report a copy that does not exist. Two disk copies satisfy
redundancy but replicate any pre-existing corruption identically.

## HE\* conversion

`convert-he.sh` converts Nikon **High Efficiency** NEF to DNG through Adobe's converter under
wine, because no open-source decoder handles HE/HE\* (intoPIX TicoRAW). Chunked and parallel,
resumable, and it stages each chunk so a killed run cannot leave a truncated DNG that a later run
counts as done. `validate_dng.py` is the structural gate: DNGVersion, a CFA/LinearRaw SubIFD, and
every tile inside the file.

Shoot lossless-compressed rather than HE\* and none of this is needed.

## Eclipse processing

Pipeline for bracketed totality frames:

```
eclipse_grade.py     fit the lunar disk, rank frames by limb sharpness within an exposure level
eclipse_merge.py     register on the limb, HDR-merge in linear radiance
eclipse_render.py    sky fit, grey balance, radial flattening, tone mapping
eclipse_single.py    straight single-frame development, no merge or flattening
eclipse_sequence.py  partial-phase montage from manually curated frames
```

Notes worth keeping:

- Sharpness numbers compare only **within** one exposure level. The metric normalises by each
  frame's own profile amplitude, and long exposures clip the inner corona, so cross-level
  comparisons are meaningless.
- Near second and third contact the emerging chromosphere dominates the limb, wrecking both the
  circle fit and the width metric. Gate on fit residual before trusting either.
- Registration fits the limb rather than using phase correlation: a near-circularly-symmetric
  annulus gives phase correlation an ambiguous peak.
- Flatten with one shared luminance profile. Dividing each channel by its own profile neutralises
  the mean radial colour and inverts hue into a coloured rim.
- Automated cross-exposure sharpness ranking selects underexposed noise. Curate montage panels by
  hand; `eclipse_sequence.py` requires exactly one file per panel for that reason.

## 2027 planning

`eclipse2027_plan.py` prints a shooting timeline from the site's contact times — exposure sets,
filter cues, interval-timer programme, frame and card budget, and hands-on versus hands-free
seconds. It generates a plan; it does not control the camera.

## Requirements

`gphoto2`, `rsync`, `exiftool`, `python3` with `numpy`, `scipy`, `Pillow`, `rawpy`, `tifffile`.
HE\* conversion additionally needs wine and Adobe DNG Converter, plus `DirectML.dll` from the
`Microsoft.AI.DirectML` redistributable next to the converter binary — without it the converter
aborts with `Required C2PA Library could not be loaded`.
