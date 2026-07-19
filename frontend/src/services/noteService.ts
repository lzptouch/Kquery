/**
 * 笔记服务 (P0-2)
 * 封装 AI 写作笔记 API 调用
 */

import { apiClient } from '@/lib/api'

export interface Note {
  id: string
  title: string
  content: string
  collection_name?: string
  tags?: string[]
  created_at: string
  updated_at: string
}

export interface NoteCreateRequest {
  title: string
  content: string
  collection_name?: string
  tags?: string[]
}

export interface NoteUpdateRequest {
  title?: string
  content?: string
  tags?: string[]
}

export type AIAction = 'continue' | 'polish' | 'summarize' | 'translate' | 'title' | 'outline'

export interface AIActionRequest {
  action: AIAction
  content: string
  context?: string
  target_language?: string
}

export interface NoteIngestRequest {
  note_id: string
  collection_name: string
}

export const noteService = {
  /** 列出笔记 */
  list: async (params?: { collection?: string }): Promise<Note[]> => {
    const response = await apiClient.get('/notes', { params })
    return response.data.data ?? response.data
  },

  /** 获取单条笔记 */
  get: async (id: string): Promise<Note> => {
    const response = await apiClient.get(`/notes/${id}`)
    return response.data.data ?? response.data
  },

  /** 创建笔记 */
  create: async (data: NoteCreateRequest): Promise<Note> => {
    const response = await apiClient.post('/notes', data)
    return response.data.data ?? response.data
  },

  /** 更新笔记 */
  update: async (id: string, data: NoteUpdateRequest): Promise<Note> => {
    const response = await apiClient.put(`/notes/${id}`, data)
    return response.data.data ?? response.data
  },

  /** 删除笔记 */
  delete: async (id: string): Promise<void> => {
    await apiClient.delete(`/notes/${id}`)
  },

  /** AI 操作（非流式） */
  aiAction: async (id: string, data: AIActionRequest): Promise<{ result: string }> => {
    const response = await apiClient.post(`/notes/${id}/ai/${data.action}`, data)
    return response.data.data ?? response.data
  },

  /** AI 流式操作 */
  aiActionStream: async (id: string, data: AIActionRequest): Promise<ReadableStream<Uint8Array>> => {
    const response = await fetch(`/api/v1/notes/${id}/ai/${data.action}/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${localStorage.getItem('token') || ''}`,
      },
      body: JSON.stringify(data),
    })
    if (!response.ok) throw new Error(`AI 操作失败: ${response.status}`)
    return response.body!
  },

  /** 笔记入库 */
  ingest: async (data: NoteIngestRequest): Promise<{ message: string }> => {
    const response = await apiClient.post('/notes/ingest', data)
    return response.data.data ?? response.data
  },
}
