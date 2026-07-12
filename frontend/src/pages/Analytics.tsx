import React, { useState } from 'react'
import {
  PieChart,
  Pie,
  Cell,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  LineChart,
  Line,
  AreaChart,
  Area,
  ResponsiveContainer,
} from 'recharts'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Badge } from '@/components/ui/badge'
import { BarChart3, PieChart as PieChartIcon, TrendingUp, Activity, Loader2 } from 'lucide-react'
import { useDashboardCharts, useQueryStats } from '@/hooks/useChartData'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

// 预设颜色板
const COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16']

interface ChartDatum {
  name: string
  value: number
  [key: string]: string | number
}

export default function Analytics() {
  const { data, isLoading, error } = useDashboardCharts()
  const charts = data?.charts || []

  const findChart = (type: string) => charts.find((c) => c.chart_type === type)

  const pieChart = findChart('pie')
  const barChart = findChart('bar')
  const lineChart = findChart('line')
  const areaChart = findChart('area')

  // 粒度与下钻状态
  const [granularity, setGranularity] = useState<'hour' | 'day' | 'month' | 'year'>('month')
  const [selectedDate, setSelectedDate] = useState<string | undefined>(undefined)
  const { data: queryStats, isLoading: statsLoading } = useQueryStats({
    granularity,
    date: selectedDate,
    limit: granularity === 'hour' ? 24 : granularity === 'day' ? 31 : granularity === 'month' ? 12 : 5,
  })

  /** 点击柱子下钻 */
  const handleBarClick = (data: any) => {
    if (!data?.activeLabel) return
    const name = data.activeLabel as string

    if (granularity === 'year') {
      setSelectedDate(`${name}-01-01`)
      setGranularity('month')
    } else if (granularity === 'month') {
      setSelectedDate(`${name}-01`)
      setGranularity('day')
    } else if (granularity === 'day') {
      setSelectedDate(name)
      setGranularity('hour')
    }
  }

  /** 返回上级粒度 */
  const handleDrillUp = () => {
    if (granularity === 'hour') {
      setGranularity('day')
      setSelectedDate(selectedDate?.substring(0, 7))
    } else if (granularity === 'day') {
      setGranularity('month')
      setSelectedDate(selectedDate?.substring(0, 4))
    } else if (granularity === 'month') {
      setGranularity('year')
      setSelectedDate(undefined)
    }
  }

  /** 手动切换粒度时重置下钻状态 */
  const handleGranularityChange = (newGranularity: 'hour' | 'day' | 'month' | 'year') => {
    setGranularity(newGranularity)
    setSelectedDate(undefined)
  }

  /** 格式化 X 轴标签 */
  const formatAxisLabel = (value: string) => {
    if (granularity === 'hour') {
      const parts = value.split(' ')
      return parts.length > 1 ? parts[1] + ':00' : value
    }
    if (granularity === 'day') {
      return value.substring(5)
    }
    if (granularity === 'month') {
      return value.substring(5) + '月'
    }
    return value
  }

  /** 格式化 Tooltip 标签 */
  const formatTooltipLabel = (value: string) => {
    if (granularity === 'hour') {
      const parts = value.split(' ')
      return parts.length > 1 ? `${parts[0]} ${parts[1]}:00` : value
    }
    return value
  }

  return (
    <div className="space-y-6 animate-fade-in">
      {/* 页面标题 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight flex items-center gap-2">
            <BarChart3 className="h-7 w-7 text-primary" />
            数据分析
          </h1>
          <p className="text-muted-foreground mt-1">
            知识库多维度统计与可视化洞察
          </p>
        </div>
        {isLoading && (
          <Badge variant="secondary" className="flex items-center gap-1">
            <Loader2 className="h-3 w-3 animate-spin" />
            加载中
          </Badge>
        )}
      </div>

      {/* 错误提示 */}
      {error && (
        <div className="p-4 rounded-lg border border-red-200 bg-red-50 text-red-700">
          加载图表数据失败: {error.message}
        </div>
      )}

      {/* 图表网格 */}
      <Tabs defaultValue="overview" className="space-y-4">
        <TabsList>
          <TabsTrigger value="overview">概览</TabsTrigger>
          <TabsTrigger value="distribution">分布</TabsTrigger>
          <TabsTrigger value="trends">趋势</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="space-y-4">
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {/* 饼图 */}
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base flex items-center gap-2">
                  <PieChartIcon className="h-4 w-4 text-primary" />
                  {pieChart?.title || '知识库分布'}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <ResponsiveContainer width="100%" height={280}>
                  <PieChart>
                    <Pie
                      data={pieChart?.data || []}
                      cx="50%"
                      cy="50%"
                      innerRadius={60}
                      outerRadius={100}
                      paddingAngle={4}
                      dataKey="value"
                      nameKey="name"
                    >
                      {(pieChart?.data || []).map((entry: ChartDatum, index: number) => (
                        <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                      ))}
                    </Pie>
                    <Tooltip formatter={(value: number, name: string) => [`${value}`, `${name}`]} />
                    <Legend />
                  </PieChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>

            {/* 柱状图（支持粒度切换与下钻） */}
            <Card>
              <CardHeader className="pb-2">
                <div className="flex items-center justify-between">
                  <CardTitle className="text-base flex items-center gap-2">
                    <BarChart3 className="h-4 w-4 text-primary" />
                    {queryStats?.title || '查询与文档统计'}
                  </CardTitle>
                  <div className="flex items-center gap-2">
                    {granularity !== 'month' && selectedDate && (
                      <Button variant="ghost" size="sm" className="h-8 text-xs" onClick={handleDrillUp}>
                        返回上级
                      </Button>
                    )}
                    <Select value={granularity} onValueChange={(v) => handleGranularityChange(v as any)}>
                      <SelectTrigger className="w-24 h-8 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="hour">按小时</SelectItem>
                        <SelectItem value="day">按日</SelectItem>
                        <SelectItem value="month">按月</SelectItem>
                        <SelectItem value="year">按年</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>
              </CardHeader>
              <CardContent>
                {statsLoading ? (
                  <div className="flex items-center justify-center h-[280px] text-muted-foreground text-sm">
                    <Loader2 className="h-5 w-5 animate-spin mr-2" />
                    加载中...
                  </div>
                ) : queryStats?.data?.length > 0 ? (
                  <ResponsiveContainer width="100%" height={280}>
                    <BarChart data={queryStats.data} onClick={handleBarClick}>
                      <CartesianGrid strokeDasharray="3 3" />
                      <XAxis dataKey="name" tickFormatter={formatAxisLabel} />
                      <YAxis />
                      <Tooltip labelFormatter={formatTooltipLabel} />
                      <Legend />
                      <Bar dataKey="queries" name="查询次数" fill="#3b82f6" radius={[4, 4, 0, 0]} cursor="pointer" />
                      <Bar dataKey="documents" name="文档处理" fill="#10b981" radius={[4, 4, 0, 0]} cursor="pointer" />
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="flex items-center justify-center h-[280px] text-muted-foreground text-sm">
                    暂无图表数据
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="distribution" className="space-y-4">
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {/* 面积图 */}
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base flex items-center gap-2">
                  <Activity className="h-4 w-4 text-primary" />
                  {areaChart?.title || '知识积累趋势'}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <ResponsiveContainer width="100%" height={280}>
                  <AreaChart data={areaChart?.data || []}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis dataKey="name" />
                    <YAxis />
                    <Tooltip />
                    <Area type="monotone" dataKey="value" stroke="#3b82f6" fill="#3b82f6" fillOpacity={0.15} />
                  </AreaChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>

            {/* 雷达图占位（使用饼图数据适配） */}
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base flex items-center gap-2">
                  <TrendingUp className="h-4 w-4 text-primary" />
                  实体关系分布
                </CardTitle>
              </CardHeader>
              <CardContent>
                <ResponsiveContainer width="100%" height={280}>
                  <BarChart data={pieChart?.data || []} layout="vertical">
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis type="number" />
                    <YAxis dataKey="name" type="category" width={80} />
                    <Tooltip />
                    <Bar dataKey="value" radius={[0, 4, 4, 0]}>
                      {(pieChart?.data || []).map((entry: ChartDatum, index: number) => (
                        <Cell key={`hbar-${index}`} fill={COLORS[index % COLORS.length]} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="trends" className="space-y-4">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base flex items-center gap-2">
                <TrendingUp className="h-4 w-4 text-primary" />
                {lineChart?.title || '查询趋势分析'}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <ResponsiveContainer width="100%" height={360}>
                <LineChart data={lineChart?.data || []}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="name" />
                  <YAxis />
                  <Tooltip />
                  <Legend />
                  <Line type="monotone" dataKey="value" stroke="#3b82f6" strokeWidth={2} dot={{ r: 4 }} activeDot={{ r: 6 }} />
                </LineChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  )
}
