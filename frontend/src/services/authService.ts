/**
 * 认证服务 (P2-1)
 * 封装用户认证 API 调用
 */

import { apiClient } from '@/lib/api'

export interface User {
  id: string
  username: string
  email?: string
  display_name?: string
  avatar_url?: string
  is_active: boolean
  is_superuser: boolean
  created_at: string
}

export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export interface LoginRequest {
  username: string
  password: string
}

export interface RegisterRequest {
  username: string
  password: string
  email?: string
  display_name?: string
}

const TOKEN_KEY = 'kquery_token'
const REFRESH_KEY = 'kquery_refresh_token'

export const authService = {
  /** 登录 */
  login: async (data: LoginRequest): Promise<TokenResponse> => {
    const response = await apiClient.post('/auth/login', data)
    const tokenData: TokenResponse = response.data.data ?? response.data
    authService.saveToken(tokenData.access_token, tokenData.refresh_token)
    return tokenData
  },

  /** 注册 */
  register: async (data: RegisterRequest): Promise<User> => {
    const response = await apiClient.post('/auth/register', data)
    return response.data.data ?? response.data
  },

  /** 获取当前用户 */
  getMe: async (): Promise<User> => {
    const response = await apiClient.get('/auth/me')
    return response.data.data ?? response.data
  },

  /** 刷新 token */
  refresh: async (): Promise<TokenResponse> => {
    const refreshToken = authService.getRefreshToken()
    if (!refreshToken) throw new Error('无刷新令牌')

    const response = await apiClient.post('/auth/refresh', {
      refresh_token: refreshToken,
    })
    const tokenData: TokenResponse = response.data.data ?? response.data
    authService.saveToken(tokenData.access_token, tokenData.refresh_token)
    return tokenData
  },

  /** 保存认证令牌 */
  saveToken: (accessToken: string, refreshToken: string) => {
    localStorage.setItem(TOKEN_KEY, accessToken)
    localStorage.setItem(REFRESH_KEY, refreshToken)
  },

  /** 获取访问令牌 */
  getToken: (): string | null => {
    return localStorage.getItem(TOKEN_KEY)
  },

  /** 获取刷新令牌 */
  getRefreshToken: (): string | null => {
    return localStorage.getItem(REFRESH_KEY)
  },

  /** 是否已登录 */
  isAuthenticated: (): boolean => {
    return !!localStorage.getItem(TOKEN_KEY)
  },

  /** 登出 */
  logout: () => {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(REFRESH_KEY)
  },
}
