import { act, renderHook, waitFor } from '@testing-library/react'
import { useChatStore, selectLastMessage, selectUserMessageCount, selectCanSendMessage } from '@/stores/useChatStore'
import { agentApi } from '@/lib/api'

vi.mock('@/lib/api', () => ({
  agentApi: {
    chat: vi.fn(),
  },
}))

const mockedAgentApi = vi.mocked(agentApi, true)

describe('useChatStore', () => {
  beforeEach(() => {
    useChatStore.setState({
      messages: [],
      sessions: [],
      currentSessionId: null,
      isLoading: false,
      isStreaming: false,
      error: null,
      activeCollectionName: '',
    })
    mockedAgentApi.chat.mockReset()
  })

  it('should create a new session', () => {
    const session = useChatStore.getState().createSession('kb-1')
    expect(session.collection_name).toBe('kb-1')
    expect(useChatStore.getState().currentSessionId).toBe(session.session_id)
  })

  it('should send message and update state', async () => {
    mockedAgentApi.chat.mockResolvedValue({
      data: {
        success: true,
        response: 'mock answer',
        session_id: 's-1',
        tools_used: [],
      },
    } as any)

    await act(async () => {
      await useChatStore.getState().sendMessage({ text: 'hi', images: [] })
    })

    await waitFor(() => expect(useChatStore.getState().isLoading).toBe(false))
    expect(selectUserMessageCount(useChatStore.getState())).toBe(1)
    expect(selectLastMessage(useChatStore.getState())?.role).toBe('assistant')
  })

  it('should handle send error gracefully', async () => {
    mockedAgentApi.chat.mockRejectedValue(new Error('network error'))

    await act(async () => {
      await useChatStore.getState().sendMessage({ text: 'hi', images: [] })
    })

    await waitFor(() => expect(useChatStore.getState().isLoading).toBe(false))
    expect(useChatStore.getState().error).toMatch(/发送消息失败|network error|请求失败/i)
  })

  it('selectCanSendMessage returns true when not loading', () => {
    expect(selectCanSendMessage(useChatStore.getState())).toBe(true)
  })

  it('selectCanSendMessage returns false when loading', () => {
    useChatStore.setState({ isLoading: true })
    expect(selectCanSendMessage(useChatStore.getState())).toBe(false)
  })
})
