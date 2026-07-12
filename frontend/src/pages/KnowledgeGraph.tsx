import { useEffect, useRef, useState, useCallback, useMemo } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
import {
  Search,
  ZoomIn,
  ZoomOut,
  Maximize2,
  Minimize2,
  LayoutGrid,
  Network,
  GitBranch,
  Target,
  RotateCcw,
  Download,
  Loader2,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/select'
import { useGraphStore, selectHasGraphData, selectNodeCount, selectEdgeCount } from '@/stores/useGraphStore'
import type { GraphEntityNode, GraphRelationEdge } from '@/stores/useGraphStore'

interface FGNode {
  id: string
  name: string
  val: number
  color: string
  entityType: string
  nodeData: GraphEntityNode
  fx?: number
  fy?: number
}

interface FGLink {
  source: string
  target: string
  label: string
}

interface FGGraphData {
  nodes: FGNode[]
  links: FGLink[]
}

export function KnowledgeGraph() {
  const fgRef = useRef<any>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  const {
    graphData,
    isLoading,
    isSearching,
    searchResults,
    selectedEntity,
    currentLayout,
    showLabels,
    isFullscreen,

    fetchVisualizationData,
    searchEntities,
    setSelectedEntity,
    setCurrentLayout,
    setZoomLevel,
    toggleFullscreen,
    toggleShowLabels,
    clearSelection,
    resetView,
    expandNode,
    isExpanding,
    fetchEntityDetail,
  } = useGraphStore()

  const [searchQuery, setSearchQuery] = useState('')
  const [showSearchResults, setShowSearchResults] = useState(false)
  const [hoveredNode, setHoveredNode] = useState<FGNode | null>(null)
  const [fgDimensions, setFgDimensions] = useState({ width: 800, height: 600 })

  // Initialize: load default KG data only if needed
  useEffect(() => {
    const saved = localStorage.getItem('graph-store')
    if (saved) {
      try {
        const state = JSON.parse(saved)
        if (state?.currentLayout) {
          setCurrentLayout(state.currentLayout)
        }
      } catch { /* ignore */ }
    }

    // 如果已有图谱数据，不强制刷新；否则尝试加载一个高频实体作为默认中心
    if (graphData && graphData.nodes.length > 0) return

    const loadDefaultCenter = async () => {
      try {
        const res = await fetch('/api/v1/graph/statistics')
        const data = await res.json()
        if (data.success && data.data?.top_entities?.length > 0) {
          const topEntity = data.data.top_entities[0]
          if (topEntity?.id) {
            fetchVisualizationData(topEntity.id, { radius: 1 })
            return
          }
        }
        // 没有任何实体时，保持空状态，由空状态引导用户
      } catch {
        // 静默失败，空状态即可
      }
    }

    loadDefaultCenter()
  }, [])

  // Update dimensions on resize
  useEffect(() => {
    const updateSize = () => {
      if (containerRef.current) {
        const rect = containerRef.current.getBoundingClientRect()
        setFgDimensions({ width: rect.width, height: rect.height })
      }
    }
    updateSize()
    window.addEventListener('resize', updateSize)
    return () => window.removeEventListener('resize', updateSize)
  }, [isFullscreen])

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!searchQuery.trim()) return
    try {
      await searchEntities(searchQuery.trim())
      setShowSearchResults(true)
    } catch {
      setShowSearchResults(false)
    }
  }

  // ─── 布局位置计算 ───────────────────────────────────
  const computeLayoutPositions = useCallback((
    nodes: GraphEntityNode[],
    width: number,
    height: number,
  ): Record<string, { fx: number; fy: number }> => {
    const positions: Record<string, { fx: number; fy: number }> = {}
    const cx = width / 2
    const cy = height / 2
    const padding = 80
    const n = nodes.length
    if (n === 0) return positions

    switch (currentLayout) {
      case 'circular': {
        const radius = Math.min(width, height) / 2 - padding
        nodes.forEach((node, i) => {
          const angle = (2 * Math.PI * i) / n - Math.PI / 2
          positions[node.id] = { fx: cx + radius * Math.cos(angle), fy: cy + radius * Math.sin(angle) }
        })
        return positions
      }

      case 'grid': {
        const cols = Math.ceil(Math.sqrt(n))
        const rows = Math.ceil(n / cols)
        const cellW = Math.max(60, (width - 2 * padding) / cols)
        const cellH = Math.max(60, (height - 2 * padding) / rows)
        nodes.forEach((node, i) => {
          const col = i % cols
          const row = Math.floor(i / cols)
          positions[node.id] = {
            fx: padding + col * cellW + cellW / 2,
            fy: padding + row * cellH + cellH / 2,
          }
        })
        return positions
      }

      case 'hierarchical': {
        const typeOrder = ['person', 'organization', 'product', 'technology', 'concept', 'location', 'event', 'other']
        const grouped: Record<string, GraphEntityNode[]> = {}
        nodes.forEach((node) => {
          const t = (node.type || 'other').toLowerCase()
          if (!grouped[t]) grouped[t] = []
          grouped[t].push(node)
        })
        const levels = Object.keys(grouped).sort(
          (a, b) => typeOrder.indexOf(a) - typeOrder.indexOf(b),
        )
        const levelH = Math.max(100, (height - 2 * padding) / Math.max(levels.length, 1))
        levels.forEach((type, li) => {
          const levelNodes = grouped[type]
          const levelW = Math.max(80, (width - 2 * padding) / levelNodes.length)
          levelNodes.forEach((node, i) => {
            positions[node.id] = {
              fx: padding + i * levelW + levelW / 2,
              fy: padding + li * levelH + levelH / 2,
            }
          })
        })
        return positions
      }

      default:
        return positions // force layout: no pinning
    }
  }, [currentLayout])

  // ─── 数据转换（带布局计算）─────────────────────────
  const fgData: FGGraphData | null = useMemo(() => {
    if (!graphData || graphData.nodes.length === 0) return null

    const nodes: FGNode[] = graphData.nodes.map((n: GraphEntityNode) => ({
      id: n.id,
      name: n.label,
      val: Math.max((n.size || 15) / 12, 1),
      color: n.color || '#73c0de',
      entityType: n.type,
      nodeData: n,
    }))

    const links: FGLink[] = graphData.edges.map((e: GraphRelationEdge) => ({
      source: e.source,
      target: e.target,
      label: e.label,
    }))

    // 非力导向布局：冻结节点位置
    if (currentLayout !== 'force') {
      const pos = computeLayoutPositions(graphData.nodes, fgDimensions.width, fgDimensions.height)
      nodes.forEach((node) => {
        if (pos[node.id]) {
          node.fx = pos[node.id].fx
          node.fy = pos[node.id].fy
        }
      })
    }

    return { nodes, links }
  }, [graphData, currentLayout, fgDimensions, computeLayoutPositions])

  const handleSelectEntity = (entityId: string) => {
    fetchVisualizationData(entityId, { radius: 1 })
    setShowSearchResults(false)
    setSearchQuery('')
  }

  const handleNodeClick = useCallback((node: FGNode) => {
    // 打开详情面板
    setSelectedEntity(node.nodeData)
    fetchEntityDetail(node.id)
    // 展开该节点的邻居（加载更多节点到图中）
    expandNode(node.id, 1)
  }, [setSelectedEntity, expandNode, fetchEntityDetail])

  const handleNodeHover = useCallback((node: FGNode | null) => {
    setHoveredNode(node)
  }, [])

  const handleZoomIn = () => {
    if (fgRef.current) {
      const d = fgRef.current.zoom()
      fgRef.current.zoom(d * 1.3, 400)
    }
  }

  const handleZoomOut = () => {
    if (fgRef.current) {
      const d = fgRef.current.zoom()
      fgRef.current.zoom(d / 1.3, 400)
    }
  }

  const handleResetView = () => {
    if (fgRef.current) {
      fgRef.current.zoomToFit(400)
    }
  }

  const handleExport = () => {
    if (!fgData) return
    const canvas = document.querySelector('canvas')
    if (canvas) {
      const link = document.createElement('a')
      link.download = 'knowledge-graph.png'
      link.href = canvas.toDataURL('image/png')
      link.click()
    }
  }

  const handleQuickSearch = async (keyword: string) => {
    setSearchQuery(keyword)
    try {
      await searchEntities(keyword)
      const results = useGraphStore.getState().searchResults
      if (results.length > 0) {
        // 直接加载第一个结果的图谱
        handleSelectEntity(results[0].entity_id)
      } else {
        setShowSearchResults(true)
      }
    } catch {
      setShowSearchResults(false)
    }
  }

  const hasResults = searchResults.length > 0
  const showEmptyResults = showSearchResults && !!searchQuery.trim() && !isSearching && !hasResults

  const layoutOptions = [
    { value: 'force', label: '力导向', icon: Network },
    { value: 'hierarchical', label: '层级', icon: GitBranch },
    { value: 'circular', label: '圆形', icon: Target },
    { value: 'grid', label: '网格', icon: LayoutGrid },
  ]

  return (
    <div className="space-y-4 animate-fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight flex items-center gap-3">
            知识图谱探索
            <Badge variant="info">GraphRAG 核心</Badge>
          </h1>
          <p className="text-muted-foreground mt-1">
            可视化浏览和分析知识图谱中的实体和关系网络
          </p>
        </div>
        <div className="flex gap-3 text-sm text-muted-foreground">
          <span>节点: <strong className="text-foreground">{selectNodeCount(useGraphStore.getState())}</strong></span>
          <span>边: <strong className="text-foreground">{selectEdgeCount(useGraphStore.getState())}</strong></span>
        </div>
      </div>

      <form onSubmit={handleSearch} className="relative max-w-md" onBlur={() => setTimeout(() => setShowSearchResults(false), 200)}>
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
        <Input
          type="search"
          placeholder="搜索实体（人名、组织、技术等）..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="pl-9 pr-10"
        />
        {isSearching && (
          <Loader2 className="absolute right-3 top-1/2 -translate-y-1/2 h-4 w-4 animate-spin text-muted-foreground" />
        )}
        {showSearchResults && searchResults.length > 0 && (
          <div className="absolute top-full left-0 right-0 mt-1 bg-background border rounded-lg shadow-lg z-50 max-h-64 overflow-auto">
            {searchResults.map((entity) => (
              <button
                key={entity.entity_id}
                onClick={() => handleSelectEntity(entity.entity_id)}
                className="w-full px-3 py-2 text-left hover:bg-accent transition-colors border-b last:border-b-0"
              >
                <div className="font-medium">{entity.name}</div>
                <div className="text-xs text-muted-foreground">{entity.type}</div>
              </button>
            ))}
          </div>
        )}
        {showEmptyResults && (
          <div className="absolute top-full left-0 right-0 mt-1 bg-background border rounded-lg shadow-lg z-50 p-4 text-center text-sm text-muted-foreground">
            未找到与 "{searchQuery}" 相关的实体
          </div>
        )}
      </form>

      <Card className="overflow-hidden">
        <div className="flex items-center justify-between p-3 border-b bg-muted/30">
          <div className="flex items-center gap-2">
            <Select value={currentLayout} onValueChange={setCurrentLayout}>
              <SelectTrigger className="w-[140px] h-8 text-sm">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {layoutOptions.map((layout) => (
                  <SelectItem key={layout.value} value={layout.value}>
                    <span className="flex items-center gap-2">
                      <layout.icon className="h-4 w-4" />
                      {layout.label}
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <div className="w-px h-6 bg-border mx-2" />
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon" className="h-8 w-8" onClick={handleZoomIn}>
                  <ZoomIn className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>放大</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon" className="h-8 w-8" onClick={handleZoomOut}>
                  <ZoomOut className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>缩小</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon" className="h-8 w-8" onClick={handleResetView}>
                  <RotateCcw className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>重置视图</TooltipContent>
            </Tooltip>
          </div>
          <div className="flex items-center gap-2">
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant={showLabels ? "default" : "outline"}
                  size="sm"
                  onClick={toggleShowLabels}
                  className="h-8"
                >
                  标签
                </Button>
              </TooltipTrigger>
              <TooltipContent>{showLabels ? '隐藏标签' : '显示标签'}</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon" className="h-8 w-8" onClick={toggleFullscreen}>
                  {isFullscreen ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
                </Button>
              </TooltipTrigger>
              <TooltipContent>{isFullscreen ? '退出全屏' : '全屏'}</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon" className="h-8 w-8" onClick={handleExport} disabled={!fgData}>
                  <Download className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>导出图片</TooltipContent>
            </Tooltip>
          </div>
        </div>

        <div
          ref={containerRef}
          className={`relative bg-gradient-to-br from-background to-muted/20 ${isFullscreen ? 'fixed inset-0 z-50' : 'h-[600px]'}`}
        >
          {isLoading && (
            <div className="absolute inset-0 flex items-center justify-center bg-background/80 backdrop-blur-sm z-10">
              <div className="text-center space-y-3">
                <Loader2 className="h-10 w-10 animate-spin text-primary mx-auto" />
                <p className="text-sm font-medium">正在加载知识图谱...</p>
              </div>
            </div>
          )}

          {!isLoading && !fgData && (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="text-center space-y-4 max-w-md px-6">
                <div className="p-4 rounded-full bg-primary/10 inline-block">
                  <Network className="h-12 w-12 text-primary" />
                </div>
                <div>
                  <h3 className="text-lg font-semibold">开始探索知识图谱</h3>
                  <p className="text-sm text-muted-foreground mt-2">
                    在上方搜索框输入实体名称（如公司名称、人名、技术术语等），
                    即可查看相关的知识子图。
                  </p>
                </div>
                <div className="flex flex-wrap justify-center gap-2 pt-2">
                  {['腾讯公司', '人工智能', '深度学习'].map((keyword) => (
                    <Button
                      key={keyword}
                      variant="outline"
                      size="sm"
                      onClick={() => handleQuickSearch(keyword)}
                    >
                      {keyword}
                    </Button>
                  ))}
                </div>
              </div>
            </div>
          )}

          {fgData && (
            <ForceGraph2D
              ref={fgRef}
              graphData={fgData}
              width={fgDimensions.width}
              height={fgDimensions.height}
              nodeLabel={showLabels ? 'name' : ''}
              nodeColor="color"
              nodeVal="val"
              linkLabel={showLabels ? 'label' : ''}
              linkColor={() => '#94a3b8'}
              linkWidth={0.5}
              linkDirectionalArrowLength={2}
              linkDirectionalArrowRelPos={1}
              linkCanvasObjectMode={() => 'after'}
              linkCanvasObject={(link: any, ctx: CanvasRenderingContext2D) => {
                if (!showLabels) return
                const text = link.label
                const start = link.source
                const end = link.target
                if (!start || !end || typeof start === 'string' || typeof end === 'string') return
                const midX = (start.x + end.x) / 2
                const midY = (start.y + end.y) / 2
                ctx.font = '8px sans-serif'
                ctx.fillStyle = '#94a3b8'
                ctx.textAlign = 'center'
                ctx.fillText(text, midX, midY - 4)
              }}
              nodeCanvasObject={(node: any, ctx: CanvasRenderingContext2D) => {
                const label = node.name
                const size = Math.max(node.val * 2, 3)
                // 主圆
                ctx.beginPath()
                ctx.arc(node.x, node.y, size, 0, 2 * Math.PI)
                ctx.fillStyle = node.color
                ctx.fill()
                ctx.strokeStyle = 'rgba(255,255,255,0.4)'
                ctx.lineWidth = 1
                ctx.stroke()
                // 可点击展开指示：小圆环
                ctx.beginPath()
                ctx.arc(node.x, node.y, size + 3, 0, 2 * Math.PI)
                ctx.strokeStyle = 'rgba(100,116,139,0.3)'
                ctx.lineWidth = 1
                ctx.stroke()
                if (showLabels) {
                  ctx.font = '9px Inter, sans-serif'
                  ctx.fillStyle = '#64748b'
                  ctx.textAlign = 'center'
                  ctx.fillText(label, node.x, node.y + size + 12)
                }
              }}
              onNodeClick={handleNodeClick}
              onNodeHover={handleNodeHover}
              // ─── 力模拟参数调优 ─────────────────────
              d3AlphaDecay={0.03}          // 更快的冷却速度，减少抖动
              d3VelocityDecay={0.35}        // 更高的速度衰减，抑制震荡
              warmupTicks={200}             // 预计算轮数，初始布局更稳定
              cooldownTicks={0}             // 不自动降温，保持持续稳定
              cooldownTime={0}              // 禁用自动冷却
              enableNodeDrag={true}
              enableZoomInteraction={true}
              enablePanInteraction={true}
            />
          )}

          {/* Hovered node tooltip */}
          {hoveredNode && (
            <div className="absolute top-4 right-4 bg-background/90 backdrop-blur-sm rounded-lg px-3 py-2 shadow-lg text-xs z-20">
              <div className="font-semibold">{hoveredNode.name}</div>
              <div className="text-muted-foreground mt-0.5">{hoveredNode.entityType}</div>
            </div>
          )}

          {/* Graph info overlay */}
          {fgData && (
            <div className="absolute bottom-4 left-4 bg-background/90 backdrop-blur-sm rounded-lg p-3 shadow-lg text-xs space-y-1 z-20">
              <div className="font-semibold mb-1">当前视图信息</div>
              <div>节点: {graphData!.statistics.nodeCount}</div>
              <div>边: {graphData!.statistics.edgeCount}</div>
              <div>布局: {currentLayout}</div>
              {isExpanding && <div className="text-primary animate-pulse">展开中...</div>}
              <div className="text-muted-foreground pt-1">点击节点可展开更多邻居</div>
              {graphData!.statistics.centerEntityId && (
                <div>中心: {graphData!.statistics.centerEntityId}</div>
              )}
            </div>
          )}
        </div>
      </Card>

      {selectedEntity && (
        <Card className="animate-slide-up-enter fixed right-0 top-[5rem] bottom-0 w-80 z-40 rounded-none border-l shadow-xl">
          <CardContent className="p-4 h-full overflow-auto">
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold">实体详情</h3>
                <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setSelectedEntity(null)}>
                  ✕
                </Button>
              </div>
              <div className="space-y-3">
                <div>
                  <label className="text-xs font-medium text-muted-foreground">名称</label>
                  <p className="font-semibold mt-1">{selectedEntity.label}</p>
                </div>
                <div>
                  <label className="text-xs font-medium text-muted-foreground">类型</label>
                  <Badge variant="info" className="mt-1">{selectedEntity.type}</Badge>
                </div>
                {selectedEntity.properties && Object.keys(selectedEntity.properties).length > 0 && (
                  <div>
                    <label className="text-xs font-medium text-muted-foreground block mb-2">属性</label>
                    <div className="bg-muted/50 rounded-lg p-3 space-y-2 text-sm max-h-[400px] overflow-auto">
                      {Object.entries(selectedEntity.properties).map(([key, value]) => (
                        <div key={key} className="flex justify-between gap-2">
                          <span className="text-muted-foreground truncate">{key}</span>
                          <span className="text-right break-all font-mono text-xs">{String(value)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

export default KnowledgeGraph
