/**
 * 图表数据 React Query Hooks (v2.0)
 *
 * 封装图表数据的查询与缓存，供 Analytics 页面使用。
 */

import { useQuery } from '@tanstack/react-query'
import { chartService, type ChartDataResponse, type DashboardChartsResponse, type QueryStatsParams, type QueryStatsResponse } from '@/services/chartService'

/** 获取支持的图表类型 */
export function useChartTypes() {
  return useQuery({
    queryKey: ['chart-types'],
    queryFn: chartService.getTypes,
  })
}

/** 获取指定类型的图表数据 */
export function useChartData(chartType: string, collection?: string) {
  return useQuery<ChartDataResponse>({
    queryKey: ['chart-data', chartType, collection],
    queryFn: () => chartService.getData({ chart_type: chartType, collection }),
    enabled: !!chartType,
  })
}

/** 获取仪表盘全部图表数据 */
export function useDashboardCharts(collection?: string) {
  return useQuery<DashboardChartsResponse>({
    queryKey: ['dashboard-charts', collection],
    queryFn: () => chartService.getDashboard(collection),
  })
}

/** 获取查询与文档处理统计数据 */
export function useQueryStats(params?: QueryStatsParams) {
  return useQuery<QueryStatsResponse>({
    queryKey: ['query-stats', params],
    queryFn: () => chartService.getQueryStats(params),
  })
}
