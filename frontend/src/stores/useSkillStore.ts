/**
 * Skills 状态管理 Store (P2-2)
 */

import { create } from 'zustand'
import { skillService, type Skill, type SkillCreateRequest, type SkillMatchResult } from '@/services/skillService'

interface SkillState {
  skills: Skill[]
  activeSkillId: string | null
  matchResults: SkillMatchResult[]
  isLoading: boolean
  error: string | null

  fetchSkills: (includeDisabled?: boolean) => Promise<void>
  getSkill: (id: string) => Promise<Skill | null>
  createSkill: (data: SkillCreateRequest) => Promise<void>
  updateSkill: (id: string, data: Partial<Skill>) => Promise<void>
  deleteSkill: (id: string) => Promise<void>
  toggleSkill: (id: string, enabled: boolean) => Promise<void>
  matchSkill: (query: string) => Promise<void>
  setActiveSkill: (id: string | null) => void
  clearError: () => void
}

export const useSkillStore = create<SkillState>()((set, get) => ({
  skills: [],
  activeSkillId: null,
  matchResults: [],
  isLoading: false,
  error: null,

  fetchSkills: async (includeDisabled) => {
    set({ isLoading: true, error: null })
    try {
      const skills = await skillService.list(includeDisabled)
      set({ skills, isLoading: false })
    } catch (err: any) {
      set({ error: err.message || '获取 Skills 失败', isLoading: false })
    }
  },

  getSkill: async (id) => {
    try {
      return await skillService.get(id)
    } catch {
      return null
    }
  },

  createSkill: async (data) => {
    set({ isLoading: true, error: null })
    try {
      const skill = await skillService.create(data)
      set((s) => ({ skills: [...s.skills, skill], isLoading: false }))
    } catch (err: any) {
      set({ error: err.message || '创建 Skill 失败', isLoading: false })
    }
  },

  updateSkill: async (id, data) => {
    set({ error: null })
    try {
      const updated = await skillService.update(id, data as any)
      set((s) => ({
        skills: s.skills.map((sk) => (sk.id === id ? updated : sk)),
      }))
    } catch (err: any) {
      set({ error: err.message || '更新 Skill 失败' })
    }
  },

  deleteSkill: async (id) => {
    try {
      await skillService.delete(id)
      set((s) => ({
        skills: s.skills.filter((sk) => sk.id !== id),
        activeSkillId: s.activeSkillId === id ? null : s.activeSkillId,
      }))
    } catch (err: any) {
      set({ error: err.message || '删除 Skill 失败' })
    }
  },

  toggleSkill: async (id, enabled) => {
    try {
      if (enabled) {
        await skillService.enable(id)
      } else {
        await skillService.disable(id)
      }
      set((s) => ({
        skills: s.skills.map((sk) => (sk.id === id ? { ...sk, is_enabled: enabled } : sk)),
      }))
    } catch (err: any) {
      set({ error: err.message || '切换 Skill 状态失败' })
    }
  },

  matchSkill: async (query) => {
    try {
      const results = await skillService.match(query)
      set({ matchResults: results })
    } catch {
      set({ matchResults: [] })
    }
  },

  setActiveSkill: (id) => set({ activeSkillId: id }),
  clearError: () => set({ error: null }),
}))
