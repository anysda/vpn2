import type { APIRequestContext } from '@playwright/test'
import { expect } from '@playwright/test'
import type { AccessOperation } from '../../shared/access-operation'

export async function waitAccess(api: APIRequestContext, operation: Pick<AccessOperation, 'id'>): Promise<AccessOperation> {
  let state!: AccessOperation
  await expect.poll(async () => {
    const response = await api.get(`/api/access-operations/${operation.id}`)
    expect(response.ok()).toBe(true)
    state = await response.json()
    return state.status
  }, { timeout: 45_000, message: 'Все включённые VPN-службы подтвердили операцию' }).toBe('confirmed')
  return state
}
