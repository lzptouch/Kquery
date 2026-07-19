/**
 * Skills 服务 (P2-2)
 * 封装 Skills 生态 API 调用
 */

import { apiClient } from '@/lib/api'

export interface Skill {
  id: string
  name: string
  description: string
  system_prompt: string
  tools: string[]
  default_kb_id?: string | null
  example_questions: string[]
  model?: string | null
  temperature?: number | null
  is_builtin: boolean
  is_enabled: boolean
  author?: string | null
  version: string
  created_at: string
  updated_at: string
}

export interface SkillCreateRequest {
  name: string
  description: string
  system_prompt: string
  tools: string[]
  default_kb_id?: string | null
  example_questions: string[]
  model?: string | null
  temperature?: number | null
  is_builtin?: boolean
  is_enabled?: boolean
  author?: string | null
  version?: string
}

export interface SkillUpdateRequest {
  name?: string
  description?: string
  system_prompt?: string
  tools?: string[]
  default_kb_id?: string | null
  example_questions?: string[]
  model?: string | null
  temperature?: number | null
  is_enabled?: boolean
  version?: string
}

export interface SkillMatchResult {
  skill_id: string
  skill_name: string
  confidence: number
  reason: string
}

export const skillService = {
  /** 列出 Skills */
  list: async (includeDisabled?: boolean): Promise<Skill[]> => {
    const response = await apiClient.get('/skills', {
      params: { include_disabled: includeDisabled },
    })
    return response.data.data ?? response.data
  },

  /** 获取 Skill 详情 */
  get: async (id: string): Promise<Skill> => {
    const response = await apiClient.get(`/skills/${id}`)
    return response.data.data ?? response.data
  },

  /** 创建 Skill */
  create: async (data: SkillCreateRequest): Promise<Skill> => {
    const response = await apiClient.post('/skills', data)
    return response.data.data ?? response.data
  },

  /** 更新 Skill */
  update: async (id: string, data: SkillUpdateRequest): Promise<Skill> => {
    const response = await apiClient.put(`/skills/${id}`, data)
    return response.data.data ?? response.data
  },

  /** 删除 Skill */
  delete: async (id: string): Promise<void> => {
    await apiClient.delete(`/skills/${id}`)
  },

  /** 启用 Skill */
  enable: async (id: string): Promise<void> => {
    await apiClient.post(`/skills/${id}/enable`)
  },

  /** 禁用 Skill */
  disable: async (id: string): Promise<void> => {
    await apiClient.post(`/skills/${id}/disable`)
  },

  /** 匹配 Skill */
  match: async (query: string, topK?: number): Promise<SkillMatchResult[]> => {
    const response = await apiClient.post('/skills/match', {
      query,
      top_k: topK || 3,
    })
    return response.data.data ?? response.data
  },
}
