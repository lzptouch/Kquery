/**
 * 笔记状态管理 Store (P0-2)
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { noteService, type Note, type NoteCreateRequest, type NoteUpdateRequest, type AIAction } from '@/services/noteService'

interface NoteState {
  notes: Note[]
  currentNote: Note | null
  isLoading: boolean
  error: string | null
  aiResult: string
  isAiStreaming: boolean

  fetchNotes: (collection?: string) => Promise<void>
  getNote: (id: string) => Promise<void>
  createNote: (data: NoteCreateRequest) => Promise<Note | null>
  updateNote: (id: string, data: NoteUpdateRequest) => Promise<void>
  deleteNote: (id: string) => Promise<void>
  aiAction: (id: string, action: AIAction, content: string) => Promise<void>
  aiActionStream: (id: string, action: AIAction, content: string) => Promise<void>
  ingestToKb: (noteId: string, collectionName: string) => Promise<void>
  setCurrentNote: (note: Note | null) => void
  clearError: () => void
}

export const useNoteStore = create<NoteState>()(
  persist(
    (set, get) => ({
      notes: [],
      currentNote: null,
      isLoading: false,
      error: null,
      aiResult: '',
      isAiStreaming: false,

      fetchNotes: async (collection) => {
        set({ isLoading: true, error: null })
        try {
          const notes = await noteService.list({ collection })
          set({ notes, isLoading: false })
        } catch (err: any) {
          set({ error: err.message || '获取笔记失败', isLoading: false })
        }
      },

      getNote: async (id) => {
        set({ isLoading: true, error: null })
        try {
          const note = await noteService.get(id)
          set({ currentNote: note, isLoading: false })
        } catch (err: any) {
          set({ error: err.message || '获取笔记详情失败', isLoading: false })
        }
      },

      createNote: async (data) => {
        set({ isLoading: true, error: null })
        try {
          const note = await noteService.create(data)
          set((s) => ({ notes: [note, ...s.notes], currentNote: note, isLoading: false }))
          return note
        } catch (err: any) {
          set({ error: err.message || '创建笔记失败', isLoading: false })
          return null
        }
      },

      updateNote: async (id, data) => {
        set({ isLoading: true, error: null })
        try {
          const updated = await noteService.update(id, data)
          set((s) => ({
            notes: s.notes.map((n) => (n.id === id ? updated : n)),
            currentNote: s.currentNote?.id === id ? updated : s.currentNote,
            isLoading: false,
          }))
        } catch (err: any) {
          set({ error: err.message || '更新笔记失败', isLoading: false })
        }
      },

      deleteNote: async (id) => {
        set({ isLoading: true, error: null })
        try {
          await noteService.delete(id)
          set((s) => ({
            notes: s.notes.filter((n) => n.id !== id),
            currentNote: s.currentNote?.id === id ? null : s.currentNote,
            isLoading: false,
          }))
        } catch (err: any) {
          set({ error: err.message || '删除笔记失败', isLoading: false })
        }
      },

      aiAction: async (id, action, content) => {
        set({ isLoading: true, error: null, aiResult: '' })
        try {
          const result = await noteService.aiAction(id, { action, content })
          set({ aiResult: result.result, isLoading: false })
        } catch (err: any) {
          set({ error: err.message || 'AI 操作失败', isLoading: false })
        }
      },

      aiActionStream: async (id, action, content) => {
        set({ isAiStreaming: true, error: null, aiResult: '' })
        try {
          const stream = await noteService.aiActionStream(id, { action, content })
          const reader = stream.getReader()
          const decoder = new TextDecoder()
          let accumulated = ''

          while (true) {
            const { done, value } = await reader.read()
            if (done) break
            accumulated += decoder.decode(value, { stream: true })
            set({ aiResult: accumulated })
          }
          set({ isAiStreaming: false })
        } catch (err: any) {
          set({ error: err.message || 'AI 流式操作失败', isAiStreaming: false })
        }
      },

      ingestToKb: async (noteId, collectionName) => {
        set({ error: null })
        try {
          await noteService.ingest({ note_id: noteId, collection_name: collectionName })
        } catch (err: any) {
          set({ error: err.message || '入库失败' })
        }
      },

      setCurrentNote: (note) => set({ currentNote: note }),
      clearError: () => set({ error: null }),
    }),
    {
      name: 'note-store',
      partialize: (state) => ({
        notes: state.notes.slice(0, 50), // 只持久化最近 50 条
      }),
    }
  )
)
