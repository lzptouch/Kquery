/**
 * 多模态内容服务 (v2.0)
 *
 * 封装多模态内容检索 API，支持图像描述、表格、表格洞察等。
 */

import { apiClient } from '@/lib/api'

export interface MultimodalType {
  id: string
  name: string
  description: string
  icon: string
}

export interface MultimodalSearchRequest {
  query: string
  collection_name?: string
  chunk_types?: string[]
  top_k?: number
  filter?: string
}

export interface MultimodalItem {
  id: string
  chunk_type: string
  content: string
  score: number
  metadata: Record<string, any>
  source_file: string
  page_number?: number
}

export interface MultimodalSearchResponse {
  query: string
  chunk_types: string[]
  total: number
  results: MultimodalItem[]
}

export const multimodalService = {
  /** 获取支持的多模态内容类型 */
  getTypes: async (): Promise<MultimodalType[]> => {
    const response = await apiClient.get('/multimodal/types')
    return response.data.data.types
  },

  /** 检索多模态内容 */
  search: async (request: MultimodalSearchRequest): Promise<MultimodalSearchResponse> => {
    const response = await apiClient.post('/multimodal/search', {
      query: request.query,
      collection_name: request.collection_name || 'default',
      chunk_types: request.chunk_types || ['image_description', 'table', 'table_description'],
      top_k: request.top_k || 10,
      filter: request.filter,
    })
    return response.data.data
  },

  /** 列出多模态内容 */
  list: async (params: {
    collection_name?: string
    chunk_type?: string
    limit?: number
    offset?: number
  }): Promise<{ total: number; results: MultimodalItem[] }> => {
    const response = await apiClient.get('/multimodal/content', { params })
    return response.data.data
  },
}
