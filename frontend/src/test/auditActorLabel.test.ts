/**
 * Audit rows written by a machine principal must name an actor.
 *
 * The campaign's agent authenticates as the `incident-commander` service
 * account, so its rows carry `principal_type='service_account'` and a null
 * `user_id`. The detail modal rendered `user_id` and nothing else, which meant
 * every agent action displayed with no actor at all — in the one view
 * operators use to review what the agent did.
 */
import { describe, expect, it } from 'vitest'

import { auditActorLabel } from '../pages/AdminPage'

describe('auditActorLabel', () => {
  it('names the service account for a machine-written row', () => {
    const label = auditActorLabel({
      principal_type: 'service_account',
      principal_id: 'sa-9f2c',
      user_id: null,
    })

    expect(label).toContain('service account')
    expect(label).toContain('sa-9f2c')
  })

  it('never renders an empty actor when user_id is null', () => {
    const label = auditActorLabel({
      principal_type: 'service_account',
      principal_id: null,
      user_id: null,
    })

    expect(label.trim()).not.toBe('')
    expect(label).toContain('service account')
  })

  it('still names the user for a human-written row', () => {
    const label = auditActorLabel({
      principal_type: 'user',
      principal_id: null,
      user_id: 'user-42',
    })

    expect(label).toContain('user')
    expect(label).toContain('user-42')
  })

  it('falls back to principal_id for a user row missing user_id', () => {
    const label = auditActorLabel({
      principal_type: 'user',
      principal_id: 'principal-7',
      user_id: null,
    })

    expect(label).toContain('principal-7')
  })
})
