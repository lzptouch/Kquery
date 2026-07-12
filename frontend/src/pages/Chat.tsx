import React, { useState, useRef, useEffect } from 'react'
import {
  Send,
  Square,
  RotateCcw,
  Trash2,
  MessageSquare,
  Database,
  FileText,
  Copy,
  ThumbsUp,
  ThumbsDown,
  Loader2,
  Bot,
  User,
  X,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { ScrollArea } from '@/components/ui/scroll-area'
import { cn } from '@/lib/utils'
import { useChatStore, type ChatImage } from '@/stores/useChatStore'
import { useKbStore } from '@/stores/useKbStore'
import { ChatImageUpload } from '@/components/ChatImageUpload'

/**
 * 智能问答聊天界面
 */
export function Chat() {
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  // 聊天状态
  const {
    messages,
    isLoading,
    isStreaming,
    activeCollectionName,
    sessions,
    sendMessage,
    clearMessages,
    createSession,
    setActiveCollection,
    retryLastMessage,
  } = useChatStore()

  // 知识库列表（用于选择）
  const knowledgeBases = useKbStore((state) => state.knowledgeBases)
  const fetchKnowledgeBases = useKbStore((state) => state.fetchKnowledgeBases)

  // 加载知识库列表
  useEffect(() => {
    fetchKnowledgeBases()
  }, [fetchKnowledgeBases])

  // UI状态
  const [inputMessage, setInputMessage] = useState('')
  const [isComposing, setIsComposing] = useState(false)
  const [pendingImages, setPendingImages] = useState<ChatImage[]>([])

  // 自动滚动到底部

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  useEffect(() => {
    scrollToBottom()
  }, [messages])

  // 发送消息
  const handleSend = async () => {
    const text = inputMessage.trim()
    if ((!text && pendingImages.length === 0) || isLoading || isStreaming) return

    setInputMessage('')
    const imagesToSend = [...pendingImages]
    setPendingImages([])

    await sendMessage({ text, images: imagesToSend })

    // 重新聚焦输入框
    inputRef.current?.focus()
  }

  // 处理键盘事件
  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !isComposing) {
      e.preventDefault()
      handleSend()
    }
  }

  // 选择知识库
  const handleSelectKb = (kbName: string) => {
    setActiveCollection(kbName)
    clearMessages()
    setPendingImages([])
  }


  return (
    <div className="h-[calc(100vh-8rem)] flex flex-col animate-fade-in">
      {/* 页面标题 */}
      <div className="flex items-center justify-between pb-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight flex items-center gap-3">
            智能问答
            <Badge variant="success">在线</Badge>
          </h1>
          <p className="text-muted-foreground mt-1">
            基于RAG增强的智能对话助手
          </p>
        </div>

        {/* 操作按钮 */}
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => createSession()}>
            新建会话
          </Button>
          <Button variant="outline" size="sm" onClick={clearMessages} disabled={messages.length === 0}>
            <Trash2 className="mr-1 h-4 w-4" />清空
          </Button>
          {messages.length > 0 && (
            <Button variant="outline" size="sm" onClick={retryLastMessage} disabled={isLoading || isStreaming}>
              <RotateCcw className="mr-1 h-4 w-4" />重试
            </Button>
          )}
        </div>
      </div>

      <div className="flex-1 grid grid-cols-1 lg:grid-cols-4 gap-6 min-h-0">
        {/* 左侧：知识库选择 + 会话历史 */}
        <div className="lg:col-span-1 space-y-4 hidden lg:block">
          {/* 知识库选择卡片 */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base flex items-center gap-2">
                <Database className="h-4 w-4" />
                知识库选择
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <ScrollArea className="max-h-[300px]">
                <div className="px-4 pb-4 space-y-2">
                  {/* 通用问答选项 */}
                  <button
                    onClick={() => handleSelectKb('')}
                    className={cn(
                      "w-full text-left p-3 rounded-lg border transition-all",
                      !activeCollectionName
                        ? 'border-primary bg-primary/5 shadow-md'
                        : 'hover:border-primary/50 hover:bg-accent'
                    )}
                  >
                    <div className="font-medium text-sm flex items-center gap-2">
                      <Bot className="h-4 w-4" />
                      通用问答
                    </div>
                    <div className="text-xs text-muted-foreground mt-1">
                      不关联知识库，基于 AI 自身知识回答
                    </div>
                  </button>

                  {knowledgeBases.map((kb) => (
                    <button
                      key={kb.id}
                      onClick={() => handleSelectKb(kb.name)}
                      className={cn(
                        "w-full text-left p-3 rounded-lg border transition-all",
                        activeCollectionName === kb.name
                          ? 'border-primary bg-primary/5 shadow-md'
                          : 'hover:border-primary/50 hover:bg-accent'
                      )}
                    >
                      <div className="font-medium text-sm">{kb.name}</div>
                      <div className="text-xs text-muted-foreground mt-1 flex items-center gap-3">
                        <span className="flex items-center gap-1">
                          <FileText className="h-3 w-3" />
                          {kb.document_count}
                        </span>
                      </div>
                    </button>
                  ))}

                  {knowledgeBases.length === 0 && (
                    <div className="py-8 text-center text-sm text-muted-foreground">
                      暂无可用知识库<br/>
                      请先创建或导入
                    </div>
                  )}
                </div>
              </ScrollArea>
            </CardContent>
          </Card>

          {/* 会话历史 */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base flex items-center gap-2">
                <MessageSquare className="h-4 w-4" />
                历史会话
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-sm text-muted-foreground py-4 text-center">
                {sessions.length === 0 ? '暂无历史会话' : `${sessions.length} 个会话`}
              </div>
            </CardContent>
          </Card>
        </div>

        {/* 右侧：聊天主区域 */}
        <Card className="lg:col-span-3 flex flex-col overflow-hidden">
          {/* 当前选中的知识库标识（移动端显示） */}
          {!activeCollectionName && (
            <div className="p-3 bg-blue-50 dark:bg-blue-950 border-b">
              <p className="text-sm text-blue-700 dark:text-blue-300 text-center">
                ℹ️ 当前为通用问答模式，回答基于 AI 自身知识。选择一个知识库可获取专属内容检索。
              </p>
            </div>
          )}

          {/* 消息列表区域 */}
          <ScrollArea className="flex-1 p-4">
            <div className="space-y-4 min-h-full">
              {messages.length === 0 ? (
                /* 空状态欢迎消息 */
                <div className="h-full flex items-center justify-center">
                  <div className="text-center space-y-4 max-w-md">
                    <div className="p-4 rounded-full bg-primary/10 inline-block">
                      <Bot className="h-16 w-16 text-primary" />
                    </div>

                    <div>
                      <h3 className="text-xl font-semibold">欢迎使用智能助手</h3>
                      <p className="text-sm text-muted-foreground mt-2 leading-relaxed">
                        {activeCollectionName
                          ? '我是基于RAG技术的AI助手。我可以帮您：'
                          : '我是KQuery智能助手。我可以帮您：'}
                      </p>
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-left">
                      {[
                        { icon: '\u{1F4DD}', title: '文档查询', desc: '"公司2025年的营收是多少？"' },
                        { icon: '\u{1F50D}', title: '内容检索', desc: '"查找关于产品架构的信息"' },
                        { icon: '\u{2696}', title: '对比分析', desc: '"比较两种技术的异同"' },
                        { icon: '\u{1F4CB}', title: '列举总结', desc: '"列出所有产品功能点"' },
                      ].map((item) => (
                        <div
                          key={item.title}
                          className="p-3 rounded-lg border hover:border-primary/50 cursor-pointer transition-colors"
                          onClick={() => {
                            setInputMessage(item.desc.replace(/"/g, ''))
                            inputRef.current?.focus()
                          }}
                        >
                          <div className="font-medium text-sm">{item.icon} {item.title}</div>
                          <div className="text-xs text-muted-foreground mt-1 line-clamp-1">{item.desc}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              ) : (
                /* 消息列表 */
                messages.map((msg) => (
                  <div
                    key={msg.id}
                    className={cn(
                      "flex gap-3 animate-fade-in",
                      msg.role === 'user' ? 'justify-end' : 'justify-start'
                    )}
                  >
                    {/* 头像 */}
                    <Avatar className={cn("shrink-0", msg.role === 'user' ? 'order-2' : '')}>
                      <AvatarFallback className={
                        msg.role === 'user'
                          ? 'bg-blue-100 text-blue-600 dark:bg-blue-900 dark:text-blue-100'
                          : 'bg-green-100 text-green-600 dark:bg-green-900 dark:text-green-100'
                      }>
                        {msg.role === 'user' ? <User className="h-5 w-5" /> : <Bot className="h-5 w-5" />}
                      </AvatarFallback>
                    </Avatar>

                    {/* 消息内容 */}
                    <div className={cn(
                      "max-w-[80%] rounded-2xl px-4 py-3",
                      msg.role === 'user'
                        ? 'bg-primary text-primary-foreground rounded-br-md'
                        : 'bg-muted rounded-bl-md'
                    )}>
                      {/* 加载中状态 */}
                      {msg.isLoading ? (
                        <div className="flex items-center gap-2 py-2">
                          <Loader2 className="h-4 w-4 animate-spin" />
                          <span className="text-sm">正在思考...</span>
                        </div>
                      ) : (
                        <>
                      {/* 消息图片 */}
                      {msg.images && msg.images.length > 0 && (
                        <div className="flex flex-wrap gap-2 mb-2">
                          {msg.images.map((img) => (
                            <img key={img.id} src={img.dataUrl} alt={img.name}
                              className="max-h-40 max-w-[200px] rounded-lg border object-contain cursor-pointer"
                              onClick={() => window.open(img.dataUrl, '_blank')} />
                          ))}
                        </div>
                      )}

                      {/* 消息文本 */}
                      <div className="prose prose-sm dark:prose-invert max-w-none whitespace-pre-wrap">
                        {msg.content}
                      </div>

                          {/* 工具使用标签 */}
                          {msg.toolsUsed && msg.toolsUsed.length > 0 && msg.role === 'assistant' && (
                            <div className="mt-3 pt-3 border-t border-current/10 space-y-2">
                              <div className="flex flex-wrap gap-1.5">
                                {msg.toolsUsed.map((tool, idx) => (
                                  <Badge key={idx} variant="outline" className="text-xs">
                                    {tool}
                                  </Badge>
                                ))}
                              </div>
                            </div>
                          )}
                        </>
                      )}

                      {/* 操作按钮 */}
                      {!msg.isLoading && msg.role === 'assistant' && msg.content && (
                        <div className="mt-2 flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                          <Button variant="ghost" size="icon" className="h-7 w-7" title="复制">
                            <Copy className="h-3 w-3" />
                          </Button>
                          <Button variant="ghost" size="icon" className="h-7 w-7 text-green-600" title="有用">
                            <ThumbsUp className="h-3 w-3" />
                          </Button>
                          <Button variant="ghost" size="icon" className="h-7 w-7 text-red-600" title="无帮助">
                            <ThumbsDown className="h-3 w-3" />
                          </Button>
                        </div>
                      )}
                    </div>
                  </div>
                ))
              )}

              {/* 正在输入指示器 */}
              {isLoading && (
                <div className="flex gap-3 animate-fade-in">
                  <Avatar className="shrink-0">
                    <AvatarFallback className="bg-green-100 text-green-600">
                      <Bot className="h-5 w-5" />
                    </AvatarFallback>
                  </Avatar>
                  <div className="bg-muted rounded-2xl rounded-bl-md px-4 py-3">
                    <div className="flex gap-1.5">
                      {[0, 1, 2].map((i) => (
                        <div
                          key={i}
                          className="w-2 h-2 bg-muted-foreground/50 rounded-full animate-bounce"
                          style={{ animationDelay: `${i * 150}ms` }}
                        />
                      ))}
                    </div>
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>
          </ScrollArea>

          {/* 输入区域 */}
          <div className="border-t p-4">
            <form onSubmit={(e) => { e.preventDefault(); handleSend(); }} className="flex flex-col gap-2">
              {/* 图片预览行：有图片时显示，无图片时隐藏 */}
              {pendingImages.length > 0 && (
                <div className="flex flex-wrap gap-2 px-1">
                  {pendingImages.map((img) => (
                    <div key={img.id} className="relative w-16 h-16 group">
                      <img src={img.dataUrl} alt={img.name}
                        className="w-full h-full object-cover rounded-lg border" />
                      <button type="button" onClick={() => setPendingImages(p => p.filter(i => i.id !== img.id))}
                        className="absolute -top-1.5 -right-1.5 bg-destructive text-white rounded-full w-5 h-5 flex items-center justify-center
                                   opacity-0 group-hover:opacity-100 transition-opacity"
                        aria-label={`删除图片 ${img.name}`}>
                        <X className="h-3 w-3" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              {/* 输入行：加号按钮 + 输入框 + 发送按钮 */}
              <div className="flex gap-2 items-center">
                <ChatImageUpload images={pendingImages} onImagesChange={setPendingImages} />
                <Input
                  ref={inputRef}
                  type="text"
                  placeholder={activeCollectionName ? "输入问题，支持粘贴图片..." : "输入问题（通用问答模式，支持图片）"}
                  value={inputMessage}
                  onChange={(e) => setInputMessage(e.target.value)}
                  onKeyDown={handleKeyDown}
                  onCompositionStart={() => setIsComposing(true)}
                  onCompositionEnd={() => setIsComposing(false)}
                  disabled={isLoading || isStreaming}
                  className="flex-1 resize-none"
                  autoFocus
                />

                {isLoading || isStreaming ? (
                  <Button type="button" variant="destructive" size="icon" onClick={() => { /* TODO: 取消生成 */ }}>
                    <Square className="h-4 w-4 fill-current" />
                  </Button>
                ) : (
                  <Button
                    type="submit"
                    size="icon"
                    disabled={!inputMessage.trim() && pendingImages.length === 0}
                  >
                    <Send className="h-4 w-4" />
                  </Button>
                )}
              </div>
            </form>

            <p className="text-xs text-muted-foreground text-center mt-2">
              按 Enter 发送 · Shift+Enter 换行 · 支持图文混合输入
            </p>
          </div>
        </Card>
      </div>
    </div>
  )
}

export default Chat
