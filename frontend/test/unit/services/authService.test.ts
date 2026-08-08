import { authService } from '@/services/authService'
import { apiClient } from '@/lib/api'

vi.mock('@/lib/api', () => ({
  apiClient: {
    post: vi.fn(),
    get: vi.fn(),
  },
}))

const mockedApiClient = vi.mocked(apiClient, true)

describe('authService', () => {
  beforeEach(() => {
    localStorage.clear()
    mockedApiClient.post.mockReset()
    mockedApiClient.get.mockReset()
  })

  it('login should save tokens and return token data', async () => {
    const tokenData = {
      access_token: 'a-token',
      refresh_token: 'r-token',
      token_type: 'bearer',
      expires_in: 3600,
    }
    mockedApiClient.post.mockResolvedValue({ data: tokenData } as any)

    const result = await authService.login({ username: 'u', password: 'p' })

    expect(result.access_token).toBe('a-token')
    expect(authService.getToken()).toBe('a-token')
    expect(authService.getRefreshToken()).toBe('r-token')
  })

  it('login should unwrap data wrapper', async () => {
    mockedApiClient.post.mockResolvedValue({ data: { data: { access_token: 'a', refresh_token: 'r', token_type: 'bearer', expires_in: 3600 } } } as any)

    const result = await authService.login({ username: 'u', password: 'p' })

    expect(result.access_token).toBe('a')
  })

  it('register should return user', async () => {
    mockedApiClient.post.mockResolvedValue({ data: { id: 'u1', username: 'u' } } as any)

    const result = await authService.register({ username: 'u', password: 'p' })

    expect(result.username).toBe('u')
  })

  it('getMe should unwrap data wrapper', async () => {
    mockedApiClient.get.mockResolvedValue({ data: { data: { id: 'u1', username: 'u' } } } as any)

    const result = await authService.getMe()

    expect(result.username).toBe('u')
  })

  it('refresh should throw without refresh token', async () => {
    await expect(authService.refresh()).rejects.toThrow('无刷新令牌')
  })

  it('isAuthenticated should reflect localStorage', () => {
    expect(authService.isAuthenticated()).toBe(false)
    authService.saveToken('a', 'r')
    expect(authService.isAuthenticated()).toBe(true)
  })

  it('logout should clear tokens', () => {
    authService.saveToken('a', 'r')
    authService.logout()
    expect(authService.isAuthenticated()).toBe(false)
  })
})
