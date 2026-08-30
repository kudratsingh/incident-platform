# context/

Session context archives. A place for what a session *learned* to outlive the session that
learned it.

This exists because the audit campaign ran across a dozen sessions, and every one of them
rediscovered things the last one already knew. `STATE.md` at the workspace root carries the
current state; this directory carries the *history* — the reasoning, the dead ends, and the
detail that was too specific to promote into a summary but too expensive to work out twice.

## The layout

```text
context/
├── README.md      # this file — the convention
├── INDEX.md       # the map. A new session reads this, not the archives.
├── pack.sh        # makes an archive: scrub → verify (different patterns) → zip
├── pack-selftest.sh  # proves pack.sh still scrubs and still catches; runs in a temp dir
└── archives/      # the archives themselves — NOT committed, see below
    └── 2026-08-16-audit-campaign.zip
```

## Read this before you assume the archives are in git

**They are not.** `archives/*.zip` is gitignored, deliberately, for two reasons:

1. **Session transcripts contain live credentials.** A scan of this workspace's transcripts
   found an Anthropic API key and two Postgres URLs with embedded passwords. They land there
   because agents echo `.env` files, print connection strings, and paste bootstrap output.
   `pack.sh` scrubs the patterns it knows, but "the patterns it knows" is not the same as
   "all of them", and a scrubber that has never failed is a scrubber that has not yet met a
   new secret format.
2. **Size.** The raw transcripts for this workspace alone are 120 MB. Git stores every version
   of a binary forever.

So the archives live on disk, next to the repo, and travel by copy rather than by clone. If you
need one on another machine, copy it. If you want one in git, scrub it, read it yourself, and
make that a deliberate commit with a reason in the message — not a habit.

**What *is* committed is `INDEX.md`**, and that is the part that actually does the work.

## How a new session uses this

Read `INDEX.md`. That is the whole instruction.

It is a table of one-line-per-session summaries. Nine times out of ten the line is enough —
you find out that the thing you were about to investigate was already investigated on
2026-08-13 and the answer was "the gap record was read backwards." The tenth time, the line
tells you which archive to open, and you `unzip -p` the one file you need.

Do not try to read an archive into context. A 120 MB transcript is not context, it is a
haystack. The index is the needle map.

```bash
unzip -l context/archives/2026-08-16-audit-campaign.zip     # what's in it
unzip -p context/archives/2026-08-16-audit-campaign.zip SUMMARY.md | less
unzip -p context/archives/2026-08-16-audit-campaign.zip transcript.jsonl \
  | grep -i 'fixture drift'                                  # find the one thing
```

## How to add one, at the end of a session

```bash
./context/pack.sh audit-campaign
```

It stages the transcripts, redacts credentials, **verifies the redaction actually worked and
refuses to write the zip if it did not**, and prints the line to paste into `INDEX.md`.

Then write the `SUMMARY.md` it asks you for. This is the part with all the value and the part
that is tempting to skip. A good summary answers three questions:

- **What was decided, and why** — especially decisions that look wrong without their context.
- **What was tried and abandoned** — the most expensive thing to rediscover.
- **What is still wrong** — known-broken things, so the next session doesn't file them as new.

A summary that only lists what was built is close to worthless; git log already says that.

## Archives cannot be deleted, and that is enforced rather than requested

Every archive is written read-only and flagged **user-immutable** (`chflags uchg`). This is the
same rule as CLAUDE.md invariant 9 for eval artifacts — a new session *adds* a record, it never
replaces one — except here it is enforced by the filesystem instead of by discipline.

Verified on write, not assumed. All four of these fail with *Operation not permitted*:

| attempt | result |
|---|---|
| `rm -f <archive>` | refused |
| `mv <archive> elsewhere` | refused |
| `> <archive>` (truncate) | refused |
| `git clean -xfd context/` | refused, warns, moves on |

That last row is the one that matters most in practice: `git clean -xfd` is the standard "reset my
checkout" reflex, the archives are gitignored, and gitignored files are exactly what it deletes.
Without the flag, one routine cleanup would take the whole history with it.

**To remove one on purpose** — which should be rare and deliberate:

```bash
chflags nouchg context/archives/<name>.zip && rm context/archives/<name>.zip
```

If you find yourself typing that, note in `INDEX.md` that the archive existed and was removed. A
gap that announces itself is fine; a gap that looks like it was never there is not.

**Immutable is not backed up.** The flag stops deletion, not disk failure, and these files are not
in git. If the archives matter, they need to exist somewhere other than this machine.

## Do not trust the scrubber's word for it

A scrubber that checks its own work with its own patterns cannot fail — it can only report that
it did what it does. Three bugs of exactly that shape have been found here:

- the scheme list named `postgres://` and `postgresql://` and therefore sailed past
  `postgresql+asyncpg://` — the scheme this project actually uses — and `https://user:token@`;
- the `s///` delimiter was unescaped, so the `//` inside `postgres://` closed the expression and
  the whole pattern list failed to compile;
- the private-key pattern matched the `-----BEGIN…-----` line only, so the key body and the END
  line survived — and the verifier, which reused that same pattern, called the result clean.

The first two were caught by scanning the finished archive with *different* patterns than the
ones that built it. The third is why `pack.sh` now does that itself: `REDACT_PATTERNS` names
vendor shapes to remove, `VERIFY_PATTERNS` is a separate list that looks for what a secret *is*
— key material, URL userinfo, a labelled quoted value — and some of it describes things the
redactor deliberately does not scrub. A hit there stops the archive rather than being cleaned up
silently. `./context/pack-selftest.sh` exercises both halves against a fixture (a multi-line PEM
key, a symlinked out-of-stage secret, two extras sharing a basename) in a temp directory; run it
after touching either list.

None of that makes the archive *safe*, only checked twice by two different readers. Still scan
from outside the script when you add a pattern:

```bash
unzip -qq context/archives/<name>.zip -d /tmp/check
grep -rEoh '[a-z][a-z0-9+.-]*://[^:/@"[:space:]]+:[^@/"[:space:]]+@' /tmp/check | sort -u
grep -rEoh 'sk-[A-Za-z0-9-]{12,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}' /tmp/check | sort -u
rm -rf /tmp/check
```

A scrubber only knows the secret formats someone thought of. Treat a clean report as "the known
shapes are gone", never as "this is safe to publish."

## Naming

`YYYY-MM-DD-<slug>.zip`, dated for the session's **end**. The slug names the work, not the
session — `audit-campaign`, `stage-1-primitives`, `fixture-drift-burndown`. Several sessions on
one thread can share a slug with different dates, and that reads correctly in `ls`.
