/**
 * 多模态内容 React Query Hooks (v2.0)
 *
 * 封装多模态内容的查询、搜索与缓存。
 */

import { useQuery, useMutation } from '@tanstack/react-query'
import { multimodalService, type MultimodalSearchRequest, type MultimodalSearchResponse } from '@/services/multimodalService'

/** 获取支持的多模态内容类型 */
export function useMultimodalTypes() {
  return useQuery({
    queryKey: ['multimodal-types'],
    queryFn: multimodalService.getTypes,
  })
}

/** 多模态内容搜索 */
export function useMultimodalSearch() {
  return useMutation<MultimodalSearchResponse, Error, MultimodalSearchRequest>({
    mutationFn: (request) => multimodalService.search(request),
  })
}

/** 列出多模态内容 */
export function useMultimodalList(params?: {
  collection_name?: string
  chunk_type?: string
  limit?: number
  offset?: number
}) {
  return useQuery({
    queryKey: ['multimodal-list', params],
    queryFn: () => multimodalService.list(params || {}),
  })
}
