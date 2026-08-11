# Running a fix campaign with parallel agents — what worked, and what it cost

Case study from the 2026-08 audit remediation campaign, which landed 44 work orders across
43 PRs on this repo in about six hours with roughly ten agents working at once. It went
well, and the reasons it went well are worth writing down — because two of them were luck
dressed up as design, and one of them was a problem we only found because we went looking.
Written after the campaign closed at `3795805`.

This is not a postmortem: nothing broke in production, and nothing was deployed. It goes in
`docs/lessons/` rather than `docs/postmortems/` for that reason.

## 1. Ten agents worked here because `master` has no required checks

The sibling `incident-commander` repo ran the same kind of campaign at the same time and
had to cut back to two or three agents. Its `main` requires status checks to be up to date,
so every merge made every other open PR stale — one merge costs N−1 rebases across N
agents. An 80-line change there took five rebases and 2.7 hours.

This repo had none of that, because `master` has no branch protection at all. Nothing goes
stale, nothing needs rebasing, and ten agents merged cleanly.

Same task, same tooling, opposite result, and branch protection is the only difference.
That is close to a controlled comparison, and it gives a rule worth keeping:

> Check branch protection before choosing how many agents to run. Required
> up-to-date checks means 2-3 agents. No required checks means the limit is elsewhere.

**But read the next section before treating that as good news.**

## 2. No branch protection means the discipline is yours to keep

The flip side of merging without friction is merging without a gate. On this repo nothing
mechanically stops a direct push to `master`, and nothing stops merging a red PR. During
the campaign the rule was self-imposed: always branch, always open a PR, always wait for
`ci.yml` green, never push to `master` directly. All 43 PRs held to it, and no merge used
`--admin`.

That worked, but it worked because it was written into the agent's instructions and
checked afterwards. It was not enforced. If you run this again, either say so explicitly in
the agent's brief, or turn on branch protection and accept the rebase cost from section 1.

Picking neither is the bad option: no gate and no stated rule means the first agent under
time pressure discovers it can merge red.

## 3. A permanently-failing CI job hides everything behind it

We found this at the start of the campaign, and fixing it first was what made the rest of
the run legible.

`ci.yml`'s `Build & Deploy to ECS` job runs on every push to `master` and fails at
**Configure AWS credentials**, because no AWS secrets are configured. Every deploy step
after it is skipped and the run is marked failed. So **every merge to `master` produced a
red CI run**, and had done for a long time.

The cost is not the failing job. It is that a genuine breakage looks exactly like the
standing failure, so nobody looks. Signal that is always red carries no information.

The fix (PR #96) gates the job on an `ENABLE_ECS_DEPLOY` repository variable, so it reports
**skipped** instead of failed. `master` went green for the first time, and from then on a
red run meant somebody had actually broken something — which is the only reason the rest of
the campaign could be trusted.

> A CI job that cannot pass in the current environment should skip, not fail. Gate it on
> an explicit opt-in, and say in the gate's comment what turning it on will do.

Related: re-enabling that job means every merge deploys to real infrastructure. The gate
comment says so, and it should keep saying so.

## 4. Concurrent agents allocating ADR numbers

Roughly ten agents wrote ADRs during the campaign and none collided, but only because
numbers were checked against the directory at write time rather than reserved up front.
This is worth being deliberate about next time: ADR numbers are a shared counter, and a
shared counter with ten concurrent writers is a race unless something serialises it.

The campaign also hit the mirror image of this in the sibling repo, where three work orders
had each independently earmarked the same ADR number in advance. The fix there was to say
plainly that the first one to land takes the number and the rest renumber.

## What to check before the next campaign

1. What does branch protection say, and how many agents does that allow?
2. If there is no protection, is the branch-PR-CI-merge discipline written into the brief?
3. Is any CI job currently failing for environmental reasons? Gate it before you start.
4. How are shared counters — ADR numbers, migration revisions — allocated under concurrency?

## See also

- `incident-commander/docs/lessons/parallel-agent-campaigns.md` — the same campaign from the
  other repo, including a near-miss data loss where `git stash` turned out to be shared
  across worktrees.
- [ADR 0013](../ADR/0013-release-before-rerun.md) — the release-ordering decision this
  campaign ran under.
