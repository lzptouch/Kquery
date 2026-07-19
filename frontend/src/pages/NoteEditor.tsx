/**
 * 笔记编辑器页面 (P0-2)
 * AI 写作：续写、润色、总结、翻译、标题生成、大纲
 */

import React, { useState, useEffect, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  Save,
  ArrowLeft,
  Wand2,
  PenLine,
  Sparkles,
  FileText,
  ListChecks,
  Languages,
  Loader2,
  Database,
  BookOpen,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { useNoteStore } from '@/stores/useNoteStore'
import { useKbStore } from '@/stores/useKbStore'
import type { AIAction } from '@/services/noteService'

const AI_ACTIONS: { value: AIAction; label: string; icon: React.ReactNode }[] = [
  { value: 'continue', label: '续写', icon: <PenLine className="h-4 w-4" /> },
  { value: 'polish', label: '润色', icon: <Sparkles className="h-4 w-4" /> },
  { value: 'summarize', label: '总结', icon: <FileText className="h-4 w-4" /> },
  { value: 'outline', label: '大纲', icon: <ListChecks className="h-4 w-4" /> },
  { value: 'translate', label: '翻译', icon: <Languages className="h-4 w-4" /> },
  { value: 'title', label: '标题', icon: <Wand2 className="h-4 w-4" /> },
]

export function NoteEditorPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const {
    currentNote,
    isLoading,
    aiResult,
    isAiStreaming,
    error,
    getNote,
    updateNote,
    aiAction,
    aiActionStream,
    ingestToKb,
    clearError,
  } = useNoteStore()

  const { knowledgeBases, fetchKnowledgeBases } = useKbStore()

  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [selectedAction, setSelectedAction] = useState<AIAction>('polish')
  const [actionContext, setActionContext] = useState('')
  const [targetLanguage, setTargetLanguage] = useState('en')
  const [saving, setSaving] = useState(false)
  const [targetKb, setTargetKb] = useState('')

  useEffect(() => {
    if (id) getNote(id)
  }, [id])

  useEffect(() => {
    if (currentNote) {
      setTitle(currentNote.title)
      setContent(currentNote.content)
    }
  }, [currentNote])

  const handleSave = async () => {
    if (!id) return
    setSaving(true)
    await updateNote(id, { title, content })
    setSaving(false)
  }

  const handleAiAction = async () => {
    if (!id || !content.trim()) return
    if (selectedAction === 'translate') {
      await aiActionStream(id, selectedAction, content)
    } else {
      await aiAction(id, selectedAction, content)
    }
  }

  const handleIngestToKb = async () => {
    if (!id || !targetKb) return
    await ingestToKb(id, targetKb)
  }

  // 键盘快捷键：Ctrl/Cmd+S 保存
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 's') {
        e.preventDefault()
        handleSave()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [title, content, id])

  return (
    <div className="space-y-4 max-w-4xl mx-auto">
      {/* 顶部工具栏 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" onClick={() => navigate('/notes')}>
            <ArrowLeft className="h-5 w-5" />
          </Button>
          <h1 className="text-xl font-bold">笔记编辑</h1>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" onClick={handleSave} disabled={saving}>
            <Save className="h-4 w-4 mr-2" />
            {saving ? '保存中...' : '保存'}
          </Button>
        </div>
      </div>

      {error && (
        <div className="bg-destructive/10 text-destructive px-4 py-3 rounded-lg text-sm flex justify-between items-center">
          <span>{error}</span>
          <Button variant="ghost" size="sm" onClick={clearError}>关闭</Button>
        </div>
      )}

      {/* 标题输入 */}
      <Input
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="笔记标题"
        className="text-lg font-semibold border-none px-0 text-2xl focus-visible:ring-0"
      />

      {/* 正文编辑 */}
      <Textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        placeholder="在这里开始写作... 选中文字后可使用 AI 润色、续写等功能"
        className="min-h-[400px] resize-y border-border/50 focus-visible:ring-primary"
      />

      {/* AI 操作栏 */}
      <Card>
        <CardContent className="pt-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex-1 min-w-[120px]">
              <Select value={selectedAction} onValueChange={(v) => setSelectedAction(v as AIAction)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {AI_ACTIONS.map((a) => (
                    <SelectItem key={a.value} value={a.value}>
                      <span className="flex items-center gap-2">
                        {a.icon}
                        {a.label}
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {selectedAction === 'translate' && (
              <Select value={targetLanguage} onValueChange={setTargetLanguage}>
                <SelectTrigger className="w-28">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="en">英文</SelectItem>
                  <SelectItem value="zh">中文</SelectItem>
                  <SelectItem value="ja">日文</SelectItem>
                  <SelectItem value="ko">韩文</SelectItem>
                </SelectContent>
              </Select>
            )}

            <Button
              onClick={handleAiAction}
              disabled={isLoading || isAiStreaming || !content.trim()}
              className="bg-gradient-to-r from-violet-500 to-indigo-500 hover:from-violet-600 hover:to-indigo-600 text-white"
            >
              {isLoading || isAiStreaming ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <Wand2 className="h-4 w-4 mr-2" />
              )}
              {isAiStreaming ? '生成中...' : `AI ${AI_ACTIONS.find((a) => a.value === selectedAction)?.label}`}
            </Button>
          </div>

          {/* AI 结果展示 */}
          {(aiResult || isAiStreaming) && (
            <div className="mt-4 p-4 bg-accent/50 rounded-lg border">
              <div className="flex items-center gap-2 mb-2">
                <Sparkles className="h-4 w-4 text-indigo-500" />
                <span className="text-sm font-medium text-muted-foreground">AI 生成结果</span>
                {!isAiStreaming && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="ml-auto text-xs"
                    onClick={() => {
                      setContent(aiResult)
                      useNoteStore.setState({ aiResult: '' })
                    }}
                  >
                    采用
                  </Button>
                )}
              </div>
              <div className="text-sm whitespace-pre-wrap leading-relaxed">
                {aiResult || (isAiStreaming && <Loader2 className="h-4 w-4 animate-spin" />)}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* 入库操作 */}
      <Card>
        <CardContent className="pt-4">
          <div className="flex items-center gap-3">
            <Database className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm font-medium">入库到知识库</span>
            <Select value={targetKb} onValueChange={setTargetKb}>
              <SelectTrigger className="w-48">
                <SelectValue placeholder="选择知识库" />
              </SelectTrigger>
              <SelectContent>
                {knowledgeBases.map((kb: any) => (
                  <SelectItem key={kb.id || kb.name} value={kb.name || kb.id}>
                    {kb.name || kb.id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              variant="outline"
              size="sm"
              onClick={handleIngestToKb}
              disabled={!targetKb || !id}
            >
              <BookOpen className="h-4 w-4 mr-2" />
              入库
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

export default NoteEditorPage
