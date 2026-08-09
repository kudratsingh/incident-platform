# ADR 0014 — SSE stream auth: a short-lived, job-bound stream token minted by an authenticated endpoint

**Status:** Accepted · **Date:** 2026-08-09 · **Owner:** Platform

## Context

`GET /jobs/{id}/stream` is the browser's live-progress channel, opened with the native
`EventSource` API. Three defects converged on it (audit findings F1-03, F2-01, F2-03):

1. **No authorization at all (F1-03).** The route depended on `get_current_user` for
   *authentication* but never loaded the job: any authenticated user in any tenant could
   subscribe to any job's progress channel by id.
2. **Unusable auth transport (F2-01).** Native `EventSource` cannot set request headers —
   that is a platform limitation of the API, not a bug in our code. The frontend therefore
   appended `?token=` to the URL, but the backend read only the `Authorization` header via
   `OAuth2PasswordBearer`. Every browser stream request 401'd, and the hook's error handler
   reconnect-looped forever. The streaming feature had never actually worked from a browser.
3. **Primary JWT in the URL (F2-03).** The workaround the frontend attempted — the primary
   access JWT as a query param — puts a long-lived, all-endpoints credential where URLs go:
   proxy and ALB access logs, browser history, and `Referer` headers.

The three are one problem: the stream needs an auth transport a browser can actually use,
and that transport must not be the primary JWT, and whatever authenticates the stream must
also *authorize* the specific job.

## Decision

A **stream token**: short-lived, single-purpose, bound to one job, minted by an
authenticated endpoint.

- `POST /api/v1/jobs/{job_id}/stream-token` authenticates normally (header JWT — a plain
  `fetch` can set headers). It authorizes via `JobService.get_job`, which 404s cross-tenant
  lookups (existence is never confirmed) and 403s non-owners without admin/support role.
  **This call is the F1-03 authorization.** Only then does it mint the token.
- The token is a JWT with `type="stream"`, `sub=str(job_id)`, `tenant_id`, and an expiry of
  `STREAM_TOKEN_TTL_SECONDS = 60`. The subject is the *job*, not the user: the stream route
  compares it to its own path param, so a token minted for job X can never be replayed
  against job Y — without that binding, F1-03 would reopen through the back door.
- `GET /jobs/{job_id}/stream?token=…` validates only the stream token. `get_current_user`
  is deliberately **removed** from the route — leaving it in place would keep 401ing every
  browser, since `OAuth2PasswordBearer` reads only the header. 401 for a missing, invalid,
  expired, or wrong-type token (a primary JWT pasted into the URL is refused); 403 for a
  token bound to a different job.
- The frontend mints a token before every connect and re-mints on every reconnect (the old
  one is expired by then), still gated by the terminal-state check.

A leaked stream URL is now low-value: the credential in it expires in about a minute and
opens exactly one job's progress feed. It grants no REST access — `decode_token` rejects
`type="stream"` everywhere an access token is expected.

## Alternatives considered

**Primary access JWT in the query string** — the smallest diff, and what the frontend
already half-did. Rejected: it cements F2-03. The access JWT lives 30 minutes and opens
every endpoint; URLs are the most-logged, most-shared strings in the system. Especially
untenable while the ALB listener is still plain HTTP (see residual risk below).

**Fetch-based SSE reader** — replace `EventSource` with `fetch()` + a `ReadableStream`
parser, which *can* send the `Authorization` header, keeping tokens out of URLs entirely.
Rejected for this slice: it abandons the browser's native reconnect and `text/event-stream`
parsing, meaning a hand-rolled parser plus a full rewrite of the `useJobStream` lifecycle —
exactly the code a separate work order (reconnect-leak fix) is about to rework. The stream
token achieves the security properties with a surgical diff; a fetch-based reader remains a
reasonable future evolution and would slot in behind the same mint endpoint.

**Cookie-based auth for the stream** — `EventSource` does send cookies. Rejected: the demo
deliberately keeps tokens in localStorage (documented tradeoff), the API is consumed
cross-origin in dev, and introducing a cookie session for one route buys a CSRF surface and
a second auth path to maintain.

## Consequences

- Opening a stream costs one extra round trip (the mint POST). Negligible against an
  SSE connection's lifetime; reconnects were already re-hitting the server.
- The stream route itself no longer touches the DB — its identity is entirely the signed
  token. Revocation inside the 60s window is therefore not possible (e.g. a role revoked
  mid-minute); accepted, since the blast radius is read-only progress events for one job
  the caller was authorized on seconds earlier.
- Anything opening the stream must first call the mint endpoint — `curl` included. That is
  the point: there is no unauthenticated path to the channel anymore. (The incident
  commander does not consume this endpoint; no sibling contract churn.)
- **Residual F2-03 surfaces, explicitly out of scope here:** `infra/alb.tf` still has only
  an HTTP:80 listener, so *all* bearer traffic — headers and stream-token URLs alike —
  travels plaintext until the TLS work lands (Phase 8); and access/refresh tokens remain in
  localStorage as a documented demo tradeoff. This ADR reduces what a logged URL is worth;
  it does not encrypt the wire.
