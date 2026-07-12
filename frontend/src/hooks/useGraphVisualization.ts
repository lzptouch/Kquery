/**
 * 知识图谱可视化 React Query Hooks (v2.0)
 *
 * 封装知识图谱可视化数据的查询与缓存。
 */

import { useQuery, useMutation } from '@tanstack/react-query'
import { knowledgeGraphService, type VisualizationRequest } from '@/services/knowledgeGraphService'

/** 获取图谱统计 */
export function useGraphStatistics() {
  return useQuery({
    queryKey: ['graph-statistics'],
    queryFn: knowledgeGraphService.getStatistics,
  })
}

/** 搜索图谱实体 */
export function useGraphEntities(params?: {
  keyword?: string
  entity_type?: string
  kb_id?: number
  limit?: number
  offset?: number
}) {
  return useQuery({
    queryKey: ['graph-entities', params],
    queryFn: () => knowledgeGraphService.searchEntities(params || {}),
  })
}

/** 获取可视化格式和布局选项 */
export function useVisualizationFormats() {
  return useQuery({
    queryKey: ['visualization-formats'],
    queryFn: knowledgeGraphService.getFormats,
  })
}

/** 获取可视化数据 */
export function useGraphVisualization() {
  return useMutation({
    mutationFn: (request: VisualizationRequest) => knowledgeGraphService.getVisualization(request),
  })
}
