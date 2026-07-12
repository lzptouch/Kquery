import { create } from 'zustand'
import { persist } from 'zustand/middleware'

// ========================================
// 类型定义
// ========================================

export interface GraphEntityNode {
  id: string
  label: string
  type: string
  properties: Record<string, any>
  size?: number
  color?: string
  shape?: string
  icon?: string
  x?: number
  y?: number
}

export interface GraphRelationEdge {
  id: string
  source: string
  target: string
  label: string
  type: string
  properties: Record<string, any>
  width?: number
  color?: string
  style?: 'solid' | 'dashed' | 'dotted'
  weight?: number
}

export interface VisualizationData {
  nodes: GraphEntityNode[]
  edges: GraphRelationEdge[]
  layout: string
  statistics: {
    nodeCount: number
    edgeCount: number
    centerEntityId?: string
    radius?: number
  }
  options?: {
    nodeSizeRange?: [number, number]
    edgeWidthRange?: [number, number]
    showLabels?: boolean
    enablePhysics?: boolean
  }
}

export interface EntitySearchResult {
  entity_id: string
  name: string
  type: string
  score?: number
  properties: Record<string, any>
}

interface GraphState {
  selectedEntity: GraphEntityNode | null
  graphData: VisualizationData | null
  searchResults: EntitySearchResult[]
  highlightedPath: GraphEntityNode[]
  selectedNodes: Set<string>
  isLoading: boolean
  isSearching: boolean
  isExpanding: boolean
  error: string | null
  currentLayout: string
  zoomLevel: number
  isFullscreen: boolean
  showLabels: boolean
  showEdgeLabels: boolean
  viewHistory: VisualizationData[]

  fetchVisualizationData: (centerEntityId: string, params?: { radius?: number; max_nodes?: number; format?: string }) => Promise<void>
  fetchEntityDetail: (entityId: string) => Promise<void>
  fetchNeighbors: (entityId: string, depth?: number) => Promise<GraphEntity[]>
  searchEntities: (keyword: string, limit?: number) => Promise<void>
  expandNode: (entityId: string, radius?: number) => Promise<void>

  setSelectedEntity: (entity: GraphEntityNode | null) => void
  toggleNodeSelection: (nodeId: string) => void
  clearSelection: () => void

  setHighlightedPath: (path: GraphEntityNode[]) => void
  setCurrentLayout: (layout: string) => void
  setZoomLevel: (level: number) => void
  toggleFullscreen: () => void
  toggleShowLabels: () => void
  toggleShowEdgeLabels: () => void

  resetView: () => void
  undo: () => void
  redo: () => void
  clearError: () => void
}

interface GraphEntity {
  entity_id: string
  name: string
  type: string
  relation_type: string
  distance: number
}

interface EntityDetailResponse {
  entity: {
    id: string
    name: string
    type: string
    properties: Record<string, any>
  }
  neighbors?: {
    total_count: number
    by_distance: Record<string, GraphEntity[]>
    by_relation_type: Record<string, GraphEntity[]>
  }
}

interface SubgraphResponse {
  statistics: {
    total_nodes: number
    total_edges: number
    center_count: number
    query_time_ms: number
  }
  nodes: Array<{
    id: string
    name: string
    type: string
    isCenter?: boolean
    mention_count?: number
  }>
  edges: Array<{
    source: string
    target: string
    type: string
    weight?: number
  }>
  visualization?: {
    format_version: string
    layout: string
    nodes: Array<{
      id: string
      label: string
      group: string
      value: number
      isCenter?: boolean
      size: number
      color: string
    }>
    links: Array<{
      source: string
      target: string
      type: string
      weight: number
      label: string
      width: number
      style: 'solid' | 'dashed'
    }>
  }
}

const API_BASE_URL = '' // 通过 Vite 代理

async function apiRequest<T>(endpoint: string, options?: RequestInit): Promise<T> {
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
  }
  
  const response = await fetch(`/api/v1/graph${endpoint}`, {
    ...options,
    headers: {
      ...headers,
      ...(options?.headers as Record<string, string>),
    },
  })
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Request failed' }))
    throw new Error(error.detail || `API request failed: ${response.status}`)
  }
  
  const result = await response.json()
  if (!result.success) {
    throw new Error(result.error || result.message || 'Request failed')
  }
  return result.data as T
}

const entityColorMap: Record<string, string> = {
  'person': '#5470c6',
  'organization': '#91cc75',
  'location': '#fac858',
  'event': '#ee6666',
  'concept': '#73c0de',
  'product': '#3ba272',
  'technology': '#fc8452',
  'unknown': '#9a60b4',
}

const initialState = {
  selectedEntity: null as GraphEntityNode | null,
  graphData: null as VisualizationData | null,
  searchResults: [] as EntitySearchResult[],
  highlightedPath: [] as GraphEntityNode[],
  selectedNodes: new Set<string>(),
  isLoading: false,
  isSearching: false,
  isExpanding: false,
  error: null as string | null,
  currentLayout: 'force',
  zoomLevel: 50,
  isFullscreen: false,
  showLabels: true,
  showEdgeLabels: true,
  viewHistory: [] as VisualizationData[],
}

export const useGraphStore = create<GraphState>()(
  persist(
    (set, get) => ({
      ...initialState,

      fetchVisualizationData: async (centerEntityId, params) => {
        set({ isLoading: true, error: null })

        try {
          const { radius = 2, max_nodes = 200 } = params || {}

          const response = await apiRequest<SubgraphResponse>('/subgraph', {
            method: 'POST',
            body: JSON.stringify({
              center_ids: [centerEntityId],
              radius,
              max_nodes,
            }),
          })

          const nodes: GraphEntityNode[] = response.nodes.map((n) => ({
            id: n.id,
            label: n.name,
            type: n.type,
            properties: {},
            size: 20 + Math.min((n.mention_count || 0) * 2, 40),
            color: entityColorMap[n.type?.toLowerCase()] || entityColorMap.unknown,
            ...(n.isCenter && { isCenter: true }),
          }))

          const edges: GraphRelationEdge[] = response.edges.map((e, i) => ({
            id: `edge_${i}`,
            source: e.source,
            target: e.target,
            label: e.type,
            type: e.type,
            properties: {},
            width: 1 + Math.min(e.weight || 1, 5),
            color: '#999',
            style: (e.weight || 1) > 2 ? 'solid' : 'dashed',
            weight: e.weight || 1,
          }))

          const mockData: VisualizationData = {
            nodes,
            edges,
            layout: get().currentLayout,
            statistics: {
              nodeCount: response.statistics.total_nodes,
              edgeCount: response.statistics.total_edges,
              centerEntityId,
              radius,
            },
            options: {
              showLabels: get().showLabels,
              enablePhysics: true,
            },
          }

          if (get().graphData) {
            set((state) => ({
              viewHistory: [...state.viewHistory.slice(-9), state.graphData!].slice(-10),
            }))
          }

          set({
            graphData: mockData,
            isLoading: false,
          })
        } catch (error: any) {
          set({
            isLoading: false,
            error: error.message || '获取图谱数据失败',
          })
        }
      },

      fetchEntityDetail: async (entityId) => {
        try {
          const response = await apiRequest<EntityDetailResponse>(`/entities/${entityId}?include_neighbors=true&neighbor_depth=1`)

          const entity: GraphEntityNode = {
            id: response.entity.id,
            label: response.entity.name,
            type: response.entity.type,
            properties: response.entity.properties || {},
          }

          set({ selectedEntity: entity })
        } catch (error: any) {
          set({ error: error.message || '获取实体详情失败' })
        }
      },

      fetchNeighbors: async (entityId, depth = 1) => {
        set({ isExpanding: true, error: null })

        try {
          const response = await apiRequest<{
            center_entity_id: string
            total_neighbors: number
            neighbors: Array<{
              node_id: string
              node_name: string
              node_type: string
              relation_type: string
              distance: number
            }>
          }>(`/entities/${entityId}/neighbors?depth=${depth}`)

          const neighbors: GraphEntity[] = (response.neighbors || []).map((n) => ({
            entity_id: n.node_id,
            name: n.node_name,
            type: n.node_type,
            relation_type: n.relation_type,
            distance: n.distance,
          }))

          set({ isExpanding: false })
          return neighbors
        } catch (error: any) {
          set({
            isExpanding: false,
            error: error.message || '获取邻居节点失败',
          })
          throw error
        }
      },

      searchEntities: async (keyword, limit = 20) => {
        set({ isSearching: true, error: null })

        try {
          const response = await apiRequest<{
            items: Array<{
              id: string
              name: string
              type: string
              properties: Record<string, any>
            }>
            total: number
          }>(`/entities?keyword=${encodeURIComponent(keyword)}&limit=${limit}`)

          const results: EntitySearchResult[] = (response.items || []).map((e) => ({
            entity_id: e.id,
            name: e.name,
            type: e.type,
            properties: e.properties || {},
          }))

          set({
            searchResults: results,
            isSearching: false,
          })
        } catch (error: any) {
          set({
            isSearching: false,
            error: error.message || '搜索实体失败',
          })
        }
      },

      expandNode: async (entityId, radius = 1) => {
        const neighbors = await get().fetchNeighbors(entityId, radius)

        if (!neighbors.length) return

        set((state) => {
          if (!state.graphData) return state

          const existingIds = new Set(state.graphData.nodes.map((n) => n.id))
          const newNodes: GraphEntityNode[] = []

          neighbors.forEach((n) => {
            if (!existingIds.has(n.entity_id)) {
              newNodes.push({
                id: n.entity_id,
                label: n.name,
                type: n.type,
                properties: {},
                color: entityColorMap[n.type?.toLowerCase()] || entityColorMap.unknown,
              })
            }
          })

          const newEdges: GraphRelationEdge[] = neighbors.map((n, i) => ({
            id: `edge_new_${i}`,
            source: entityId,
            target: n.entity_id,
            label: n.relation_type,
            type: n.relation_type,
            properties: {},
            style: 'dashed',
          }))

          return {
            graphData: {
              ...state.graphData,
              nodes: [...state.graphData.nodes, ...newNodes],
              edges: [...state.graphData.edges, ...newEdges],
              statistics: {
                ...state.graphData.statistics,
                nodeCount: state.graphData.statistics.nodeCount + newNodes.length,
                edgeCount: state.graphData.statistics.edgeCount + newEdges.length,
              },
            },
          }
        })
      },

      setSelectedEntity: (entity) => {
        set({ selectedEntity: entity })
      },

      toggleNodeSelection: (nodeId) => {
        set((state) => {
          const newSet = new Set(state.selectedNodes)
          if (newSet.has(nodeId)) {
            newSet.delete(nodeId)
          } else {
            newSet.add(nodeId)
          }
          return { selectedNodes: newSet }
        })
      },

      clearSelection: () => {
        set({ selectedNodes: new Set(), highlightedPath: [], selectedEntity: null })
      },

      setHighlightedPath: (path) => {
        set({ highlightedPath: path })
      },

      setCurrentLayout: (layout) => {
        set({ currentLayout: layout })
      },

      setZoomLevel: (level) => {
        set({ zoomLevel: Math.max(0, Math.min(100, level)) })
      },

      toggleFullscreen: () => {
        set((state) => ({ isFullscreen: !state.isFullscreen }))
      },

      toggleShowLabels: () => {
        set((state) => {
          const newShowLabels = !state.showLabels
          if (state.graphData) {
            return {
              showLabels: newShowLabels,
              graphData: {
                ...state.graphData,
                options: {
                  ...state.graphData.options,
                  showLabels: newShowLabels,
                },
              },
            }
          }
          return { showLabels: newShowLabels }
        })
      },

      toggleShowEdgeLabels: () => {
        set((state) => ({ showEdgeLabels: !state.showEdgeLabels }))
      },

      resetView: () => {
        set(initialState)
      },

      undo: () => {
        const { viewHistory } = get()
        if (viewHistory.length > 0) {
          const previous = viewHistory[viewHistory.length - 1]
          set((state) => ({
            graphData: previous,
            viewHistory: state.viewHistory.slice(0, -1),
          }))
        }
      },

      redo: () => {
        console.warn('Redo not yet implemented')
      },

      clearError: () => {
        set({ error: null })
      },
    }),
    {
      name: 'graph-store',
      partialize: (state) => ({
        currentLayout: state.currentLayout,
        zoomLevel: state.zoomLevel,
        showLabels: state.showLabels,
        showEdgeLabels: state.showEdgeLabels,
      }),
    }
  )
)

export const selectHasGraphData = (state: GraphState) =>
  state.graphData !== null && state.graphData.nodes.length > 0

export const selectNodeCount = (state: GraphState) =>
  state.graphData?.nodes.length ?? 0

export const selectEdgeCount = (state: GraphState) =>
  state.graphData?.edges.length ?? 0

export const selectHasSelectedNodes = (state: GraphState) =>
  state.selectedNodes.size > 0

export const selectSelectedNodeCount = (state: GraphState) =>
  state.selectedNodes.size

export const selectNodeTypeStats = (state: GraphState): Record<string, number> => {
  if (!state.graphData) return {}

  return state.graphData.nodes.reduce((acc, node) => {
    acc[node.type] = (acc[node.type] || 0) + 1
    return acc
  }, {} as Record<string, number>)
}