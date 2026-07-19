/**
 * 任务服务 (P1-3)
 * 封装任务式 Agent API 调用
 */

import { apiClient } from '@/lib/api'

export interface Task {
  id: string
  title: string
  description: string
  status: 'pending' | 'planning' | 'executing' | 'completed' | 'failed' | 'cancelled'
  steps: TaskStep[]
  result?: string
  error?: string
  created_at: string
  updated_at: string
}

export interface TaskStep {
  id: string
  description: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  result?: string
  error?: string
}

export interface TaskCreateRequest {
  title: string
  description: string
  context?: string
  collection_name?: string
}

export const taskService = {
  /** 创建任务 */
  create: async (data: TaskCreateRequest): Promise<Task> => {
    const response = await apiClient.post('/tasks', data)
    return response.data.data ?? response.data
  },

  /** 获取任务详情 */
  get: async (id: string): Promise<Task> => {
    const response = await apiClient.get(`/tasks/${id}`)
    return response.data.data ?? response.data
  },

  /** 列出所有任务 */
  list: async (): Promise<Task[]> => {
    const response = await apiClient.get('/tasks')
    return response.data.data ?? response.data
  },

  /** 执行任务 */
  execute: async (id: string): Promise<Task> => {
    const response = await apiClient.post(`/tasks/${id}/execute`)
    return response.data.data ?? response.data
  },

  /** 取消任务 */
  cancel: async (id: string): Promise<void> => {
    await apiClient.post(`/tasks/${id}/cancel`)
  },

  /** SSE 流式任务进度 */
  streamProgress: (id: string, onProgress: (data: any) => void, onError?: (err: Error) => void) => {
    const token = localStorage.getItem('token') || ''
    const eventSource = new EventSource(
      `/api/v1/tasks/${id}/stream?token=${encodeURIComponent(token)}`
    )

    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        onProgress(data)
      } catch {
        // 非 JSON 消息忽略
      }
    }

    eventSource.onerror = () => {
      eventSource.close()
      onError?.(new Error('任务流连接中断'))
    }

    return () => eventSource.close()
  },
}
