import { useState, useEffect } from 'react'
import { X, FileText, File, Download } from 'lucide-react'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ScrollArea } from '@/components/ui/scroll-area'
import { formatDate } from '@/lib/utils'

export interface DocumentPreviewProps {
  document: {
    id: string
    filename: string
    file_path: string
    chunk_count: number
    embedding_count: number
    success: boolean
    error_message?: string | null
    uploaded_at: string
  } | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function DocumentPreview({ document, open, onOpenChange }: DocumentPreviewProps) {
  const [content, setContent] = useState<string>('')
  const [isLoading, setIsLoading] = useState(false)
  const [activeTab, setActiveTab] = useState('info')

  useEffect(() => {
    if (!document || !open) {
      setContent('')
      return
    }

    // 尝试加载文本内容（仅支持文本类文件）
    const ext = document.filename.split('.').pop()?.toLowerCase() || ''
    const textExts = ['txt', 'md', 'markdown', 'json', 'xml', 'csv', 'html', 'htm', 'log']
    if (textExts.includes(ext)) {
      setIsLoading(true)
      fetch(`/api/v1/kquery/documents/download/${encodeURIComponent(document.file_path)}`)
        .then((res) => {
          if (res.ok) return res.text()
          throw new Error('无法加载文件内容')
        })
        .then((text) => {
          setContent(text)
          setIsLoading(false)
          setActiveTab('content')
        })
        .catch(() => {
          setContent('文件内容加载失败，请直接下载查看。')
          setIsLoading(false)
        })
    }
  }, [document, open])

  if (!document) return null

  const isText = content.length > 0
  const ext = document.filename.split('.').pop()?.toLowerCase() || ''
  const fileType =
    ext === 'pdf'
      ? 'PDF 文档'
      : ext === 'docx' || ext === 'doc'
      ? 'Word 文档'
      : ext === 'xlsx' || ext === 'xls'
      ? 'Excel 表格'
      : ext === 'pptx' || ext === 'ppt'
      ? 'PPT 演示'
      : ext === 'txt'
      ? '文本文件'
      : ext === 'md' || ext === 'markdown'
      ? 'Markdown 文档'
      : ext === 'html' || ext === 'htm'
      ? 'HTML 网页'
      : ext === 'json'
      ? 'JSON 数据'
      : ext === 'xml'
      ? 'XML 数据'
      : ext === 'csv'
      ? 'CSV 表格'
      : '未知类型'

  const status = document.error_message
    ? { label: '失败', color: 'destructive' as const }
    : document.chunk_count > 0
    ? { label: '已完成', color: 'success' as const }
    : { label: '处理中', color: 'warning' as const }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl h-[80vh] flex flex-col p-0">
        <DialogHeader className="px-6 pt-6 pb-2">
          <DialogTitle className="flex items-center gap-2 text-lg">
            <FileText className="h-5 w-5 text-primary" />
            <span className="truncate max-w-[400px]" title={document.filename}>
              {document.filename}
            </span>
            <Badge variant={status.color} className="ml-2">
              {status.label}
            </Badge>
          </DialogTitle>
        </DialogHeader>

        <Tabs value={activeTab} onValueChange={setActiveTab} className="flex-1 flex flex-col">
          <TabsList className="mx-6">
            <TabsTrigger value="info">基本信息</TabsTrigger>
            {isText && <TabsTrigger value="content">文件内容</TabsTrigger>}
            <TabsTrigger value="chunks">分块信息</TabsTrigger>
          </TabsList>

          <TabsContent value="info" className="flex-1 m-0 px-6 pb-6">
            <ScrollArea className="h-full">
              <div className="space-y-4 pt-2">
                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-1">
                    <p className="text-sm text-muted-foreground">文件类型</p>
                    <p className="text-sm font-medium">{fileType}</p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm text-muted-foreground">文件路径</p>
                    <p className="text-sm font-medium truncate" title={document.file_path}>
                      {document.file_path}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm text-muted-foreground">分块数量</p>
                    <p className="text-sm font-medium">{document.chunk_count}</p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm text-muted-foreground">向量数量</p>
                    <p className="text-sm font-medium">{document.embedding_count}</p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm text-muted-foreground">上传时间</p>
                    <p className="text-sm font-medium">{formatDate(document.uploaded_at)}</p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm text-muted-foreground">文档 ID</p>
                    <p className="text-sm font-medium font-mono">{document.id}</p>
                  </div>
                </div>

                {document.error_message && (
                  <div className="rounded-md bg-destructive/10 p-3 border border-destructive/20">
                    <p className="text-sm text-destructive font-medium">处理错误</p>
                    <p className="text-sm text-destructive/80 mt-1">{document.error_message}</p>
                  </div>
                )}

                <div className="flex gap-2 pt-2">
                  <Button variant="outline" size="sm" asChild>
                    <a
                      href={`/api/v1/kquery/documents/download/${encodeURIComponent(document.file_path)}`}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      <Download className="mr-2 h-4 w-4" />
                      下载原文件
                    </a>
                  </Button>
                </div>
              </div>
            </ScrollArea>
          </TabsContent>

          {isText && (
            <TabsContent value="content" className="flex-1 m-0 px-6 pb-6">
              <ScrollArea className="h-full">
                {isLoading ? (
                  <div className="py-12 text-center text-muted-foreground">加载中...</div>
                ) : (
                  <pre className="text-sm font-mono whitespace-pre-wrap break-words bg-muted p-4 rounded-md min-h-[200px]">
                    {content}
                  </pre>
                )}
              </ScrollArea>
            </TabsContent>
          )}

          <TabsContent value="chunks" className="flex-1 m-0 px-6 pb-6">
            <ScrollArea className="h-full">
              <div className="space-y-2 pt-2">
                {document.chunk_count > 0 ? (
                  Array.from({ length: Math.min(document.chunk_count, 50) }).map((_, i) => (
                    <div
                      key={i}
                      className="flex items-center gap-3 p-3 rounded-md border bg-card hover:bg-accent/50 transition-colors"
                    >
                      <File className="h-4 w-4 text-muted-foreground" />
                      <div className="flex-1">
                        <p className="text-sm font-medium">Chunk {i + 1}</p>
                        <p className="text-xs text-muted-foreground">向量 ID: {document.id}_chunk_{i}</p>
                      </div>
                      <Badge variant="outline" className="text-xs">
                        已嵌入
                      </Badge>
                    </div>
                  ))
                ) : (
                  <div className="py-12 text-center text-muted-foreground">
                    <FileText className="mx-auto h-10 w-10 mb-3 opacity-50" />
                    <p>暂无分块信息</p>
                    <p className="text-sm mt-1">文档正在处理中，请稍后刷新</p>
                  </div>
                )}
              </div>
            </ScrollArea>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  )
}

export default DocumentPreview
