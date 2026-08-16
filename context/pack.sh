#!/usr/bin/env bash
#
# pack.sh — bundle this session's transcripts into context/archives/ as a scrubbed zip.
#
#   ./context/pack.sh <slug> [extra-file-or-dir ...]
#
# Stages the transcripts, redacts credentials, verifies the redaction worked, and only then
# writes the zip. If verification fails the zip is not written — a partially-scrubbed archive
# is worse than none, because it looks safe.
#
# Written for macOS's bash 3.2. No mapfile, no associative arrays.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
WORKSPACE="$(cd "$REPO/.." && pwd)"

SLUG="${1:-}"
if [ -z "$SLUG" ]; then
  echo "usage: ./context/pack.sh <slug> [extra-file-or-dir ...]" >&2
  echo "  e.g. ./context/pack.sh stage-2-fixtures" >&2
  exit 2
fi
shift || true

# Credential shapes, as Perl regexes. Used for BOTH redaction and the post-redaction check,
# through the same engine, so the two halves can never drift apart. Add a shape here and both
# learn it at once.
PATTERNS=(
  'sk-ant-[A-Za-z0-9_-]{16,}'
  'sa_[A-Za-z0-9_-]{16,}'
  'gh[pousr]_[A-Za-z0-9]{20,}'
  'xox[baprse]-[A-Za-z0-9-]{10,}'
  'AKIA[0-9A-Z]{16}'
  # Any scheme, not an enumerated list. The first version named postgres/postgresql/redis/amqp
  # and consequently missed `postgresql+asyncpg://` — which is the scheme this project actually
  # uses — and `https://user:token@`. Excluding "/" from the password half keeps
  # `https://host:8080/p@th` from being mistaken for credentials.
  '(?<=://)[^:/@[:space:]"]+:[^@/[:space:]"]+(?=@)'
  '(?<=Bearer )[A-Za-z0-9._-]{20,}'
  '-----BEGIN[A-Z ]*PRIVATE KEY-----'
)

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/ctxpack.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

# ---- collect ---------------------------------------------------------------
# Claude Code stores transcripts under ~/.claude/projects/<workspace-path, slashes as dashes>/
PROJDIR="$HOME/.claude/projects/$(echo "$WORKSPACE" | tr '/' '-')"
mkdir -p "$STAGE/transcripts"

# Recursive, not maxdepth 1: subagent transcripts live in <session-id>/subagents/ and are the
# bulk of the material — the audit fan-out's actual reasoning is all down there.
FOUND=0
if [ -d "$PROJDIR" ]; then
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    mkdir -p "$STAGE/transcripts/$(dirname "$rel")"
    cp "$PROJDIR/$rel" "$STAGE/transcripts/$rel"
    FOUND=$((FOUND + 1))
  done < <(cd "$PROJDIR" && find . -name '*.jsonl' 2>/dev/null | sed 's|^\./||' | sort)
fi

if [ "$FOUND" -eq 0 ]; then
  echo "warning: no transcripts found under $PROJDIR — packing extras only." >&2
else
  echo "staged $FOUND transcript(s)"
fi

for extra in "$@"; do
  if [ ! -e "$extra" ]; then
    echo "warning: skipping missing $extra" >&2
    continue
  fi
  mkdir -p "$STAGE/artifacts"
  cp -R "$extra" "$STAGE/artifacts/"
done

# ---- redact ----------------------------------------------------------------
echo "redacting credentials..."
# Escape the delimiter: patterns contain "://", which would otherwise close s/// early.
SUBS=""
for p in "${PATTERNS[@]}"; do
  SUBS="${SUBS}s/${p//\//\\/}/[REDACTED]/g; "
done

# Preflight: compile the substitutions against empty input before touching any file, so a bad
# pattern fails clean instead of half-rewriting the archive.
if ! printf '' | perl -pe "$SUBS" >/dev/null 2>"$STAGE/.perr"; then
  echo "pattern list does not compile:" >&2
  sed 's/^/  /' "$STAGE/.perr" >&2
  exit 1
fi
rm -f "$STAGE/.perr"

# xargs (not a while-read subshell) so a perl failure propagates through pipefail to set -e.
find "$STAGE" -type f -print0 | xargs -0 perl -i -pe "$SUBS"

# ---- verify ----------------------------------------------------------------
# The load-bearing step. Same engine, same patterns as the redaction above.
echo "verifying..."
CHECK=""
for p in "${PATTERNS[@]}"; do
  CHECK="${CHECK}while (/${p//\//\\/}/g) { print \"\$&\\n\" } "
done

# The `|| true` matters: on a clean archive grep matches nothing and exits 1, which under
# `set -e` + pipefail would abort the script on the success path.
LEAKS="$(find "$STAGE" -type f -print0 \
  | xargs -0 perl -ne "$CHECK" 2>/dev/null \
  | { grep -v '^\[REDACTED\]$' || true; } | wc -l | tr -d ' ')"

if [ "$LEAKS" -gt 0 ]; then
  echo "" >&2
  echo "REFUSING to write the archive: $LEAKS credential-shaped string(s) survived redaction." >&2
  echo "Shapes that got through:" >&2
  find "$STAGE" -type f -print0 | xargs -0 perl -ne "$CHECK" 2>/dev/null \
    | grep -v '^\[REDACTED\]$' | sort -u | head -10 | cut -c1-12 | sed 's/^/  /' >&2
  echo "Fix the PATTERNS list in $HERE/pack.sh, then re-run." >&2
  exit 1
fi
echo "  clean — no credential shapes survived."

# ---- summary stub ----------------------------------------------------------
DATE="$(date +%Y-%m-%d)"
OUT="$HERE/archives/$DATE-$SLUG.zip"
mkdir -p "$HERE/archives"

cat > "$STAGE/SUMMARY.md" <<EOF
# $SLUG — $DATE

<!-- Replace this. The summary is the reason the archive is worth keeping;
     git log already says what was built. -->

## What was decided, and why
(especially decisions that look wrong without their context)

## What was tried and abandoned
(the most expensive thing to rediscover)

## What is still wrong
(so the next session does not file it as new)
EOF

# ---- pack ------------------------------------------------------------------
if [ -e "$OUT" ]; then
  echo "refusing to overwrite $OUT" >&2
  exit 1
fi
( cd "$STAGE" && zip -qr "$OUT" . )

# ---- lock -------------------------------------------------------------------
# Archives are append-only in the same sense as eval artifacts (CLAUDE.md invariant 9): a new
# session adds a record, it never replaces one. Read-only permissions do not achieve that — the
# owner can still rm -f. macOS's user-immutable flag does: rm, mv and truncation all fail with
# "Operation not permitted" until the flag is deliberately cleared.
chmod 444 "$OUT"
if command -v chflags >/dev/null 2>&1; then
  chflags uchg "$OUT"
  if ls -lO "$OUT" 2>/dev/null | grep -q uchg; then
    LOCK="locked (uchg) — rm will refuse"
  else
    LOCK="WARNING: immutable flag did not take; this archive is deletable"
  fi
else
  # Linux fallback; needs root, so report honestly rather than claiming a lock that is not there.
  if command -v chattr >/dev/null 2>&1 && chattr +i "$OUT" 2>/dev/null; then
    LOCK="locked (chattr +i) — rm will refuse"
  else
    LOCK="read-only only — no immutable flag available, rm -f can still delete this"
  fi
fi

SIZE="$(du -h "$OUT" | cut -f1 | tr -d ' ')"
echo ""
echo "wrote $OUT ($SIZE)"
echo "     $LOCK"
echo ""
echo "Now do two things:"
echo "  1. Write the summary — the stub is at SUMMARY.md inside the zip."
echo "  2. Add this line to context/INDEX.md:"
echo ""
echo "| $DATE | \`$DATE-$SLUG.zip\` | _one line: what this session established_ |"
