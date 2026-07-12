/**
 * 知识图谱服务 (v2.0)
 *
 * 封装与知识图谱相关的 API 调用，提供类型安全的方法。
 */

import { apiClient } from '@/lib/api'

export interface GraphStatistics {
  total_entities: number
  total_relations?: number
  total_edges?: number
  total_communities?: number
}

export interface VisualizationRequest {
  center_ids: string[]
  radius?: number
  max_nodes?: number
  format?: string
  layout?: string
  include_properties?: boolean
  highlight_paths?: string[][]
}

export interface VisualizationFormat {
  id: string
  name: string
  description: string
}

export interface LayoutOption {
  id: string
  name: string
  description: string
}

export const knowledgeGraphService = {
  /** 获取图谱统计 */
  getStatistics: async (): Promise<GraphStatistics> => {
    const response = await apiClient.get('/graph/statistics')
    return response.data.data
  },

  /** 搜索实体 */
  searchEntities: async (params: {
    keyword?: string
    entity_type?: string
    kb_id?: number
    limit?: number
    offset?: number
  }) => {
    const response = await apiClient.get('/graph/entities', { params })
    return response.data.data
  },

  /** 获取可视化数据 */
  getVisualization: async (request: VisualizationRequest) => {
    const response = await apiClient.post('/graph/visualization', request)
    return response.data.data
  },

  /** 获取支持的格式和布局 */
  getFormats: async (): Promise<{ formats: VisualizationFormat[]; layouts: LayoutOption[] }> => {
    const response = await apiClient.get('/graph/visualization/formats')
    return response.data.data
  },
}
