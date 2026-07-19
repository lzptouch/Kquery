import React, { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import {
  Database,
  FileText,
  Network,
  MessageSquare,
  TrendingUp,
  Users,
  Activity,
  Search,
  ImageIcon,
  Loader2,
  BarChart3,
  PieChart as PieChartIcon,
  ArrowRight,
  Eye,
  Table,
  Sparkles,
  ArrowUpRight,
  ArrowDownRight,
  Clock,
  Layers,
  RefreshCw,
} from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { collectionApi } from '@/lib/api'
import { useDashboardCharts, useQueryStats } from '@/hooks/useChartData'
import { useMultimodalList, useMultimodalSearch } from '@/hooks/useMultimodalSearch'
import type { MultimodalItem } from '@/services/multimodalService'
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
  ResponsiveContainer,
} from 'recharts'

// ========================================
// 常量
// ========================================

const COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16']

const multimodalIcons: Record<string, React.ComponentType<{ className?: string }>> = {
  image_description: ImageIcon,
  table: Table,
  table_description: FileText,
}

const multimodalLabels: Record<string, string> = {
  image_description: '图片描述',
  table: '表格数据',
  table_description: '表格洞察',
}

const multimodalColors: Record<string, string> = {
  image_description: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300',
  table: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
  table_description: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300',
}

// ========================================
// 类型
// ========================================

interface Stats {
  knowledgeBaseCount: number
  documentCount: number
  graphEntityCount: number
  relationCount: number
}

interface ChartDatum {
  name: string
  value: number
  [key: string]: string | number
}

// ========================================
// 组件
// ========================================

export function Dashboard() {
  const [stats, setStats] = useState<Stats>({
    knowledgeBaseCount: 0,
    documentCount: 0,
    graphEntityCount: 0,
    relationCount: 0,
  })
  const [isLoading, setIsLoading] = useState(true)
  const [mmQuery, setMmQuery] = useState('')

  // 图表数据
  const { data: chartData } = useDashboardCharts()
  const charts = chartData?.charts || []
  const findChart = (type: string) => charts.find((c: any) => c.chart_type === type)
  const pieChart = findChart('pie')
  const barChart = findChart('bar')

  // 粒度与下钻状态
  const [granularity, setGranularity] = useState<'hour' | 'day' | 'month' | 'year'>('month')
  const [selectedDate, setSelectedDate] = useState<string | undefined>(undefined)
  const { data: queryStats, isLoading: statsLoading } = useQueryStats({
    granularity,
    date: selectedDate,
    limit: granularity === 'hour' ? 24 : granularity === 'day' ? 31 : granularity === 'month' ? 12 : 5,
  })

  // 多模态数据
  const { data: mmListData, isLoading: mmLoading } = useMultimodalList({ limit: 6 })
  const mmSearch = useMultimodalSearch()
  const mmItems = mmSearch.data?.results || mmListData?.results || []
  const isSearching = mmSearch.isPending

  useEffect(() => {
    fetchStats()
  }, [])

  const fetchStats = async () => {
    setIsLoading(true)
    try {
      let collections: string[] = []
      let totalDocs = 0

      try {
        const listResp = await collectionApi.list()
        collections = listResp.data.collections || []
        for (const name of collections) {
          try {
            const statsResp = await collectionApi.stats(name)
            totalDocs += statsResp.data?.count || 0
          } catch (e) {
            console.error(`获取集合 ${name} 统计失败:`, e)
          }
        }
      } catch (e) {
        console.error('获取知识库列表失败:', e)
      }

      let graphEntityCount = 0
      let relationCount = 0
      try {
        const graphResp = await fetch('/api/v1/graph/statistics')
        if (graphResp.ok) {
          const graphData = await graphResp.json()
          graphEntityCount = graphData.data?.total_entities || 0
          relationCount = graphData.data?.total_relations || graphData.data?.total_edges || 0
        }
      } catch (e) {
        console.error('获取图谱统计失败:', e)
      }

      setStats({
        knowledgeBaseCount: collections.length,
        documentCount: totalDocs,
        graphEntityCount,
        relationCount,
      })
    } catch (error) {
      console.error('获取统计数据失败:', error)
    }
    setIsLoading(false)
  }

  const handleMmSearch = (e: React.FormEvent) => {
    e.preventDefault()
    if (!mmQuery.trim()) return
    mmSearch.mutate({
      query: mmQuery.trim(),
      chunk_types: ['image_description', 'table', 'table_description'],
      top_k: 6,
    })
  }

  /** 点击柱子下钻 */
  const handleBarClick = (data: any) => {
    if (!data?.activeLabel) return
    const name = data.activeLabel as string

    if (granularity === 'year') {
      // 点击年份 -> 下钻到月
      setSelectedDate(`${name}-01-01`)
      setGranularity('month')
    } else if (granularity === 'month') {
      // 点击月份 -> 下钻到日
      setSelectedDate(`${name}-01`)
      setGranularity('day')
    } else if (granularity === 'day') {
      // 点击日期 -> 下钻到小时
      setSelectedDate(name)
      setGranularity('hour')
    }
    // hour 粒度不再下钻
  }

  /** 返回上级粒度 */
  const handleDrillUp = () => {
    if (granularity === 'hour') {
      setGranularity('day')
      // selectedDate 如 "2026-07-05" -> 截取到月 "2026-07"
      setSelectedDate(selectedDate?.substring(0, 7))
    } else if (granularity === 'day') {
      setGranularity('month')
      // selectedDate 如 "2026-07" -> 截取到年 "2026"
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
      // "2026-07-05 14" -> "14:00"
      const parts = value.split(' ')
      return parts.length > 1 ? parts[1] + ':00' : value
    }
    if (granularity === 'day') {
      // "2026-07-05" -> "07-05"
      return value.substring(5)
    }
    if (granularity === 'month') {
      // "2026-07" -> "07月"
      return value.substring(5) + '月'
    }
    // year: "2026" -> "2026"
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

  // ========================================
  // 渲染函数
  // ========================================

  const renderMultimodalItem = (item: MultimodalItem, index: number) => {
    const Icon = multimodalIcons[item.chunk_type] || FileText
    const label = multimodalLabels[item.chunk_type] || item.chunk_type
    const colorClass = multimodalColors[item.chunk_type] || 'bg-gray-100 text-gray-700'

    return (
      <Card key={`${item.id}-${index}`} className="hover:shadow-md transition-shadow">
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 min-w-0">
              <div className={`p-1.5 rounded-md shrink-0 ${colorClass}`}>
                <Icon className="h-4 w-4" />
              </div>
              <CardTitle className="text-sm font-medium truncate">
                {item.source_file}
              </CardTitle>
            </div>
            <Badge variant="secondary" className="text-xs shrink-0 ml-2">
              {(item.score * 100).toFixed(1)}%
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="pt-0">
          <p className="text-sm text-muted-foreground line-clamp-3">
            {item.content}
          </p>
          <div className="flex items-center justify-between mt-2 text-xs text-muted-foreground">
            <span className={`px-1.5 py-0.5 rounded ${colorClass}`}>{label}</span>
            {item.page_number && <span>第 {item.page_number} 页</span>}
          </div>
        </CardContent>
      </Card>
    )
  }

  // ========================================
  // 静态数据
  // ========================================

  const statCards = [
    {
      title: '知识库总数',
      value: (stats?.knowledgeBaseCount ?? 0).toString(),
      icon: Database,
      gradient: 'from-blue-500 to-cyan-500',
      bgGradient: 'from-blue-500/10 to-cyan-500/10',
      iconBg: 'bg-blue-500/10',
      iconColor: 'text-blue-600 dark:text-blue-400',
      trend: '+12%',
      trendUp: true,
    },
    {
      title: '文档总数',
      value: (stats?.documentCount ?? 0).toString(),
      icon: FileText,
      gradient: 'from-emerald-500 to-teal-500',
      bgGradient: 'from-emerald-500/10 to-teal-500/10',
      iconBg: 'bg-emerald-500/10',
      iconColor: 'text-emerald-600 dark:text-emerald-400',
      trend: '+8%',
      trendUp: true,
    },
    {
      title: '图谱实体',
      value: (stats?.graphEntityCount ?? 0).toString(),
      icon: Network,
      gradient: 'from-violet-500 to-purple-500',
      bgGradient: 'from-violet-500/10 to-purple-500/10',
      iconBg: 'bg-violet-500/10',
      iconColor: 'text-violet-600 dark:text-violet-400',
      trend: '+24%',
      trendUp: true,
    },
    {
      title: '关系数量',
      value: (stats?.relationCount ?? 0).toString(),
      icon: TrendingUp,
      gradient: 'from-orange-500 to-amber-500',
      bgGradient: 'from-orange-500/10 to-amber-500/10',
      iconBg: 'bg-orange-500/10',
      iconColor: 'text-orange-600 dark:text-orange-400',
      trend: '+18%',
      trendUp: true,
    },
  ]

  const quickActions = [
    { title: '上传文档', description: '添加新的知识文档', href: '/documents', icon: FileText, gradient: 'from-blue-500 to-cyan-500', emoji: '📄' },
    { title: '知识图谱', description: '探索实体和关系网络', href: '/knowledge-graph', icon: Network, gradient: 'from-violet-500 to-purple-500', emoji: '🔗' },
    { title: '智能问答', description: '使用 GraphRAG 对话助手', href: '/chat', icon: MessageSquare, gradient: 'from-emerald-500 to-teal-500', emoji: '💬' },
    { title: '知识库管理', description: '配置和管理知识库', href: '/knowledge-bases', icon: Database, gradient: 'from-orange-500 to-amber-500', emoji: '📚' },
  ]

  // ========================================
  // 主渲染
  // ========================================

  return (
    <div className="space-y-6 animate-fade-in">
      {/* ========== 页面标题 ========== */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="p-2.5 rounded-xl bg-gradient-to-br from-blue-500/10 to-purple-500/10">
            <Sparkles className="h-6 w-6 text-blue-600 dark:text-blue-400" />
          </div>
          <div>
            <h1 className="text-3xl font-bold tracking-tight">首页概览</h1>
            <p className="text-muted-foreground mt-1 flex items-center gap-2">
              欢迎使用 KQuery Pro 企业级知识中台
              <span className="inline-block w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
              <span className="text-xs text-emerald-600 dark:text-emerald-400 font-medium">运行中</span>
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <div className="hidden sm:flex items-center gap-1 text-xs text-muted-foreground bg-muted/50 rounded-lg px-3 py-1.5">
            <Clock className="h-3.5 w-3.5" />
            {new Date().toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' })}
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={fetchStats}
            disabled={isLoading}
            className="gap-1.5"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isLoading ? 'animate-spin' : ''}`} />
            刷新
          </Button>
        </div>
      </div>

      {/* ========== 关键指标卡片 ========== */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {statCards.map((stat) => (
          <Card key={stat.title} className="relative overflow-hidden border-0 shadow-md hover:shadow-xl transition-all duration-300 group">
            {/* 渐变背景装饰 */}
            <div className={`absolute inset-0 bg-gradient-to-br ${stat.bgGradient} opacity-0 group-hover:opacity-100 transition-opacity duration-300`} />
            {/* 顶部彩色条 */}
            <div className={`absolute top-0 left-0 right-0 h-1 bg-gradient-to-r ${stat.gradient}`} />
            <CardContent className="p-6 relative">
              <div className="flex items-center justify-between mb-3">
                <div className={`p-2.5 rounded-xl ${stat.iconBg} ring-1 ring-black/5 dark:ring-white/5`}>
                  <stat.icon className={`h-5 w-5 ${stat.iconColor}`} />
                </div>
                <div className={`inline-flex items-center gap-0.5 text-xs font-medium px-2 py-1 rounded-full ${
                  stat.trendUp
                    ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400'
                    : 'bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-400'
                }`}>
                  {stat.trendUp ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
                  {stat.trend}
                </div>
              </div>
              <div>
                <p className="text-3xl font-bold tracking-tight">
                  {isLoading ? (
                    <span className="inline-block w-12 h-8 bg-muted animate-pulse rounded" />
                  ) : (
                    stat.value
                  )}
                </p>
                <p className="text-sm text-muted-foreground mt-1 font-medium">{stat.title}</p>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* ========== 快捷操作 ========== */}
      <div>
        <div className="flex items-center gap-2 mb-4">
          <h2 className="text-lg font-semibold">快捷操作</h2>
          <Badge variant="secondary" className="text-xs">快速入口</Badge>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          {quickActions.map((action) => (
            <Link
              key={action.title}
              to={action.href}
              className="group relative flex items-start gap-4 p-5 rounded-xl border bg-card hover:border-transparent hover:shadow-lg transition-all duration-300 overflow-hidden"
            >
              {/* hover 时渐变背景 */}
              <div className={`absolute inset-0 bg-gradient-to-br ${action.gradient} opacity-0 group-hover:opacity-5 transition-opacity duration-300`} />
              <div className={`relative p-2.5 rounded-lg bg-gradient-to-br ${action.gradient} bg-opacity-10`}>
                <action.icon className="h-5 w-5 text-white" />
              </div>
              <div className="relative">
                <h3 className="font-semibold group-hover:text-primary transition-colors">{action.title}</h3>
                <p className="text-sm text-muted-foreground mt-0.5">{action.description}</p>
                <div className="flex items-center gap-1 mt-2 text-xs text-primary/60 group-hover:text-primary transition-colors">
                  <span>立即前往</span>
                  <ArrowRight className="h-3 w-3 group-hover:translate-x-0.5 transition-transform" />
                </div>
              </div>
            </Link>
          ))}
        </div>
      </div>

      {/* ========== 数据可视化 ========== */}
      <div>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">数据可视化</h2>
          <Link
            to="/analytics"
            className="text-sm text-primary hover:underline inline-flex items-center gap-1"
          >
            查看全部分析
            <ArrowRight className="h-3.5 w-3.5" />
          </Link>
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* 饼图 */}
          <Card className="border-0 shadow-md">
            <CardHeader className="pb-2">
              <CardTitle className="text-base flex items-center gap-2">
                <div className="p-1.5 rounded-lg bg-blue-500/10">
                  <PieChartIcon className="h-4 w-4 text-blue-600 dark:text-blue-400" />
                </div>
                {pieChart?.title || '知识库分布'}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {pieChart?.data?.length > 0 ? (
                <ResponsiveContainer width="100%" height={260}>
                  <PieChart>
                    <Pie
                      data={pieChart.data}
                      cx="50%"
                      cy="50%"
                      innerRadius={60}
                      outerRadius={100}
                      paddingAngle={4}
                      dataKey="value"
                      nameKey="name"
                    >
                      {pieChart.data.map((_: ChartDatum, i: number) => (
                        <Cell key={i} fill={COLORS[i % COLORS.length]} />
                      ))}
                    </Pie>
                    <Tooltip
                      contentStyle={{
                        borderRadius: '12px',
                        border: 'none',
                        boxShadow: '0 4px 20px rgba(0,0,0,0.1)',
                      }}
                      formatter={(v: number, n: string) => [`${v}`, `${n}`]}
                    />
                    <Legend />
                  </PieChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex flex-col items-center justify-center h-[260px] text-muted-foreground">
                  <PieChartIcon className="h-10 w-10 mb-2 opacity-30" />
                  <p className="text-sm">暂无图表数据</p>
                </div>
              )}
            </CardContent>
          </Card>

          {/* 柱状图（支持粒度切换与下钻） */}
          <Card className="border-0 shadow-md">
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base flex items-center gap-2">
                  <div className="p-1.5 rounded-lg bg-emerald-500/10">
                    <BarChart3 className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
                  </div>
                  {queryStats?.title || '查询与文档统计'}
                </CardTitle>
                <div className="flex items-center gap-2">
                  {granularity !== 'month' && selectedDate && (
                    <Button variant="ghost" size="sm" className="h-8 text-xs" onClick={handleDrillUp}>
                      返回上级
                    </Button>
                  )}
                  <Select value={granularity} onValueChange={(v) => handleGranularityChange(v as any)}>
                    <SelectTrigger className="w-24 h-8 text-xs rounded-lg">
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
                <div className="flex items-center justify-center h-[260px] text-muted-foreground text-sm">
                  <Loader2 className="h-5 w-5 animate-spin mr-2" />
                  加载中...
                </div>
              ) : queryStats?.data?.length > 0 ? (
                <ResponsiveContainer width="100%" height={260}>
                  <BarChart data={queryStats.data} onClick={handleBarClick}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                    <XAxis dataKey="name" tickFormatter={formatAxisLabel} tick={{ fontSize: 12 }} />
                    <YAxis tick={{ fontSize: 12 }} />
                    <Tooltip
                      labelFormatter={formatTooltipLabel}
                      contentStyle={{
                        borderRadius: '12px',
                        border: 'none',
                        boxShadow: '0 4px 20px rgba(0,0,0,0.1)',
                      }}
                    />
                    <Legend />
                    <Bar dataKey="queries" name="查询次数" fill="#3b82f6" radius={[6, 6, 0, 0]} cursor="pointer" />
                    <Bar dataKey="documents" name="文档处理" fill="#10b981" radius={[6, 6, 0, 0]} cursor="pointer" />
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex flex-col items-center justify-center h-[260px] text-muted-foreground">
                  <BarChart3 className="h-10 w-10 mb-2 opacity-30" />
                  <p className="text-sm">暂无图表数据</p>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      {/* ========== 多模态内容预览 + 系统信息 ========== */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* 多模态内容预览（占 2 列） */}
        <Card className="lg:col-span-2 border-0 shadow-md">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base flex items-center gap-2">
                <div className="p-1.5 rounded-lg bg-purple-500/10">
                  <Eye className="h-4 w-4 text-purple-600 dark:text-purple-400" />
                </div>
                多模态内容
              </CardTitle>
              <Link
                to="/multimodal"
                className="text-sm text-primary hover:underline inline-flex items-center gap-1"
              >
                查看全部
                <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* 搜索栏 */}
            <form onSubmit={handleMmSearch} className="flex gap-2">
              <div className="relative flex-1">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <Input
                  placeholder="搜索多模态内容，例如：流程图、销售额..."
                  className="pl-10 h-9 text-sm"
                  value={mmQuery}
                  onChange={(e) => setMmQuery(e.target.value)}
                />
              </div>
              <Button type="submit" size="sm" disabled={isSearching || !mmQuery.trim()}>
                {isSearching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
              </Button>
            </form>

            {/* 内容卡片 */}
            {mmLoading ? (
              <div className="flex items-center justify-center py-12 text-muted-foreground">
                <Loader2 className="h-5 w-5 animate-spin mr-2" />
                加载中...
              </div>
            ) : mmItems.length > 0 ? (
              <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
                {mmItems.map(renderMultimodalItem)}
              </div>
            ) : (
              <div className="text-center py-12 text-muted-foreground border rounded-lg">
                <ImageIcon className="h-8 w-8 mx-auto mb-2 opacity-50" />
                <p className="text-sm">暂无可展示的多模态内容</p>
                <p className="text-xs mt-1">请先上传包含图片或表格的文档并建立索引</p>
              </div>
            )}
          </CardContent>
        </Card>

        {/* 系统信息 */}
        <Card className="border-0 shadow-md">
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2">
              <div className="p-1.5 rounded-lg bg-primary/10">
                <Activity className="h-4 w-4 text-primary" />
              </div>
              系统信息
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-1">
              {[
                { label: '版本', value: 'v3.0.0', type: 'text' },
                { label: 'Milvus', value: '已连接', type: 'badge' },
                { label: 'Neo4j', value: '已连接', type: 'badge' },
                { label: '知识库', value: `${stats?.knowledgeBaseCount ?? 0} 个`, type: 'text' },
                { label: '文档总数', value: `${stats?.documentCount ?? 0} 个`, type: 'text' },
                { label: '图谱实体', value: `${stats?.graphEntityCount ?? 0} 个`, type: 'text' },
                { label: '关系数量', value: `${stats?.relationCount ?? 0} 条`, type: 'text' },
              ].map((item) => (
                <div key={item.label} className="flex items-center justify-between py-2.5 px-3 rounded-lg hover:bg-muted/50 transition-colors">
                  <span className="text-sm text-muted-foreground">{item.label}</span>
                  {item.type === 'badge' ? (
                    <span className="inline-flex items-center gap-1.5 text-xs font-medium text-emerald-600 dark:text-emerald-400">
                      <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
                      {item.value}
                    </span>
                  ) : (
                    <span className="text-sm font-semibold tabular-nums">{item.value}</span>
                  )}
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
