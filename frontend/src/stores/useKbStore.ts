import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { collectionApi } from '@/lib/api'
import { normalizeError } from '@/lib/utils'

// ========================================
// 类型定义
// ========================================

export interface KnowledgeBase {
  id: string          // 使用集合名称作为ID
  name: string        // 集合名称
  description: string | null
  document_count: number
  chunk_count: number
  embedding_dimension: number
  status: 'active' | 'empty'
  created_at?: string
}

interface KbState {
  // 数据状态
  knowledgeBases: KnowledgeBase[]
  currentKbId: string | null

  // UI状态
  isLoading: boolean
  isCreating: boolean
  error: string | null

  // Actions
  fetchKnowledgeBases: () => Promise<void>
  createKnowledgeBase: (data: { name: string; description?: string }) => Promise<KnowledgeBase>
  deleteKnowledgeBase: (id: string) => Promise<void>
  setCurrentKbId: (id: string | null) => void
  clearError: () => void
}

// ========================================
// 知识库状态管理 Store
// ========================================

export const useKbStore = create<KbState>()(
  persist(
    (set, get) => ({
      // 初始状态
      knowledgeBases: [],
      currentKbId: null,
      isLoading: false,
      isCreating: false,
      error: null,

      /**
       * 获取知识库列表（从Milvus集合）
       */
      fetchKnowledgeBases: async () => {
        set({ isLoading: true, error: null })

        try {
          const listResp = await collectionApi.list()
          const collectionNames = listResp.data.collections || []

          // 获取每个集合的统计信息
          const kbList: KnowledgeBase[] = []
          for (const name of collectionNames) {
            try {
              const statsResp = await collectionApi.stats(name)
              const stats = statsResp.data
              kbList.push({
                id: name,
                name,
                description: null,
                document_count: stats?.count || 0,
                chunk_count: stats?.count || 0,
                embedding_dimension: stats?.dimension || 1024,
                status: (stats?.count || 0) > 0 ? 'active' : 'empty',
              })
            } catch {
              kbList.push({
                id: name,
                name,
                description: null,
                document_count: 0,
                chunk_count: 0,
                embedding_dimension: 1024,
                status: 'empty',
              })
            }
          }

          set({
            knowledgeBases: kbList,
            currentKbId: kbList.length > 0 ? kbList[0].id : null,
            isLoading: false,
          })
        } catch (error: any) {
          set({
            isLoading: false,
            error: normalizeError(error, '获取知识库列表失败'),
          })
        }
      },

      /**
       * 创建知识库（前端模拟，实际通过上传文档到指定集合名创建）
       */
      createKnowledgeBase: async (data) => {
        set({ isCreating: true, error: null })

        try {
          const newKb: KnowledgeBase = {
            id: data.name,
            name: data.name,
            description: data.description || null,
            document_count: 0,
            chunk_count: 0,
            embedding_dimension: 1024,
            status: 'empty',
          }

          set((state) => ({
            knowledgeBases: [newKb, ...state.knowledgeBases],
            isCreating: false,
          }))

          return newKb
        } catch (error: any) {
          set({
            isCreating: false,
            error: normalizeError(error, '创建知识库失败'),
          })
          throw error
        }
      },

      /**
       * 删除知识库（清空Milvus集合）
       */
      deleteKnowledgeBase: async (id) => {
        try {
          await collectionApi.clear(id)

          set((state) => ({
            knowledgeBases: state.knowledgeBases.filter((kb) => kb.id !== id),
            currentKbId: state.currentKbId === id ? null : state.currentKbId,
          }))
        } catch (error: any) {
          set({
            error: normalizeError(error, '删除知识库失败'),
          })
          throw error
        }
      },

      /**
       * 设置当前选中的知识库ID
       */
      setCurrentKbId: (id) => {
        set({ currentKbId: id })
      },

      /**
       * 清除错误信息
       */
      clearError: () => {
        set({ error: null })
      },
    }),
    {
      name: 'kb-store',
      partialize: (state) => ({
        currentKbId: state.currentKbId,
      }),
    }
  )
)

// ========================================
// Selectors（派生状态）
// ========================================

/** 获取当前选中的知识库详情 */
export const selectCurrentKb = (state: KbState): KnowledgeBase | undefined =>
  state.currentKbId ? state.knowledgeBases.find((kb) => kb.id === state.currentKbId) : undefined

/** 是否有活跃的知识库 */
export const selectHasActiveKb = (state: KbState) =>
  state.knowledgeBases.some((kb) => kb.status === 'active')

/** 统计总文档数 */
export const selectTotalDocuments = (state: KbState) =>
  state.knowledgeBases.reduce((sum, kb) => sum + kb.document_count, 0)

/** 是否正在加载或创建 */
export const selectIsBusy = (state: KbState) => state.isLoading || state.isCreating
