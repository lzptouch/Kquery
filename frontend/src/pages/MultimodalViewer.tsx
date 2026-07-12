import React, { useState } from 'react'
import { ImageIcon, Table, FileText, Search, Loader2, Eye } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useMultimodalTypes, useMultimodalSearch, useMultimodalList } from '@/hooks/useMultimodalSearch'
import type { MultimodalItem } from '@/services/multimodalService'

const chunkTypeIcons: Record<string, React.ComponentType<{ className?: string }>> = {
  image_description: ImageIcon,
  table: Table,
  table_description: FileText,
}

const chunkTypeLabels: Record<string, string> = {
  image_description: '图片描述',
  table: '表格数据',
  table_description: '表格洞察',
}

const chunkTypeColors: Record<string, string> = {
  image_description: 'bg-purple-100 text-purple-700',
  table: 'bg-blue-100 text-blue-700',
  table_description: 'bg-green-100 text-green-700',
}

export default function MultimodalViewer() {
  const [query, setQuery] = useState('')
  const [activeType, setActiveType] = useState<string>('all')
  const { data: typesData } = useMultimodalTypes()
  const searchMutation = useMultimodalSearch()
  const { data: listData, isLoading: listLoading } = useMultimodalList({
    chunk_type: activeType === 'all' ? undefined : activeType,
    limit: 20,
  })

  const types = typesData || []
  const searchResults = searchMutation.data?.results || []
  const isSearching = searchMutation.isPending
  const listResults = listData?.results || []

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    if (!query.trim()) return
    searchMutation.mutate({
      query: query.trim(),
      chunk_types: activeType === 'all'
        ? ['image_description', 'table', 'table_description']
        : [activeType],
      top_k: 12,
    })
  }

  const renderItem = (item: MultimodalItem, index: number) => {
    const Icon = chunkTypeIcons[item.chunk_type] || FileText
    const label = chunkTypeLabels[item.chunk_type] || item.chunk_type
    const colorClass = chunkTypeColors[item.chunk_type] || 'bg-gray-100 text-gray-700'

    return (
      <Card key={`${item.id}-${index}`} className="hover:shadow-md transition-shadow">
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <div className={`p-1.5 rounded-md ${colorClass}`}>
                <Icon className="h-4 w-4" />
              </div>
              <CardTitle className="text-sm font-medium truncate max-w-[200px]">
                {item.source_file}
              </CardTitle>
            </div>
            <Badge variant="secondary" className="text-xs">
              相似度 {(item.score * 100).toFixed(1)}%
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="pt-0">
          <p className="text-sm text-muted-foreground line-clamp-4">
            {item.content}
          </p>
          <div className="flex items-center justify-between mt-3 text-xs text-muted-foreground">
            <span className={colorClass}>{label}</span>
            {item.page_number && <span>第 {item.page_number} 页</span>}
          </div>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-6 animate-fade-in">
      {/* 页面标题 */}
      <div>
        <h1 className="text-3xl font-bold tracking-tight flex items-center gap-2">
          <ImageIcon className="h-7 w-7 text-primary" />
          多模态内容
        </h1>
        <p className="text-muted-foreground mt-1">
          浏览和检索文档中的图片、表格及智能洞察
        </p>
      </div>

      {/* 搜索栏 */}
      <form onSubmit={handleSearch} className="flex gap-2">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="搜索多模态内容，例如：流程图、销售额、组织架构..."
            className="pl-10"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <Button type="submit" disabled={isSearching || !query.trim()}>
          {isSearching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
          <span className="ml-2">搜索</span>
        </Button>
      </form>

      {/* 内容标签页 */}
      <Tabs value={activeType} onValueChange={setActiveType} className="space-y-4">
        <TabsList>
          <TabsTrigger value="all">全部</TabsTrigger>
          {types.map((t) => (
            <TabsTrigger key={t.id} value={t.id}>
              {t.name}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="all" className="space-y-4">
          {query && searchResults.length > 0 ? (
            <>
              <div className="text-sm text-muted-foreground">
                搜索到 {searchResults.length} 条与 "{query}" 相关的结果
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                {searchResults.map(renderItem)}
              </div>
            </>
          ) : listLoading ? (
            <div className="flex items-center justify-center py-20 text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin mr-2" />
              加载中...
            </div>
          ) : listResults.length > 0 ? (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
              {listResults.map(renderItem)}
            </div>
          ) : (
            <div className="text-center py-20 text-muted-foreground border rounded-lg">
              <Eye className="h-10 w-10 mx-auto mb-3 opacity-50" />
              <p>暂无可展示的多模态内容</p>
              <p className="text-sm mt-1">请先上传包含图片或表格的文档并建立索引</p>
            </div>
          )}
        </TabsContent>

        {types.map((t) => (
          <TabsContent key={t.id} value={t.id} className="space-y-4">
            {query && searchResults.length > 0 ? (
              <>
                <div className="text-sm text-muted-foreground">
                  搜索到 {searchResults.length} 条与 "{query}" 相关的 {t.name} 结果
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                  {searchResults.map(renderItem)}
                </div>
              </>
            ) : listLoading ? (
              <div className="flex items-center justify-center py-20 text-muted-foreground">
                <Loader2 className="h-5 w-5 animate-spin mr-2" />
                加载中...
              </div>
            ) : listResults.length > 0 ? (
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                {listResults.map(renderItem)}
              </div>
            ) : (
              <div className="text-center py-20 text-muted-foreground border rounded-lg">
                <Eye className="h-10 w-10 mx-auto mb-3 opacity-50" />
                <p>暂无 {t.name} 内容</p>
              </div>
            )}
          </TabsContent>
        ))}
      </Tabs>
    </div>
  )
}
