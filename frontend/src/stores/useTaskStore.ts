/**
 * 任务状态管理 Store (P1-3)
 */

import { create } from 'zustand'
import { taskService, type Task, type TaskCreateRequest, type TaskStep } from '@/services/taskService'

interface TaskState {
  tasks: Task[]
  currentTask: Task | null
  isLoading: boolean
  error: string | null
  streamCleanup: (() => void) | null

  fetchTasks: () => Promise<void>
  getTask: (id: string) => Promise<void>
  createTask: (data: TaskCreateRequest) => Promise<Task | null>
  executeTask: (id: string) => Promise<void>
  cancelTask: (id: string) => Promise<void>
  streamProgress: (id: string) => void
  stopStream: () => void
  setCurrentTask: (task: Task | null) => void
  clearError: () => void
}

export const useTaskStore = create<TaskState>()((set, get) => ({
  tasks: [],
  currentTask: null,
  isLoading: false,
  error: null,
  streamCleanup: null,

  fetchTasks: async () => {
    set({ isLoading: true, error: null })
    try {
      const tasks = await taskService.list()
      set({ tasks, isLoading: false })
    } catch (err: any) {
      set({ error: err.message || '获取任务列表失败', isLoading: false })
    }
  },

  getTask: async (id) => {
    set({ isLoading: true, error: null })
    try {
      const task = await taskService.get(id)
      set({ currentTask: task, isLoading: false })
    } catch (err: any) {
      set({ error: err.message || '获取任务详情失败', isLoading: false })
    }
  },

  createTask: async (data) => {
    set({ isLoading: true, error: null })
    try {
      const task = await taskService.create(data)
      set((s) => ({ tasks: [task, ...s.tasks], currentTask: task, isLoading: false }))
      return task
    } catch (err: any) {
      set({ error: err.message || '创建任务失败', isLoading: false })
      return null
    }
  },

  executeTask: async (id) => {
    set({ error: null })
    try {
      const task = await taskService.execute(id)
      set((s) => ({
        tasks: s.tasks.map((t) => (t.id === id ? task : t)),
        currentTask: s.currentTask?.id === id ? task : s.currentTask,
      }))
    } catch (err: any) {
      set({ error: err.message || '执行任务失败' })
    }
  },

  cancelTask: async (id) => {
    try {
      await taskService.cancel(id)
      get().stopStream()
      set((s) => ({
        tasks: s.tasks.map((t) =>
          t.id === id ? { ...t, status: 'cancelled' as const } : t
        ),
        currentTask:
          s.currentTask?.id === id
            ? { ...s.currentTask, status: 'cancelled' as const }
            : s.currentTask,
      }))
    } catch (err: any) {
      set({ error: err.message || '取消任务失败' })
    }
  },

  streamProgress: (id) => {
    get().stopStream()
    const cleanup = taskService.streamProgress(
      id,
      (data) => {
        if (data.type === 'step_update') {
          set((s) => {
            const updatedSteps = (s.currentTask?.steps || []).map((step: TaskStep) =>
              step.id === data.step_id
                ? { ...step, status: data.status, result: data.result }
                : step
            )
            const updatedTask = s.currentTask
              ? { ...s.currentTask, steps: updatedSteps, status: data.task_status || s.currentTask.status }
              : null
            return {
              currentTask: updatedTask,
              tasks: s.tasks.map((t) => (t.id === id ? updatedTask! : t)),
            }
          })
        } else if (data.type === 'complete') {
          set((s) => {
            const completedTask = s.currentTask
              ? { ...s.currentTask, status: 'completed' as const }
              : null
            return {
              currentTask: completedTask,
              tasks: s.tasks.map((t) => (t.id === id ? completedTask! : t)),
            }
          })
          get().stopStream()
        }
      },
      (err) => {
        set({ error: err.message || '任务流连接中断' })
      }
    )
    set({ streamCleanup: cleanup })
  },

  stopStream: () => {
    const { streamCleanup } = get()
    if (streamCleanup) {
      streamCleanup()
      set({ streamCleanup: null })
    }
  },

  setCurrentTask: (task) => set({ currentTask: task }),
  clearError: () => set({ error: null }),
}))
