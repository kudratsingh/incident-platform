# Documentation map

One line per document, so you can find the right one without opening five.

`CLAUDE.md` at the repo root is the high-signal index: what is shipped, what runs where, and the conventions. `README.md` is the front door. Everything longer lives here.

## Reference — how the platform works today

| Document | What it answers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Runtime topology, five annotated request lifecycles, the concurrency model, the auth and tenant matrix, the failure-mode catalog, and the cost model |
| [DATA_MODEL.md](DATA_MODEL.md) | Every Postgres table, column, index, foreign key and constraint, each with a one-line *why* |
| [KAFKA.md](KAFKA.md) | The topic catalog, the partition-key strategy, schema-evolution rules, and the consumer-group catalog with failure isolation |
| [REDIS.md](REDIS.md) | Every Redis key pattern — who writes it, who reads it, its TTL, and what degrades when Redis is gone |

## Decisions

| Document | What it answers |
|---|---|
| [ADR/](ADR/) | Why the platform looks the way it does. [ADR/README.md](ADR/README.md) indexes all 26 with their statuses and which ones later amended which |

## History — read as a record, not as current state

| Document | What it answers |
|---|---|
| [postmortems/](postmortems/) | What broke, when, why it was not caught, and the rule adopted afterwards. Each file is dated |
| [lessons/](lessons/) | Case studies from building this repo. [parallel-agent-campaigns.md](lessons/parallel-agent-campaigns.md) is what went wrong running several agents over one checkout |

## Forward

| Document | What it answers |
|---|---|
| [ROADMAP.md](ROADMAP.md) | Extension ideas, sized and categorised. Explicitly an inventory of options, not a set of promises |

## Elsewhere in the repo

- [`runbooks/`](../runbooks/) — machine-readable on-call playbooks. 8 files cover the 10 CloudWatch alarms and both SLOs; alarm descriptions link them by `/admin/runbooks/{id}`.
- [`context/`](../context/) — the per-session history convention. `context/INDEX.md` is the map; the archives it indexes are gitignored and absent from a clone.
