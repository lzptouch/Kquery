import React, { useState, useEffect, useCallback } from 'react'
import { Upload, Search, Filter, Trash2, Eye, MoreVertical, FileText, Network } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { formatDate } from '@/lib/utils'
import { useDocumentStore } from '@/stores/useDocumentStore'
import { useKbStore } from '@/stores/useKbStore'
import { DocumentPreview } from '@/components/DocumentPreview'
import type { Document } from '@/stores/useDocumentStore'

export function Documents() {
  const {
    documents,
    isLoading: docsLoading,
    isUploading,
    selectedDocumentIds,
    fetchDocuments,
    uploadFiles,
    deleteDocument,
    batchDeleteDocuments,
    toggleDocumentSelection,
    clearSelection,
    clearError,
    error,
  } = useDocumentStore()

  const { knowledgeBases, currentKbId, fetchKnowledgeBases, isLoading: kbLoading } = useKbStore()

  // 加载知识库和文档
  useEffect(() => {
    fetchKnowledgeBases()
  }, [])

  useEffect(() => {
    if (currentKbId) {
      fetchDocuments(currentKbId)
    }
  }, [currentKbId])

  const handleFileUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files
    if (!files || files.length === 0) return
    const collectionName = currentKbId || 'default'
    try {
      await uploadFiles(Array.from(files), collectionName)
      fetchDocuments(collectionName)
    } catch (err) {
      console.error('Upload failed:', err)
    }
    event.target.value = ''
  }

  const handleBatchDelete = async () => {
    if (selectedDocumentIds.size === 0) return
    if (!confirm(`确定要移除选中的 ${selectedDocumentIds.size} 个文档记录吗？`)) return
    batchDeleteDocuments(Array.from(selectedDocumentIds))
  }

  const handleBuildKG = async () => {
    if (!currentKbId && !currentKb) return
    setIsBuildingKG(true)
    setKgBuildResult('')
    try {
      const collectionName = currentKb?.name || currentKbId || 'default'
      const body: any = {
        collection_name: collectionName,
        doc_limit: 50,
      }
      // 如果用户勾选了文档，则只构建选中文档对应的 chunk
      if (selectedDocumentIds.size > 0) {
        body.document_ids = Array.from(selectedDocumentIds)
      }
      const res = await fetch('/api/v1/graph/build-from-collection', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        throw new Error(errData.detail || `请求失败 (${res.status})`)
      }
      const data = await res.json()
      if (data.success) {
        const kg = data.data
        if (kg.entities_created || kg.relations_created) {
          setKgBuildResult(
            `KG构建成功! 实体: ${kg.entities_created || 0}, 关系: ${kg.relations_created || 0}` +
            ` (使用了 ${kg.text_chunks_used || 0} 个文本块)`
          )
        } else if (kg.entities_updated) {
          setKgBuildResult(
            `KG构建完成，无新增实体/关系（更新 ${kg.entities_updated} 个实体）。` +
            `可能原因：重复构建或文本中无可识别关系。`
          )
        } else {
          setKgBuildResult(
            `KG构建完成，但未抽取到实体/关系。可能原因：文本中无可识别实体或 LLM 未配置。` +
            `${data.error ? '详情：' + data.error : ''}`
          )
        }
        // 刷新 KG 状态
        checkKgStatus(collectionName)
      } else {
        setKgBuildResult(`KG构建异常: ${data.error || '请查看后端日志'}`)
      }
    } catch (err: any) {
      setKgBuildResult(`KG构建错误: ${err.message}`)
    } finally {
      setIsBuildingKG(false)
    }
  }

  const [searchQuery, setSearchQuery] = useState('')
  const [previewDoc, setPreviewDoc] = useState<Document | null>(null)
  const [isBuildingKG, setIsBuildingKG] = useState(false)
  const [kgBuildResult, setKgBuildResult] = useState<string>('')
  const [kgStatus, setKgStatus] = useState<{ kg_built: boolean; entity_count: number; relation_count: number } | null>(null)

  // 检查当前知识库的 KG 构建状态
  const checkKgStatus = useCallback(async (collectionName: string) => {
    try {
      const res = await fetch(`/api/v1/graph/check-collection?collection_name=${encodeURIComponent(collectionName)}`)
      const data = await res.json()
      if (data.success) setKgStatus(data.data)
    } catch {
      setKgStatus(null)
    }
  }, [])

  // 当知识库切换时更新 KG 状态
  useEffect(() => {
    if (currentKbId) {
      const cn = knowledgeBases.find((kb) => kb.id === currentKbId)?.name || currentKbId
      checkKgStatus(cn)
    }
  }, [currentKbId, knowledgeBases])

  const filteredDocs = documents.filter((doc) =>
    doc.filename.toLowerCase().includes(searchQuery.toLowerCase())
  )

  const getStatusInfo = (doc: typeof documents[0]) => {
    if (doc.error_message) {
      return { label: '失败', color: 'destructive' as const }
    }
    if (doc.chunk_count > 0) {
      return { label: '已完成', color: 'success' as const }
    }
    return { label: '处理中', color: 'warning' as const }
  }

  const currentKb = knowledgeBases.find((kb) => kb.id === currentKbId)

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">文档管理</h1>
          <p className="text-muted-foreground mt-1">
            知识库: {currentKb ? currentKb.name : '加载中...'}
            {kgStatus && (
              <span className={`ml-3 inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${
                kgStatus.kg_built
                  ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-400'
                  : 'bg-muted text-muted-foreground'
              }`}>
                <span className={`inline-block w-1.5 h-1.5 rounded-full ${kgStatus.kg_built ? 'bg-green-500' : 'bg-gray-400'}`} />
                KG: {kgStatus.kg_built
                  ? `${kgStatus.entity_count}实体 / ${kgStatus.relation_count}关系`
                  : '未构建'}
              </span>
            )}
            <Button variant="ghost" size="sm" onClick={fetchKnowledgeBases} className="ml-2">
              刷新
            </Button>
          </p>
        </div>

        <div className="flex gap-2">
          {selectedDocumentIds.size > 0 && (
            <Button variant="outline" onClick={handleBatchDelete}>
              <Trash2 className="mr-2 h-4 w-4" />
              移除选中 ({selectedDocumentIds.size})
            </Button>
          )}
          <label className="cursor-pointer inline-flex items-center justify-center rounded-md text-sm font-medium ring-offset-background transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 bg-primary text-primary-foreground hover:bg-primary/90 h-10 px-4 py-2">
            <Upload className="mr-2 h-4 w-4" />
            {isUploading ? '上传中...' : '上传文档'}
            <input
              type="file"
              multiple
              accept=".pdf,.doc,.docx,.txt,.md,.html,.xlsx,.xls,.pptx,.ppt"
              onChange={handleFileUpload}
              disabled={isUploading || !currentKbId}
              className="hidden"
            />
          </label>
          <Button variant="secondary" onClick={handleBuildKG} disabled={isBuildingKG || !currentKbId}>
            <Network className="mr-2 h-4 w-4" />
            {isBuildingKG ? '构建中...' : '构建知识图谱'}
          </Button>
        </div>
      </div>

      {kgBuildResult && (
        <div className={`px-4 py-2 rounded-md text-sm ${kgBuildResult.includes('成功') ? 'bg-green-50 text-green-700 dark:bg-green-900/30 dark:text-green-400' : 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'}`}>
          {kgBuildResult}
        </div>
      )}

      {error && (
        <Alert variant="destructive" className="flex items-center justify-between">
          <AlertDescription>{error}</AlertDescription>
          <Button variant="ghost" size="sm" onClick={clearError}>关闭</Button>
        </Alert>
      )}

      <div className="flex flex-col sm:flex-row gap-4">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            type="search"
            placeholder="搜索文档..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="pl-9"
          />
        </div>
        <Button variant="outline">
          <Filter className="mr-2 h-4 w-4" />
          筛选
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <FileText className="h-4 w-4" />
            已上传文档
            {filteredDocs.length > 0 && (
              <Badge variant="secondary">{filteredDocs.length}</Badge>
            )}
            {docsLoading && <span className="text-sm text-muted-foreground">加载中...</span>}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {kbLoading || docsLoading ? (
            <div className="py-12 text-center text-muted-foreground">加载中...</div>
          ) : filteredDocs.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[50px]">
                    <input
                      type="checkbox"
                      checked={selectedDocumentIds.size === filteredDocs.length && selectedDocumentIds.size > 0}
                      onChange={(e) => {
                        if (e.target.checked) {
                          clearSelection()
                          filteredDocs.forEach((doc) => toggleDocumentSelection(doc.id))
                        } else {
                          clearSelection()
                        }
                      }}
                      className="rounded"
                    />
                  </TableHead>
                  <TableHead>文件名</TableHead>
                  <TableHead>分块数</TableHead>
                  <TableHead>向量数</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>上传时间</TableHead>
                  <TableHead className="w-[80px]">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filteredDocs.map((doc) => {
                  const status = getStatusInfo(doc)
                  return (
                    <TableRow key={doc.id} className={selectedDocumentIds.has(doc.id) ? 'bg-accent/50' : ''}>
                      <TableCell>
                        <input
                          type="checkbox"
                          checked={selectedDocumentIds.has(doc.id)}
                          onChange={() => toggleDocumentSelection(doc.id)}
                          className="rounded"
                        />
                      </TableCell>
                      <TableCell className="font-medium max-w-[300px] truncate">{doc.filename}</TableCell>
                      <TableCell>{doc.chunk_count}</TableCell>
                      <TableCell>{doc.embedding_count}</TableCell>
                      <TableCell>
                        <Badge variant={status.color}>{status.label}</Badge>
                      </TableCell>
                      <TableCell>{formatDate(doc.uploaded_at)}</TableCell>
                      <TableCell>
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button variant="ghost" size="icon" className="h-8 w-8">
                              <MoreVertical className="h-4 w-4" />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            <DropdownMenuItem onClick={() => setPreviewDoc(doc)}>
                              <Eye className="mr-2 h-4 w-4" />预览
                            </DropdownMenuItem>
                            <DropdownMenuItem
                              onClick={() => deleteDocument(doc.id)}
                              className="text-destructive focus:text-destructive"
                            >
                              <Trash2 className="mr-2 h-4 w-4" />移除
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          ) : (
            <div className="py-12 text-center">
              <Upload className="mx-auto h-12 w-12 text-muted-foreground/50 mb-4" />
              <h3 className="text-lg font-medium">暂无文档</h3>
              <p className="text-sm text-muted-foreground mt-1">
                {currentKbId ? '上传文档开始构建知识库' : '请先选择一个知识库'}
              </p>
            </div>
          )}
        </CardContent>
      </Card>
      <DocumentPreview
        document={previewDoc}
        open={!!previewDoc}
        onOpenChange={(open) => { if (!open) setPreviewDoc(null) }}
      />
    </div>
  )
}

export default Documents
