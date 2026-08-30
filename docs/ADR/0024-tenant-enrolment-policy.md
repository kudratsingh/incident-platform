# ADR 0024 — Public registration may found a tenant or join the default one, and nothing else

**Status:** Accepted · **Date:** 2026 Q3 · **Owner:** Platform

## Context

`POST /auth/register` is unauthenticated by design — it is the front door. It
also accepted a free-form `tenant_slug`, and that slug decided which tenant the
new account landed in:

```python
tenant = await self.tenant_repo.get_by_slug(tenant_slug)
if tenant is None and new_tenant_name:
    tenant = await self.tenant_repo.create(...)   # founder path
    role = "admin"
if tenant is None or not tenant.is_active:
    raise NotFoundError(...)
# ...otherwise: create the user inside whatever tenant that slug named
```

The founder branch only fires when the slug is **free**. Naming a slug that
already existed skipped it and fell through to "create the user in that
tenant" — so anyone who could guess or read a tenant slug could put an account
inside an organisation they had no relationship with. No auth, no invite, no
domain check, no rate of discovery worth mentioning: slugs are short, human
and frequently visible.

Nothing in the repository recorded this as a decision. It was not documented
as open self-enrolment, and it was not documented as a defect; it was simply
what the code did, which is the state this ADR exists to end either way.

**Impact, stated honestly.** The verifier tempered it and was right to: the
registrant gets `role="user"`, the least-privileged role, and today's
tenant-scoped reads are not open to that role. So the realistic damage is
shared quota and rate-limit exhaustion, idempotency-key squatting inside the
victim's namespace, and roster pollution that an admin has to clean up. What
makes it P1 is not today's blast radius but its shape: it is a tenancy
boundary that does not hold, on an unauthenticated endpoint, and it rises to
high the moment any tenant-scoped read opens to `role=user` — a change nobody
would think of as a security change.

## Decision

### 1. Unauthenticated registration may reach exactly two destinations

* **A brand-new tenant** — the founder path, unchanged. A slug nobody holds
  has nobody to harm, and the registrant becomes that tenant's `admin` so it
  has an operator from its first second. Still no `is_platform_admin`: that is
  the cross-tenant role and only an existing platform admin grants it.
* **The default tenant** — open by design. It is the demo and self-serve
  pool; closing it would break the front door rather than the hole.

Naming any **other existing tenant** is refused with `403`.

### 2. Existing-tenant enrolment moves behind an authenticated admin

`POST /auth/tenant/members`, `role=admin` required, creates a user **in the
caller's own tenant**. Two properties carry the security, and both are about
where the inputs come from rather than what they contain:

* the tenant is read off the authenticated admin, and the request body has no
  tenant field at all. A tenant identifier the caller gets to choose is the
  entire defect above; the fix is not to validate that field but to not have
  it.
* the role is hard-coded to `user`. An admin may grow their own tenant; they
  may not mint a second admin here (X-01 / F1-04).

This endpoint is why closing public self-enrolment is not a functional
regression. Without it a founder could create a tenant and then never add a
single colleague, because the path this order shuts was the only one that
existed.

### 3. It is admin provisioning, not an invite — deliberately

The admin supplies the initial password. There is no token, no email
round-trip, no expiry, no acceptance step.

The fix shape offered two richer options and both were declined for now:

* **Invite tokens** are the right long-term answer and are a feature, not a
  guard: a table, a redemption endpoint, expiry, revocation, single-use
  semantics, and an email path to deliver them. A half-built invite flow —
  tokens that never expire, or that are mailed by nothing — would be worse
  than an honest small mechanism, because it would *look* like the real thing
  in a security review.
* **Email-domain matching** needs a verified-domain column on `tenants` and a
  domain-verification story to go with it. Without verification it is not a
  check: anyone can put `@acme.com` in a registration body. With verification
  it is a bigger feature than invites.

Both are recorded in [ROADMAP.md](../ROADMAP.md). The interim mechanism is
small enough to be obviously correct, and it moves the boundary to the right
place, which is the part that cannot wait.

## Consequences

**403 tells a caller the tenant exists.** The refusal is distinguishable from
the `404` an unknown slug still returns, so registration can be used to probe
which slugs are taken. This is accepted rather than hidden, for two reasons:
the founder path already discloses the same fact (asking to create a slug that
exists no longer creates it), and collapsing both cases into `404` would tell
a legitimate colleague that their own company's tenant does not exist, which
turns a support question into a wrong answer. Tenant slugs are not secrets and
must not become part of anyone's threat model; if that changes, the honest fix
is a uniform response on both paths plus a rate limit, not a different status
code here.

**The default tenant is now a documented trust boundary rather than an
accident.** Everything registering without a slug shares one tenant's quota
and rate limit. That was already true; it now has a name, and anyone raising
the default tenant's limits should know they are raising them for the public.

**`register` keeps its `tenant_slug` parameter.** It would be tempting to
delete the field now that it selects between only two outcomes, but the
founder path genuinely needs a caller-chosen slug, and a body that carries
`new_tenant_name` without a slug would have to invent one. The field stays;
what changed is that the service no longer treats it as an instruction it is
obliged to follow.

**Existing admin tooling is unaffected.** No endpoint in `api/admin.py`
changed, and `AuthService.register`'s only production caller is the auth
router, so nothing else in the tree had to move.
