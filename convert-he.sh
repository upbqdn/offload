#!/usr/bin/env bash
# Convert Nikon HE/HE* NEF -> lossless-compressed DNG via Adobe DNG Converter under wine.
#
# Failure handling:
#   - each wine invocation writes into a per-chunk staging dir; only DNGs that pass
#     validate_dng.py are moved into OUT, so OUT never holds a truncated file
#   - wine's exit status is preserved (the log filter runs afterwards, not in a pipe)
#   - per-file failures land in OUT/.failed, converter output in OUT/.log, exit is nonzero
# Resumable: a NEF is skipped only if OUT holds a DNG that passes validation.
set -uo pipefail

SRC=${SRC:-$HOME/nikon/he-star}
OUT=${OUT:-$HOME/nikon/he-star-dng}
JOBS=${JOBS:-4}          # concurrent wine processes (converter is itself multithreaded)
CHUNK=${CHUNK:-24}       # NEFs per wine invocation (amortizes ~4 s startup)

export WINEPREFIX=${WINEPREFIX:-$HOME/.wine-dng}
export WINEDEBUG=-all
export EXE='C:\Program Files\Adobe\Adobe DNG Converter\Adobe DNG Converter.exe'
export VALIDATE=${VALIDATE:-"$(dirname "$(readlink -f "$0")")/validate_dng.py"}
export OUT LOG=$OUT/.log FAILED=$OUT/.failed STAGE=$OUT/.stage

mkdir -p "$OUT" "$STAGE"
: >"$FAILED"

convert_chunk() {
  local stage rc=0 f b
  stage=$(mktemp -d "$STAGE/chunk.XXXXXX") || return 1

  local -a args=()
  for f in "$@"; do args+=("$(printf 'Z:%s' "${f//\//\\}")"); done

  wine "$EXE" -c -d "$(printf 'Z:%s' "${stage//\//\\}")" "${args[@]}" >"$stage/.wine.log" 2>&1
  rc=$?
  grep -viE 'GPU vendor|sensei|ModelZoo|model #|model not present|refinement model|focal matte' \
    "$stage/.wine.log" >>"$LOG" 2>/dev/null

  for f in "$@"; do
    b=$(basename "$f" .NEF)
    if python3 "$VALIDATE" "$stage/$b.dng" >>"$LOG" 2>&1; then
      mv -f "$stage/$b.dng" "$OUT/$b.dng"
    else
      printf '%s\n' "$f" >>"$FAILED"
      rc=1
    fi
  done

  rm -rf "$stage"
  if [[ $rc -ne 0 ]]; then
    echo "CHUNK FAILED (rc=$rc): $*" >>"$LOG"
    return 1
  fi
  return 0
}
export -f convert_chunk

# Resume scan: one validator process over every existing DNG.
declare -A ok=()
if compgen -G "$OUT/*.dng" >/dev/null; then
  while read -r status path _; do
    [[ $status == OK ]] && ok[$(basename "$path" .dng)]=1
  done < <(python3 "$VALIDATE" "$OUT"/*.dng)
fi

mapfile -t todo < <(
  for f in "$SRC"/*.NEF; do
    b=$(basename "$f" .NEF)
    [[ -v ok[$b] ]] || printf '%s\n' "$f"
  done
)
total=${#todo[@]}
echo "pending: $total (already valid: ${#ok[@]})  ->  $OUT"
[[ $total -eq 0 ]] && exit 0

start=$SECONDS
printf '%s\0' "${todo[@]}" |
  xargs -0 -n "$CHUNK" -P "$JOBS" bash -c 'convert_chunk "$@"' _
xargs_rc=$?

nfail=$(wc -l <"$FAILED")
ndone=$(ls -1 "$OUT"/*.dng 2>/dev/null | wc -l)
echo "elapsed: $((SECONDS-start))s  valid dng in OUT: $ndone  failed: $nfail  (xargs rc=$xargs_rc)"
if [[ $nfail -ne 0 || $xargs_rc -ne 0 ]]; then
  echo "FAILURES: see $FAILED and $LOG" >&2
  exit 1
fi
rmdir "$STAGE" 2>/dev/null
exit 0
