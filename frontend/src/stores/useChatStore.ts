import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { agentApi, type AgentChatResult } from '@/lib/api'
import { normalizeError } from '@/lib/utils'

// ========================================
// 类型定义
// ========================================

export interface ChatImage {
  id: string
  name: string
  dataUrl: string
  size: number
  type: string
}

export interface Message {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  images?: ChatImage[]
  timestamp: string
  isLoading?: boolean
  toolsUsed?: string[]
}


export interface ConversationSession {
  session_id: string
  collection_name: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
}

interface ChatState {
  // 数据状态
  messages: Message[]
  currentSessionId: string | null
  sessions: ConversationSession[]

  // UI状态
  isLoading: boolean
  isStreaming: boolean
  error: string | null

  // 当前使用的知识库
  activeCollectionName: string

  // Actions
  sendMessage: (payload: { text: string; images: ChatImage[] }) => Promise<void>
  clearMessages: () => void
  deleteMessage: (messageId: string) => void

  createSession: (collectionName?: string) => ConversationSession
  switchSession: (sessionId: string) => void

  setActiveCollection: (collectionName: string) => void
  clearError: () => void
  retryLastMessage: () => Promise<void>
}


const generateMessageId = () => `msg_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`
const generateSessionId = () => `session_${Date.now()}_${Math.random().toString(36).substr(2, 6)}`

// ========================================
// 聊天状态管理 Store
// ========================================

export const useChatStore = create<ChatState>()(
  persist(
    (set, get) => ({
      // 初始状态
      messages: [],
      currentSessionId: null,
      sessions: [],
      isLoading: false,
      isStreaming: false,
      error: null,
      activeCollectionName: '',

      /**
       * 发送消息（使用Agent智能问答）
       */
      sendMessage: async ({ text, images }) => {
        const { activeCollectionName } = get()

        // 空 collection_name 后端会自动降级为通用助手模式

        const userMessage: Message = {
          id: generateMessageId(),
          role: 'user',
          content: text,
          images: images.length > 0 ? images : undefined,
          timestamp: new Date().toISOString(),
        }

        // 立即添加用户消息到界面
        set((state) => ({
          messages: [...state.messages, userMessage],
          isLoading: true,
          error: null,
        }))

        // 创建助手消息占位符
        const assistantMessageId = generateMessageId()
        set((state) => ({
          messages: [
            ...state.messages,
            {
              id: assistantMessageId,
              role: 'assistant',
              content: '',
              timestamp: new Date().toISOString(),
              isLoading: true,
            },
          ],
        }))

        try {
          // agentApi.chat() 返回 AxiosResponse，需要提取 .data
          const chatResp = await agentApi.chat({
            query: text,
            images: images.length > 0
              ? images.map((img) => ({ id: img.id, data_url: img.dataUrl, type: img.type }))
              : undefined,
            collection_name: activeCollectionName || '',
            enable_rag: !!activeCollectionName,
          })
          const result: AgentChatResult = chatResp.data

          // 更新助手消息内容
          set((state) => ({
            messages: state.messages.map((msg) =>
              msg.id === assistantMessageId
                ? {
                    ...msg,
                    content: result.response || '',
                    toolsUsed: result.tools_used || [],
                    isLoading: false,
                    timestamp: new Date().toISOString(),
                  }
                : msg
            ),
            isLoading: false,
          }))
        } catch (error: any) {
          set((state) => ({
            messages: state.messages.map((msg) =>
              msg.id === assistantMessageId
                ? {
                    ...msg,
                    content: '抱歉，回答生成失败。请稍后重试。',
                    isLoading: false,
                  }
                : msg
            ),
            isLoading: false,
            error: normalizeError(error, '发送消息失败'),
          }))
        }
      },


      /**
       * 清空当前会话的所有消息
       */
      clearMessages: () => {
        set({ messages: [] })
      },

      /**
       * 删除单条消息
       */
      deleteMessage: (messageId) => {
        set((state) => ({
          messages: state.messages.filter((m) => m.id !== messageId),
        }))
      },

      /**
       * 创建新的对话会话
       */
      createSession: (collectionName) => {
        const newSession: ConversationSession = {
          session_id: generateSessionId(),
          collection_name: collectionName || get().activeCollectionName,
          title: `新对话 ${new Date().toLocaleString('zh-CN')}`,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          message_count: 0,
        }

        set((state) => ({
          sessions: [newSession, ...state.sessions],
          currentSessionId: newSession.session_id,
          messages: [], // 清空旧消息
        }))

        return newSession
      },

      /**
       * 切换到指定会话
       */
      switchSession: (sessionId) => {
        set({ currentSessionId: sessionId })
      },

      /**
       * 设置当前活跃的集合
       */
      setActiveCollection: (collectionName) => {
        set({
          activeCollectionName: collectionName || '',  // 允许清除选择（设为空字符串）
          messages: [], // 切换集合时清空消息
        })
      },

      /**
       * 清除错误
       */
      clearError: () => {
        set({ error: null })
      },

      /**
       * 重试最后一条用户消息
       */
      retryLastMessage: async () => {
        const { messages } = get()
        const lastUserMsg = [...messages].reverse().find((m) => m.role === 'user')

        if (!lastUserMsg) return

        // 删除最后一条用户消息及其后的所有消息
        set((state) => ({
          messages: state.messages.filter(
            (m) => m.timestamp <= lastUserMsg.timestamp && m !== lastUserMsg
          ),
        }))

        // 重新发送（携带原图片）
        await get().sendMessage({
          text: lastUserMsg.content,
          images: lastUserMsg.images || [],
        })
      },

    }),
    {
      name: 'chat-store',
      partialize: (state) => ({
        sessions: state.sessions,
        currentSessionId: state.currentSessionId,
        activeCollectionName: state.activeCollectionName,
      }),
    }
  )
)

// ========================================
// Selectors
// ========================================

/** 最后一条消息 */
export const selectLastMessage = (state: ChatState) =>
  state.messages[state.messages.length - 1]

/** 用户消息数量 */
export const selectUserMessageCount = (state: ChatState) =>
  state.messages.filter((m) => m.role === 'user').length

/** 助手消息数量 */
export const selectAssistantMessageCount = (state: ChatState) =>
  state.messages.filter((m) => m.role === 'assistant').length

/** 当前会话是否为空 */
export const selectIsConversationEmpty = (state: ChatState) =>
  state.messages.length === 0

/** 是否可以发送消息 */
export const selectCanSendMessage = (state: ChatState) =>
  !state.isLoading && !state.isStreaming  // 移除 activeCollectionName 要求
