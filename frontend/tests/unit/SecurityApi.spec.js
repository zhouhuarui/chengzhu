import { afterEach, describe, expect, it, vi } from 'vitest'
import { securityApi, service } from '../../src/api/index.js'

describe('Security API', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('searches the local security master with a bounded result count', async () => {
    const get = vi.spyOn(service, 'get').mockResolvedValue({ data: { items: [] } })

    await securityApi.search('科德', 10)

    expect(get).toHaveBeenCalledWith('/api/security/search', {
      params: { q: '科德', limit: 10 },
    })
  })
})
