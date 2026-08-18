#!/usr/bin/env bash
# Replicate the camera originals on fw to d, then verify by CHECKSUM, not size+mtime.
#
# Context: the card was reconciled against these files only by filename and the camera listing's
# KB-rounded size. That is not content verification, and until these exist in a second location
# the card is still their only other copy. Derived data (he-star-dng) is deliberately excluded:
# it is reproducible from the NEFs by the documented pipeline.
set -uo pipefail

SRC=${SRC:-$HOME/nikon}
DST_HOST=${DST_HOST:?set DST_HOST to the backup host, e.g. DST_HOST=backup.example}
DST=${DST:-/srv/nikon-originals}
rc=0

ssh "$DST_HOST" "mkdir -p $DST/he-star $DST/lossless $DST/root" || exit 1

sync_and_verify() {
  local label=$1 src=$2 dst=$3; shift 3
  echo "=== $label ==="
  if ! rsync -a --partial --info=stats2 "$@" "$src" "$DST_HOST:$dst"; then
    echo "  ! transfer FAILED: $label" >&2
    rc=1
    return
  fi
  # -c forces a checksum comparison of every file on both sides; the exit status is honoured as
  # well as the output, so a broken probe cannot read as success.
  local out
  if ! out=$(rsync -aic --dry-run "$@" "$src" "$DST_HOST:$dst" 2>&1); then
    echo "  ! verification probe FAILED: $label" >&2
    rc=1
    return
  fi
  local diffs
  diffs=$(printf '%s\n' "$out" | grep -v '^$' | grep -vE '^\.' | grep -vE '^(sending|sent|total)' || true)
  if [[ -n "$diffs" ]]; then
    echo "  ! checksum mismatch after transfer: $label" >&2
    printf '    %s\n' $(printf '%s\n' "$diffs" | head -5) >&2
    rc=1
  else
    echo "  checksum-verified: $label"
  fi
}

sync_and_verify "he-star (HE* NEF + sidecars)" "$SRC/he-star/" "$DST/he-star/"
sync_and_verify "lossless (NEF + sidecars)"    "$SRC/lossless/" "$DST/lossless/"
sync_and_verify "camera JPEG/MOV"              "$SRC/" "$DST/root/" \
  --include='*.JPG' --include='*.MOV' --exclude='*'

echo
if [[ $rc -eq 0 ]]; then
  echo "all originals now exist in two locations and match by checksum"
else
  echo "FAILURES above: originals are NOT fully replicated" >&2
fi
exit $rc
