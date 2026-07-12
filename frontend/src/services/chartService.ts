/**
 * 图表数据服务 (v2.0)
 *
 * 封装图表数据 API 调用，统一返回 recharts 可直接绑定的数据。
 */

import { apiClient } from '@/lib/api'

export interface ChartType {
  id: string
  name: string
  description: string
}

export interface ChartDataResponse<T = any> {
  chart_type: string
  title: string
  data: T[]
  metadata?: Record<string, any>
}

export interface DashboardChartsResponse {
  charts: ChartDataResponse[]
  metadata?: Record<string, any>
}

export interface QueryStatsParams {
  granularity?: 'hour' | 'day' | 'month' | 'year'
  date?: string
  start?: string
  end?: string
  limit?: number
}

export interface QueryStatsResponse {
  chart_type: string
  title: string
  data: Array<{
    name: string
    queries: number
    documents: number
    failures?: number
    avg_duration_ms?: number
  }>
  metadata?: Record<string, any>
}

export const chartService = {
  /** 获取支持的图表类型 */
  getTypes: async (): Promise<ChartType[]> => {
    const response = await apiClient.get('/charts/types')
    return response.data.data.types
  },

  /** 获取指定类型的图表数据 */
  getData: async <T = any>(params: {
    chart_type: string
    collection?: string
  }): Promise<ChartDataResponse<T>> => {
    const response = await apiClient.get('/charts/data', { params })
    return response.data.data
  },

  /** 获取仪表盘所需的全部图表数据 */
  getDashboard: async (collection?: string): Promise<DashboardChartsResponse> => {
    const response = await apiClient.get('/charts/dashboard', {
      params: { collection },
    })
    return response.data.data
  },

  /** 获取查询与文档处理统计数据 */
  getQueryStats: async (params?: QueryStatsParams): Promise<QueryStatsResponse> => {
    const response = await apiClient.get('/charts/query-stats', { params })
    return response.data.data
  },
}
