/**
 * 知识库服务 (P2-1)
 * 封装共享知识库 API 调用
 */

import { apiClient } from '@/lib/api'

export interface KB {
  id: string
  name: string
  collection_name: string
  owner_id: string
  visibility: 'public' | 'private'
  description?: string
  member_count: number
  created_at: string
}

export interface KBMember {
  id: string
  kb_id: string
  user_id: string
  username: string
  role: 'owner' | 'admin' | 'editor' | 'viewer'
  joined_at: string
}

export interface KBInvite {
  id: string
  kb_id: string
  code: string
  role: string
  invite_url: string
  expires_at?: string
  max_uses: number
  used_count: number
  is_active: boolean
}

export interface KBCreateRequest {
  name: string
  description?: string
  visibility?: 'public' | 'private'
}

export interface AddMemberRequest {
  username: string
  role: string
}

export interface UpdateRoleRequest {
  role: string
}

export interface InviteRequest {
  role?: string
  expires_in_hours?: number
  max_uses?: number
}

export const kbService = {
  /** 列出我的知识库 */
  list: async (): Promise<KB[]> => {
    const response = await apiClient.get('/knowledge-bases')
    return response.data.data ?? response.data
  },

  /** 获取知识库详情 */
  get: async (id: string): Promise<KB> => {
    const response = await apiClient.get(`/knowledge-bases/${id}`)
    return response.data.data ?? response.data
  },

  /** 创建知识库 */
  create: async (data: KBCreateRequest): Promise<KB> => {
    const response = await apiClient.post('/knowledge-bases', data)
    return response.data.data ?? response.data
  },

  /** 删除知识库 */
  delete: async (id: string): Promise<void> => {
    await apiClient.delete(`/knowledge-bases/${id}`)
  },

  /** 列出成员 */
  listMembers: async (kbId: string): Promise<KBMember[]> => {
    const response = await apiClient.get(`/knowledge-bases/${kbId}/members`)
    return response.data.data ?? response.data
  },

  /** 添加成员 */
  addMember: async (kbId: string, data: AddMemberRequest): Promise<void> => {
    await apiClient.post(`/knowledge-bases/${kbId}/members`, data)
  },

  /** 移除成员 */
  removeMember: async (kbId: string, userId: string): Promise<void> => {
    await apiClient.delete(`/knowledge-bases/${kbId}/members/${userId}`)
  },

  /** 更新成员角色 */
  updateRole: async (kbId: string, memberId: string, data: UpdateRoleRequest): Promise<void> => {
    await apiClient.put(`/knowledge-bases/${kbId}/members/${memberId}/role`, data)
  },

  /** 生成邀请码 */
  generateInvite: async (kbId: string, data?: InviteRequest): Promise<KBInvite> => {
    const response = await apiClient.post(`/knowledge-bases/${kbId}/invites`, data || {})
    return response.data.data ?? response.data
  },

  /** 通过邀请码加入 */
  joinByCode: async (code: string): Promise<void> => {
    await apiClient.post('/knowledge-bases/join', { code })
  },
}
