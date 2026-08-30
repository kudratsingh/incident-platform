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
# The redactor and the verifier deliberately use DIFFERENT pattern lists. A verifier built from
# the redactor's own patterns can only ever report "the redactor did what the redactor does";
# it cannot fail. Both bugs found on this script's first real run (see context/README.md) were
# caught by scanning with different patterns than the ones that built the archive, and the
# header-only private-key pattern below was a third: it matched the BEGIN line, left the key
# body and the END line in place, and then reported the archive clean.
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

# ---- redaction patterns ----------------------------------------------------
# Credential shapes, as Perl regexes, applied to the whole file at once (perl -0777) so a
# secret cannot hide by straddling a line boundary.
#
# Block shapes go first and are matched across newlines. They must stay first: each is
# replaced wholesale, so the single-line shapes below only ever see what is left.
REDACT_BLOCK_PATTERNS=(
  # The whole PEM block, BEGIN line through END line. Matches across real newlines and across
  # the literal \n escapes a key wears inside a JSONL transcript, which is how keys actually
  # arrive here. The shipped pattern was the BEGIN line alone, so the key body — the only part
  # that is worth anything to an attacker — survived redaction untouched.
  '-----BEGIN[A-Z -]*PRIVATE KEY-----[\s\S]*?-----END[A-Z -]*PRIVATE KEY-----'
  # A key whose END line never made it into the transcript: the header plus the base64 body
  # that follows it. The 20-char run is required so that prose *mentioning* a PEM header does
  # not swallow the sentence after it.
  '-----BEGIN[A-Z -]*PRIVATE KEY-----(?:\\n|\s)*(?:[A-Za-z0-9+/=]{20,}(?:\\n|\s)*)+'
)

REDACT_PATTERNS=(
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
  # Three base64url segments. Written differently from the verifier's JWT shape on purpose —
  # two independent spellings of one shape catch each other's mistakes.
  'eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+'
)

# ---- verification patterns -------------------------------------------------
# A SEPARATE list, and the load-bearing half of this script. These are not the shapes above
# rearranged: they look for what a secret *is* (key material, userinfo, a labelled value)
# rather than for the vendor prefixes the redactor knows. Some of them describe things the
# redactor deliberately does not scrub — a hit is meant to stop the archive and send a human
# to the redactor list, not to be quietly cleaned up.
VERIFY_PATTERNS=(
  # Key MATERIAL rather than key armour: an RSA/EC/PKCS#8 body base64-encodes a DER SEQUENCE,
  # so it opens "MII". This is exactly what the header-only redaction used to leave behind, and
  # exactly what a verifier sharing that pattern could never see. Not in the redactor list: a
  # bare base64 blob has no self-describing end, so replacing it is not safe to automate — but
  # its presence must stop the archive.
  'MII[A-Za-z0-9+/]{40,}'
  # The other half the header-only pattern left in place.
  '-----END[A-Z -]*PRIVATE KEY-----'
  # Structural: userinfo in a URL of any scheme, spelled without lookbehind.
  '[A-Za-z][A-Za-z0-9+.-]*://[^:/@[:space:]"]+:[^@/[:space:]"]+@'
  # A labelled secret with a quoted literal value. Catches shapes nobody has enumerated yet
  # (vendor keys, internal tokens). The quotes and the 20-char minimum keep it from firing on
  # prose or on code that merely names a key.
  '(?i)(?:api[_-]?key|secret|token|password|passphrase|credential)"?\s*[:=]\s*"[A-Za-z0-9_+/=.-]{20,}"'
  '(?i)aws_secret_access_key\s*[:=]\s*\S{20,}'
  # JWT, spelled independently of the redactor's version: header AND payload must both be
  # base64url JSON objects.
  'eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.'
)

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/ctxpack.XXXXXX")"
# Scratch files live OUTSIDE the stage: anything under $STAGE is archived.
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/ctxwork.XXXXXX")"
trap 'rm -rf "$STAGE" "$SCRATCH"' EXIT

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

COLLISIONS=0
for extra in "$@"; do
  if [ ! -e "$extra" ]; then
    echo "warning: skipping missing $extra" >&2
    continue
  fi
  mkdir -p "$STAGE/artifacts"
  DEST="$STAGE/artifacts/$(basename "$extra")"
  if [ -e "$DEST" ]; then
    # Two extras sharing a basename used to be merged by `cp -R` into one directory, the later
    # one silently overwriting same-named files inside an archive that is then locked
    # immutable. Both survive now, and the renaming is announced.
    COLLISIONS=$((COLLISIONS + 1))
    DEST="$STAGE/artifacts/$COLLISIONS-$(basename "$extra")"
    echo "note: basename collision — staging $extra as artifacts/$(basename "$DEST")" >&2
  fi
  # -L: resolve symlinks at stage time. A staged symlink is skipped by `find -type f` (so it is
  # neither redacted nor verified) and then dereferenced into the zip by `zip`, which is how a
  # symlinked secret used to be packed unredacted and unchecked. Resolving here means what is
  # scanned and what is archived are the same bytes.
  if ! cp -RL "$extra" "$DEST"; then
    echo "REFUSING to write the archive: could not fully stage $extra (broken symlink?)." >&2
    echo "A silently partial archive is worse than none — fix the path and re-run." >&2
    exit 1
  fi
done

# Nothing below walks symlinks, so there must not be any left. This is an assertion, not a
# workaround: if it ever fires, something reached the stage without going through `cp -RL`.
LINKS="$(find "$STAGE" -type l -print 2>/dev/null | head -5 || true)"
if [ -n "$LINKS" ]; then
  echo "REFUSING to write the archive: symlinks reached the stage and would be packed" >&2
  echo "without being redacted or verified:" >&2
  echo "$LINKS" | sed "s|^$STAGE/|  |" >&2
  exit 1
fi

# ---- redact ----------------------------------------------------------------
echo "redacting credentials..."
# Escape the delimiter: patterns contain "://", which would otherwise close s/// early.
SUBS=""
for p in "${REDACT_BLOCK_PATTERNS[@]}" "${REDACT_PATTERNS[@]}"; do
  SUBS="${SUBS}s/${p//\//\\/}/[REDACTED]/g; "
done

CHECK=""
for p in "${VERIFY_PATTERNS[@]}"; do
  CHECK="${CHECK}while (/${p//\//\\/}/g) { print \"\$&\\n\" } "
done

# Preflight: compile BOTH programs against empty input before touching any file. A bad pattern
# then fails clean instead of half-rewriting the archive — and, on the verify side, instead of
# reporting a clean archive because every scan died before it could match anything.
if ! printf '' | perl -0777 -pe "$SUBS" >/dev/null 2>"$SCRATCH/perr"; then
  echo "redaction pattern list does not compile:" >&2
  sed 's/^/  /' "$SCRATCH/perr" >&2
  exit 1
fi
if ! printf '' | perl -0777 -ne "$CHECK" >/dev/null 2>"$SCRATCH/perr"; then
  echo "verification pattern list does not compile:" >&2
  sed 's/^/  /' "$SCRATCH/perr" >&2
  exit 1
fi
rm -f "$SCRATCH/perr"

# -0777: slurp each file whole, so a PEM block spanning many lines is one match. xargs (not a
# while-read subshell) so a perl failure propagates through pipefail to set -e.
find "$STAGE" -type f -print0 | xargs -0 perl -0777 -i -pe "$SUBS"

# ---- verify ----------------------------------------------------------------
# The load-bearing step: same engine, deliberately different patterns.
echo "verifying..."

SCAN_STATUS=0
find "$STAGE" -type f -print0 \
  | xargs -0 perl -0777 -ne "$CHECK" >"$SCRATCH/raw" 2>"$SCRATCH/scanerr" || SCAN_STATUS=$?

# A scan that died reports zero matches, which reads exactly like a clean archive. It is not:
# it is an unknown archive, and the honest answer to unknown is to refuse.
if [ "$SCAN_STATUS" -ne 0 ]; then
  echo "REFUSING to write the archive: the verification scan itself failed (exit $SCAN_STATUS)." >&2
  sed 's/^/  /' "$SCRATCH/scanerr" >&2
  echo "No claim can be made about this archive, so none is made." >&2
  exit 1
fi

# The `|| true` matters: on a clean archive grep matches nothing and exits 1, which under
# `set -e` + pipefail would abort the script on the success path.
grep -v '^\[REDACTED\]$' "$SCRATCH/raw" | sort -u >"$SCRATCH/leaks" || true
LEAKS="$(wc -l <"$SCRATCH/leaks" | tr -d ' ')"

if [ "$LEAKS" -gt 0 ]; then
  # Reported from a FILE, with no early-exit consumer in the pipeline. The `head -10` that used
  # to truncate this could SIGPIPE the whole pipeline, and under `set -euo pipefail` that killed
  # the script mid-report — before the remediation guidance and before its documented exit 1.
  {
    echo ""
    echo "REFUSING to write the archive: $LEAKS distinct credential-shaped string(s) survived redaction."
    echo "Shapes that got through (first 10, truncated):"
    sed -n '1,10p' "$SCRATCH/leaks" | cut -c1-12 | sed 's/^/  /'
    echo ""
    echo "The verifier knows shapes the redactor does not, on purpose, so a hit here usually"
    echo "means a shape is missing from REDACT_PATTERNS in $HERE/pack.sh. Add it and re-run."
    echo "Nothing was written."
  } >&2 || true
  exit 1
fi
echo "  clean — no credential shapes survived (checked with patterns the redactor does not use)."

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
# -y: store symlinks as symlinks instead of following them. The stage is asserted symlink-free
# above, so this is belt and braces — but it is the belt that keeps `zip` from ever archiving
# bytes that the redaction and verification passes never saw.
( cd "$STAGE" && zip -qry "$OUT" . )

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
