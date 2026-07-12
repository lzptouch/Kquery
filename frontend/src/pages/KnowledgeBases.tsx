import React, { useEffect, useState } from 'react'
import {
  Database,
  Plus,
  Search,
  MoreVertical,
  Trash2,
  FileText,
  MessageSquare,
  AlertTriangle,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useKbStore } from '@/stores/useKbStore'
import { useChatStore } from '@/stores/useChatStore'

/**
 * 知识库管理页面
 */
export function KnowledgeBases() {
  const {
    knowledgeBases,
    isLoading,
    error,
    fetchKnowledgeBases,
    createKnowledgeBase,
    deleteKnowledgeBase,
    setCurrentKbId,
    clearError,
  } = useKbStore()

  const { setActiveCollection } = useChatStore()

  // UI状态
  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [newKbName, setNewKbName] = useState('')
  const [newKbDesc, setNewKbDesc] = useState('')

  // 加载数据
  useEffect(() => {
    fetchKnowledgeBases()
  }, [fetchKnowledgeBases])

  // 处理创建知识库
  const handleCreate = async () => {
    if (!newKbName.trim()) return

    try {
      await createKnowledgeBase({
        name: newKbName.trim(),
        description: newKbDesc.trim() || undefined,
      })

      setShowCreateDialog(false)
      setNewKbName('')
      setNewKbDesc('')
    } catch (err) {
      console.error('Create KB failed:', err)
    }
  }

  // 处理删除
  const handleDelete = async (id: string) => {
    if (!confirm('确定要删除此知识库吗？此操作将清空集合中的所有数据，不可恢复！')) return

    try {
      await deleteKnowledgeBase(id)
    } catch (err) {
      console.error('Delete KB failed:', err)
    }
  }

  // 处理选择知识库进行对话
  const handleChatWithKb = (kbName: string) => {
    setCurrentKbId(kbName)
    setActiveCollection(kbName)
    window.location.href = '/chat'
  }

  // 过滤搜索结果
  const filteredKbs = knowledgeBases.filter((kb) =>
    kb.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
    (kb.description && kb.description.toLowerCase().includes(searchQuery.toLowerCase()))
  )

  return (
    <div className="space-y-6 animate-fade-in">
      {/* 页面标题和操作栏 */}
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">知识库管理</h1>
          <p className="text-muted-foreground mt-1">
            管理和组织您的企业知识资产
          </p>
        </div>

        <Button onClick={() => setShowCreateDialog(true)}>
          <Plus className="mr-2 h-4 w-4" />
          创建知识库
        </Button>
      </div>

      {/* 搜索栏 */}
      <div className="relative max-w-md">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
        <Input
          type="search"
          placeholder="搜索知识库..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="pl-9"
        />
      </div>

      {/* 错误提示 */}
      {error && (
        <div className="rounded-lg border px-4 py-3 bg-destructive/10 border-destructive/50 text-destructive flex items-center justify-between">
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4" />
            {error}
          </div>
          <Button variant="ghost" size="sm" onClick={clearError}>关闭</Button>
        </div>
      )}

      {/* 知识库卡片列表 */}
      {isLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {[1, 2, 3, 4, 5, 6].map((i) => (
            <Card key={i} className="animate-pulse">
              <CardContent className="pt-6">
                <div className="h-5 bg-muted rounded w-1/2 mb-3" />
                <div className="h-3 bg-muted rounded w-full mb-6" />
                <div className="flex justify-between">
                  <div className="h-8 bg-muted rounded w-20" />
                  <div className="h-8 bg-muted rounded w-8" />
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {filteredKbs.map((kb) => (
            <Card key={kb.id} className="hover:shadow-lg transition-all group">
              <CardHeader>
                <div className="flex items-start justify-between">
                  <div className="space-y-1 flex-1 min-w-0">
                    <CardTitle className="text-lg truncate">{kb.name}</CardTitle>
                    <CardDescription className="line-clamp-2 h-10">
                      {kb.description || '暂无描述'}
                    </CardDescription>
                  </div>

                  {/* 操作菜单 */}
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="ghost" size="icon" className="opacity-0 group-hover:opacity-100 transition-opacity">
                        <MoreVertical className="h-4 w-4" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem onClick={() => handleChatWithKb(kb.name)}>
                        <MessageSquare className="mr-2 h-4 w-4" />对话
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        onClick={() => handleDelete(kb.id)}
                        className="text-destructive focus:text-destructive"
                      >
                        <Trash2 className="mr-2 h-4 w-4" />删除
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
              </CardHeader>

              <CardContent>
                {/* 统计信息 */}
                <div className="flex items-center gap-4 text-sm text-muted-foreground mb-4">
                  <span className="flex items-center gap-1">
                    <FileText className="h-4 w-4" />
                    {kb.document_count || 0} 文档
                  </span>
                  <span className="flex items-center gap-1">
                    <Database className="h-4 w-4" />
                    {kb.chunk_count || 0} 块
                  </span>
                </div>

                {/* 状态标签 */}
                <Badge
                  variant={
                    kb.status === 'active' ? 'success' : 'secondary'
                  }
                >
                  {kb.status === 'active' ? '正常' : '空'}
                </Badge>

                {/* 快速操作按钮 */}
                <div className="mt-4 pt-4 border-t flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    className="flex-1"
                    onClick={() => {
                      setCurrentKbId(kb.id)
                      window.location.href = `/documents`
                    }}
                  >
                    管理文档
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    className="flex-1"
                    onClick={() => handleChatWithKb(kb.name)}
                  >
                    开始对话
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}

          {/* 空状态 */}
          {filteredKbs.length === 0 && (
            <div className="col-span-full py-12 text-center">
              <Database className="mx-auto h-12 w-12 text-muted-foreground/50 mb-4" />
              <h3 className="text-lg font-medium">暂无知识库</h3>
              <p className="text-sm text-muted-foreground mt-1">
                点击上方按钮创建第一个知识库
              </p>
            </div>
          )}
        </div>
      )}

      {/* 创建知识库对话框 */}
      <Dialog open={showCreateDialog} onOpenChange={setShowCreateDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>创建新知识库</DialogTitle>
            <DialogDescription>
              创建一个新的知识库用于组织和管理相关文档
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <label htmlFor="kb-name" className="text-sm font-medium">
                名称 *
              </label>
              <Input
                id="kb-name"
                placeholder="例如：产品技术文档"
                value={newKbName}
                onChange={(e) => setNewKbName(e.target.value)}
                autoFocus
              />
              <p className="text-xs text-muted-foreground">
                名称将作为Milvus集合名，建议使用英文或下划线
              </p>
            </div>

            <div className="space-y-2">
              <label htmlFor="kb-desc" className="text-sm font-medium">
                描述
              </label>
              <Input
                id="kb-desc"
                placeholder="简要描述这个知识库的用途"
                value={newKbDesc}
                onChange={(e) => setNewKbDesc(e.target.value)}
              />
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setShowCreateDialog(false)}>
              取消
            </Button>
            <Button
              onClick={handleCreate}
              disabled={!newKbName.trim()}
            >
              创建
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default KnowledgeBases
