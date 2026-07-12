import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { documentApi, type ProcessResult } from '@/lib/api'
import { normalizeError } from '@/lib/utils'

// ========================================
// 类型定义
// ========================================

export interface Document {
  id: string
  filename: string
  file_path: string
  success: boolean
  chunk_count: number
  embedding_count: number
  error_message?: string | null
  uploaded_at: string
}

interface DocumentState {
  // 数据状态
  documents: Document[]

  // UI状态
  isLoading: boolean
  isUploading: boolean
  uploadProgress: Record<string, number>
  selectedDocumentIds: Set<string>
  error: string | null

  // 当前知识库（集合名）
  currentCollection: string | null

  // Actions
  fetchDocuments: (collectionName: string) => Promise<void>
  uploadFiles: (files: File[], collectionName: string) => Promise<ProcessResult[]>
  deleteDocument: (id: string) => void
  batchDeleteDocuments: (ids: string[]) => void
  setSelectedDocuments: (ids: Set<string>) => void
  toggleDocumentSelection: (id: string) => void
  clearSelection: () => void
  clearError: () => void
  reset: () => void
}

const initialState = {
  documents: [] as Document[],
  isLoading: false,
  isUploading: false,
  uploadProgress: {} as Record<string, number>,
  selectedDocumentIds: new Set<string>(),
  error: null as string | null,
  currentCollection: null as string | null,
}

// ========================================
// 文档管理状态 Store
// ========================================

export const useDocumentStore = create<DocumentState>()(
  persist(
    (set, get) => ({
      ...initialState,

      /**
       * 从后端获取文档列表
       */
      fetchDocuments: async (collectionName) => {
        set({ isLoading: true, error: null, currentCollection: collectionName })

        try {
          const listResp = await documentApi.list(collectionName)
          const docsList = listResp.data.documents || []

          const docs: Document[] = (docsList || []).map((d) => ({
            id: d.id,
            filename: d.filename,
            file_path: d.file_path,
            success: true,
            chunk_count: d.chunk_count,
            embedding_count: d.chunk_count,
            error_message: null,
            uploaded_at: d.uploaded_at || new Date().toISOString(),
          }))

          set({ documents: docs, isLoading: false })
        } catch (error: any) {
          set({
            isLoading: false,
            error: normalizeError(error, '获取文档列表失败'),
          })
        }
      },

      /**
       * 上传文件到指定集合
       */
      uploadFiles: async (files, collectionName) => {
        set({ isUploading: true, error: null, currentCollection: collectionName })

        try {
          const batchResp = await documentApi.processBatch(files, collectionName)
          const results = batchResp.data.results || []

          // 将结果转换为文档列表
          const newDocs: Document[] = results.map((r) => ({
            id: `${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
            filename: r.file_path?.split('/').pop() || r.filename || '',
            file_path: r.file_path || '',
            success: r.success,
            chunk_count: r.chunk_count || 0,
            embedding_count: r.chunk_count || 0,
            error_message: r.error_message || r.error || null,
            uploaded_at: new Date().toISOString(),
          }))

          set((state) => ({
            documents: [...newDocs, ...state.documents],
            isUploading: false,
          }))

          return results
        } catch (error: any) {
          set({
            isUploading: false,
            error: normalizeError(error, '上传文件失败'),
          })
          throw error
        }
      },

      /**
       * 删除单个文档（仅本地移除，后端无单条删除API）
       */
      deleteDocument: (id) => {
        set((state) => ({
          documents: state.documents.filter((doc) => doc.id !== id),
          selectedDocumentIds: (() => {
            const newSet = new Set(state.selectedDocumentIds)
            newSet.delete(id)
            return newSet
          })(),
        }))
      },

      /**
       * 批量删除文档（仅本地移除）
       */
      batchDeleteDocuments: (ids) => {
        set((state) => ({
          documents: state.documents.filter((doc) => !ids.includes(doc.id)),
          selectedDocumentIds: new Set<string>(),
        }))
      },

      /**
       * 设置多选的文档集合
       */
      setSelectedDocuments: (ids) => {
        set({ selectedDocumentIds: ids })
      },

      /**
       * 切换单个文档的选择状态
       */
      toggleDocumentSelection: (id) => {
        set((state) => {
          const newSet = new Set(state.selectedDocumentIds)
          if (newSet.has(id)) {
            newSet.delete(id)
          } else {
            newSet.add(id)
          }
          return { selectedDocumentIds: newSet }
        })
      },

      /**
       * 清空选择
       */
      clearSelection: () => {
        set({ selectedDocumentIds: new Set<string>() })
      },

      /**
       * 清除错误
       */
      clearError: () => {
        set({ error: null })
      },

      /**
       * 重置所有状态
       */
      reset: () => {
        set(initialState)
      },
    }),
    {
      name: 'document-store',
      partialize: (state) => ({
        currentCollection: state.currentCollection,
      }),
    }
  )
)

// ========================================
// Selectors
// ========================================

/** 是否有选中的文档 */
export const selectHasSelectedDocs = (state: DocumentState) =>
  state.selectedDocumentIds.size > 0

/** 选中文档数量 */
export const selectSelectedCount = (state: DocumentState) =>
  state.selectedDocumentIds.size

/** 成功上传的文档数量 */
export const selectSuccessCount = (state: DocumentState) =>
  state.documents.filter((d) => d.success).length

/** 失败上传的文档数量 */
export const selectFailedCount = (state: DocumentState) =>
  state.documents.filter((d) => !d.success).length
