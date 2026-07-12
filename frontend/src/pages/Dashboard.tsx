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
      color: 'text-blue-600 dark:text-blue-400',
      bgColor: 'bg-blue-100 dark:bg-blue-900/30'
    },
    {
      title: '文档总数',
      value: (stats?.documentCount ?? 0).toString(),
      icon: FileText,
      color: 'text-green-600 dark:text-green-400',
      bgColor: 'bg-green-100 dark:bg-green-900/30'
    },
    {
      title: '图谱实体',
      value: (stats?.graphEntityCount ?? 0).toString(),
      icon: Network,
      color: 'text-purple-600 dark:text-purple-400',
      bgColor: 'bg-purple-100 dark:bg-purple-900/30'
    },
    {
      title: '关系数量',
      value: (stats?.relationCount ?? 0).toString(),
      icon: TrendingUp,
      color: 'text-orange-600 dark:text-orange-400',
      bgColor: 'bg-orange-100 dark:bg-orange-900/30'
    },
  ]

  const quickActions = [
    { title: '上传文档', description: '添加新的知识文档', href: '/documents', icon: FileText },
    { title: '浏览知识图谱', description: '探索实体和关系网络', href: '/knowledge-graph', icon: Network },
    { title: '智能问答', description: '使用GraphRAG对话助手', href: '/chat', icon: MessageSquare },
    { title: '管理知识库', description: '配置和管理知识库', href: '/knowledge-bases', icon: Database },
  ]

  // ========================================
  // 主渲染
  // ========================================

  return (
    <div className="space-y-6 animate-fade-in">
      {/* ========== 页面标题 ========== */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">首页概览</h1>
          <p className="text-muted-foreground mt-1">
            欢迎使用 KQuery Pro 企业级知识中台
          </p>
        </div>
        <div className="flex items-center gap-2">
          {isLoading ? (
            <Badge variant="secondary">加载中...</Badge>
          ) : (
            <Badge variant="success" className="text-sm py-1 px-3">
              系统运行正常
            </Badge>
          )}
          <button
            onClick={fetchStats}
            className="text-sm text-muted-foreground hover:text-foreground"
          >
            刷新
          </button>
        </div>
      </div>

      {/* ========== 关键指标卡片 ========== */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {statCards.map((stat) => (
          <Card key={stat.title} className="hover:shadow-lg transition-shadow">
            <CardContent className="p-6">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium text-muted-foreground">{stat.title}</p>
                  <p className="text-2xl font-bold mt-2">
                    {isLoading ? '-' : stat.value}
                  </p>
                </div>
                <div className={`p-3 rounded-full ${stat.bgColor}`}>
                  <stat.icon className={`h-6 w-6 ${stat.color}`} />
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* ========== 快捷操作 ========== */}
      <div>
        <h2 className="text-lg font-semibold mb-4">快捷操作</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          {quickActions.map((action) => (
            <Link
              key={action.title}
              to={action.href}
              className="flex items-start gap-4 p-4 rounded-lg border bg-card hover:bg-accent transition-colors"
            >
              <div className="p-2 rounded-lg bg-primary/10">
                <action.icon className="h-5 w-5 text-primary" />
              </div>
              <div>
                <h3 className="font-medium">{action.title}</h3>
                <p className="text-sm text-muted-foreground">{action.description}</p>
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
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base flex items-center gap-2">
                <PieChartIcon className="h-4 w-4 text-primary" />
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
                    <Tooltip formatter={(v: number, n: string) => [`${v}`, `${n}`]} />
                    <Legend />
                  </PieChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex items-center justify-center h-[260px] text-muted-foreground text-sm">
                  暂无图表数据
                </div>
              )}
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
                <div className="flex items-center justify-center h-[260px] text-muted-foreground text-sm">
                  <Loader2 className="h-5 w-5 animate-spin mr-2" />
                  加载中...
                </div>
              ) : queryStats?.data?.length > 0 ? (
                <ResponsiveContainer width="100%" height={260}>
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
                <div className="flex items-center justify-center h-[260px] text-muted-foreground text-sm">
                  暂无图表数据
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      {/* ========== 多模态内容预览 + 系统信息 ========== */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* 多模态内容预览（占 2 列） */}
        <Card className="lg:col-span-2">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base flex items-center gap-2">
                <Eye className="h-4 w-4 text-primary" />
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
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <Activity className="h-4 w-4 text-primary" />
              系统信息
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              <div className="flex justify-between py-1">
                <span className="text-muted-foreground">版本</span>
                <span className="font-medium">v2.0.0</span>
              </div>
              <div className="flex justify-between py-1">
                <span className="text-muted-foreground">Milvus</span>
                <Badge variant="success" className="text-xs">已连接</Badge>
              </div>
              <div className="flex justify-between py-1">
                <span className="text-muted-foreground">Neo4j</span>
                <Badge variant="success" className="text-xs">已连接</Badge>
              </div>
              <div className="flex justify-between py-1">
                <span className="text-muted-foreground">知识库</span>
                <span className="font-medium">{stats?.knowledgeBaseCount ?? 0} 个</span>
              </div>
              <div className="flex justify-between py-1">
                <span className="text-muted-foreground">文档总数</span>
                <span className="font-medium">{stats?.documentCount ?? 0} 个</span>
              </div>
              <div className="flex justify-between py-1">
                <span className="text-muted-foreground">图谱实体</span>
                <span className="font-medium">{stats?.graphEntityCount ?? 0} 个</span>
              </div>
              <div className="flex justify-between py-1">
                <span className="text-muted-foreground">关系数量</span>
                <span className="font-medium">{stats?.relationCount ?? 0} 条</span>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
