#!/usr/bin/env bash
#
# pack-selftest.sh — exercise pack.sh's redaction and verification against a fixture.
#
#   ./context/pack-selftest.sh [path-to-pack.sh]
#
# Everything happens in a temp directory: a throwaway repo layout, a throwaway HOME (so no real
# transcript is read), and a throwaway archive. It never reads or writes context/archives/, and
# it never unpacks a real archive.
#
# The fixture is the one the archive's failure modes were found with: a multi-line PEM key, a
# symlink pointing at a secret that lives outside the stage, and two extras sharing a basename.
#
# Written for macOS's bash 3.2.
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACK="${1:-$HERE/pack.sh}"
if [ ! -f "$PACK" ]; then
  echo "no such script: $PACK" >&2
  exit 2
fi
PACK="$(cd "$(dirname "$PACK")" && pwd)/$(basename "$PACK")"

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); echo "  ok   — $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL — $1"; }
check() { if [ "$1" = "yes" ]; then ok "$2"; else bad "$2"; fi; }

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/packtest.XXXXXX")"
# Archives are chmod 444 + chflags uchg, so plain rm -rf cannot clean up after this.
cleanup() {
  command -v chflags >/dev/null 2>&1 && chflags -R nouchg "$ROOT" 2>/dev/null
  chmod -R u+w "$ROOT" 2>/dev/null
  rm -rf "$ROOT"
}
trap cleanup EXIT

# A throwaway repo layout, so $HERE/archives inside pack.sh points into the sandbox.
mkdir -p "$ROOT/ws/repo/context" "$ROOT/home"
cp "$PACK" "$ROOT/ws/repo/context/pack.sh"
chmod +x "$ROOT/ws/repo/context/pack.sh"

# ---- fixture ---------------------------------------------------------------
# A realistic private key: header, body, END line. Fake, but the shape is what matters.
KEYBODY="MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDNotARealKey00"
mkdir -p "$ROOT/extras/alpha" "$ROOT/extras/one" "$ROOT/extras/two" "$ROOT/outside"

{
  printf 'ssh key for the bastion:\n'
  printf -- '-----BEGIN RSA PRIVATE KEY-----\n'
  printf '%s\n%s\n%s\n' "$KEYBODY" "$KEYBODY" "$KEYBODY"
  printf -- '-----END RSA PRIVATE KEY-----\n'
  # The same key as it actually appears in a JSONL transcript: one line, \n escaped.
  printf '{"text":"-----BEGIN OPENSSH PRIVATE KEY-----\\n%s\\n-----END OPENSSH PRIVATE KEY-----"}\n' "$KEYBODY"
  printf 'db: postgresql+asyncpg://svc:hunter2hunter2@db.internal:5432/app\n'
} > "$ROOT/extras/alpha/notes.md"

# Two extras from different directories sharing a basename. Distinct content, so a silent
# overwrite inside a soon-to-be-immutable archive is visible.
echo "ONE-SIDE-CONTENT" > "$ROOT/extras/one/shared.txt"
echo "TWO-SIDE-CONTENT" > "$ROOT/extras/two/shared.txt"
EXTRAS=("$ROOT/extras/alpha" "$ROOT/extras/one/shared.txt" "$ROOT/extras/two/shared.txt")

# A secret that lives OUTSIDE the stage, reachable only through a symlink, and shaped so that
# the redactor has no pattern for it — bare key material with no PEM armour. That is the leak
# the header-only redaction used to produce, and it is exactly what a verifier built from the
# redactor's own patterns cannot see. Twelve distinct blobs, so the leak report has to truncate
# and the `head -10` SIGPIPE path is exercised.
: > "$ROOT/outside/secret.txt"
i=0
while [ "$i" -lt 12 ]; do
  echo "${KEYBODY}${i}${KEYBODY}" >> "$ROOT/outside/secret.txt"
  i=$((i + 1))
done

run_pack() {
  ( cd "$ROOT/ws/repo" && HOME="$ROOT/home" ./context/pack.sh "$@" ) \
    > "$ROOT/out.txt" 2> "$ROOT/err.txt"
  echo $?
}

# ---- 1. the leak path ------------------------------------------------------
echo "leak path (symlinked out-of-stage secret the redactor has no pattern for):"
ln -sf "$ROOT/outside/secret.txt" "$ROOT/extras/alpha/linked-secret.txt"
STATUS="$(run_pack leaky "${EXTRAS[@]}")"
BOTH="$(cat "$ROOT/out.txt" "$ROOT/err.txt")"

check "$([ "$STATUS" = "1" ] && echo yes || echo no)" \
  "exits 1 (got $STATUS)"
check "$(echo "$BOTH" | grep -q 'REFUSING to write the archive' && echo yes || echo no)" \
  "refuses to write the archive"
check "$(echo "$BOTH" | grep -qi 'MII\|survived redaction' && echo yes || echo no)" \
  "reports the symlinked secret the redactor missed"
check "$(echo "$BOTH" | grep -q 're-run\|then re-run' && echo yes || echo no)" \
  "prints the remediation guidance before exiting (SIGPIPE did not cut it short)"
check "$(ls "$ROOT/ws/repo/context/archives"/*.zip >/dev/null 2>&1 && echo no || echo yes)" \
  "wrote no archive"

# ---- 2. the clean path -----------------------------------------------------
echo "clean path:"
rm -f "$ROOT/extras/alpha/linked-secret.txt"
STATUS="$(run_pack clean "${EXTRAS[@]}")"
check "$([ "$STATUS" = "0" ] && echo yes || echo no)" \
  "exits 0 (got $STATUS)"

ZIP="$(ls "$ROOT/ws/repo/context/archives"/*-clean.zip 2>/dev/null | sed -n 1p)"
if [ -z "$ZIP" ]; then
  bad "wrote an archive"
else
  ok "wrote an archive"
  mkdir -p "$ROOT/unpacked"
  unzip -qq "$ZIP" -d "$ROOT/unpacked"
  DUMP="$(find "$ROOT/unpacked" -type f -exec cat {} +)"

  check "$(echo "$DUMP" | grep -q "$KEYBODY" && echo no || echo yes)" \
    "the key BODY is gone (header-only redaction left it behind)"
  check "$(echo "$DUMP" | grep -q -- '-----END RSA PRIVATE KEY-----' && echo no || echo yes)" \
    "the END line is gone"
  check "$(echo "$DUMP" | grep -q -- '-----BEGIN OPENSSH PRIVATE KEY-----' && echo no || echo yes)" \
    "the \\n-escaped key inside a JSONL line is gone"
  check "$(echo "$DUMP" | grep -q 'hunter2hunter2' && echo no || echo yes)" \
    "the DSN password is gone"
  check "$(echo "$DUMP" | grep -q 'ONE-SIDE-CONTENT' && echo yes || echo no)" \
    "the first same-basename extra survived"
  check "$(echo "$DUMP" | grep -q 'TWO-SIDE-CONTENT' && echo yes || echo no)" \
    "the second same-basename extra survived (not silently overwritten)"
fi

echo ""
echo "$PASS passed, $FAIL failed  ($PACK)"
[ "$FAIL" -eq 0 ]
