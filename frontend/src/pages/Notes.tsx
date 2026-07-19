/**
 * AI 写作笔记页面 (P0-2)
 */

import React, { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Plus,
  Search,
  FileText,
  Trash2,
  Edit3,
  Sparkles,
  BookOpen,
  MoreHorizontal,
  Loader2,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { ScrollArea } from '@/components/ui/scroll-area'
import { useNoteStore } from '@/stores/useNoteStore'
import { Skeleton } from '@/components/ui/skeleton'
import type { Note } from '@/services/noteService'

export function NotesPage() {
  const navigate = useNavigate()
  const { notes, isLoading, error, fetchNotes, deleteNote, createNote } = useNoteStore()
  const [searchTerm, setSearchTerm] = useState('')
  const [newTitle, setNewTitle] = useState('')

  const noteList = Array.isArray(notes) ? notes : []

  useEffect(() => {
    fetchNotes()
  }, [fetchNotes])

  const filteredNotes = noteList.filter(
    (n) =>
      n?.title?.toLowerCase().includes(searchTerm.toLowerCase()) ||
      (n?.tags || []).some((t) => t.toLowerCase().includes(searchTerm.toLowerCase()))
  )

  const handleCreateNote = async () => {
    const title = newTitle.trim() || '未命名笔记'
    const note = await createNote({ title, content: '' })
    if (note) {
      setNewTitle('')
      navigate(`/notes/${note.id}`)
    }
  }

  const handleDeleteNote = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation()
    if (confirm('确定删除这条笔记吗？')) {
      await deleteNote(id)
    }
  }

  const formatDate = (dateStr: string) => {
    const d = new Date(dateStr)
    return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  }

  return (
    <div className="space-y-6">
      {/* 标题栏 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">AI 写作笔记</h1>
          <p className="text-sm text-muted-foreground mt-1">
            智能写作助手：续写、润色、总结、翻译
          </p>
        </div>
      </div>

      {/* 工具栏 */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1 max-w-sm">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="搜索笔记..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="pl-9"
          />
        </div>
        <div className="flex items-center gap-2 flex-1">
          <Input
            placeholder="输入笔记标题..."
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleCreateNote()}
            className="max-w-xs"
          />
          <Button onClick={handleCreateNote} disabled={isLoading}>
            <Plus className="h-4 w-4 mr-2" />
            新建笔记
          </Button>
        </div>
      </div>

      {error && (
        <div className="bg-destructive/10 text-destructive px-4 py-3 rounded-lg text-sm">{error}</div>
      )}

      {/* 笔记列表 */}
      {isLoading && notes.length === 0 ? (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {[1, 2, 3].map((i) => (
            <Card key={i}>
              <CardHeader><Skeleton className="h-5 w-3/4" /></CardHeader>
              <CardContent><Skeleton className="h-16" /></CardContent>
            </Card>
          ))}
        </div>
      ) : filteredNotes.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
          <FileText className="h-12 w-12 mb-4" />
          <p className="text-lg font-medium">暂无笔记</p>
          <p className="text-sm mt-1">创建你的第一篇 AI 写作笔记</p>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {filteredNotes.map((note) => (
            <Card
              key={note.id}
              className="group cursor-pointer hover:shadow-md transition-shadow hover:border-primary/50"
              onClick={() => navigate(`/notes/${note.id}`)}
            >
              <CardHeader className="pb-2">
                <div className="flex items-start justify-between">
                  <CardTitle className="text-base font-semibold line-clamp-1">
                    {note.title || '未命名笔记'}
                  </CardTitle>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 opacity-0 group-hover:opacity-100 transition-opacity -mr-2"
                    onClick={(e) => handleDeleteNote(note.id, e)}
                  >
                    <Trash2 className="h-4 w-4 text-muted-foreground hover:text-destructive" />
                  </Button>
                </div>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground line-clamp-3 min-h-[3rem]">
                  {note.content || '空笔记，点击开始编辑'}
                </p>
                <div className="flex items-center gap-2 mt-3">
                  <span className="text-xs text-muted-foreground">{formatDate(note.updated_at)}</span>
                  {note.tags && note.tags.length > 0 && (
                    <div className="flex gap-1">
                      {note.tags.slice(0, 2).map((tag) => (
                        <Badge key={tag} variant="secondary" className="text-xs">
                          {tag}
                        </Badge>
                      ))}
                    </div>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}

export default NotesPage
