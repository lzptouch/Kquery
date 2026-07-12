import { useState, useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Search, FileText, Loader2, AlertCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { ScrollArea } from '@/components/ui/scroll-area'
import { queryApi, type QueryResult } from '@/lib/api'

interface SearchResult {
  id: string
  content: string
  source: string
  score: number
  metadata?: Record<string, any>
}

interface QueryApiItem {
  content: string
  score: number
  metadata?: Record<string, any>
}

/**
 * 全局搜索结果页面
 */
export function SearchResults() {
  const [searchParams, setSearchParams] = useSearchParams()
  const initialQuery = searchParams.get('q') || ''

  const [query, setQuery] = useState(initialQuery)
  const [results, setResults] = useState<SearchResult[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [hasSearched, setHasSearched] = useState(false)

  useEffect(() => {
    const q = searchParams.get('q') || ''
    setQuery(q)
    if (q) {
      performSearch(q)
    }
  }, [searchParams])

  const performSearch = async (searchQuery: string) => {
    if (!searchQuery.trim()) return

    setIsLoading(true)
    setError(null)
    setHasSearched(true)

    try {
      const collectionName = searchParams.get('collection') || 'tech_docs'
      const queryResult = await queryApi.search({
        query: searchQuery,
        collection_name: collectionName,
        top_k: 10,
      })
      const mapped: SearchResult[] = (queryResult.data?.sources || []).map((item: QueryApiItem, idx: number) => ({
        id: `result_${idx}`,
        content: item.content,
        source: item.metadata?.source || item.metadata?.file_path || '未知来源',
        score: item.score,
        metadata: item.metadata,
      }))
      setResults(mapped)
    } catch (err: any) {
      // 防止 React 渲染 Zod 验证错误对象（{type, loc, msg, input} 等）
      const errDetail = err.response?.data?.detail
      const errMsg = typeof errDetail === 'string'
        ? errDetail
        : Array.isArray(errDetail)
          ? errDetail.map((e: any) => e.msg || String(e)).join('; ')
          : '搜索失败，请稍后重试'
      setError(errMsg)
      setResults([])
    } finally {
      setIsLoading(false)
    }
  }

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    if (!query.trim()) return
    setSearchParams({ q: query.trim() })
  }

  return (
    <div className="container mx-auto py-6 max-w-5xl">
      {/* 搜索头部 */}
      <div className="mb-8">
        <h1 className="text-2xl font-bold mb-4 flex items-center gap-2">
          <Search className="w-6 h-6" />
          全局搜索
        </h1>
        <form onSubmit={handleSearch} className="flex gap-2">
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="输入关键词搜索知识库..."
            className="flex-1"
          />
          <Button type="submit" disabled={isLoading}>
            {isLoading ? (
              <Loader2 className="w-4 h-4 animate-spin mr-2" />
            ) : (
              <Search className="w-4 h-4 mr-2" />
            )}
            搜索
          </Button>
        </form>
      </div>

      {/* 错误提示 */}
      {error && (
        <div className="mb-4 p-4 bg-red-50 border border-red-200 rounded-lg flex items-center gap-2 text-red-700">
          <AlertCircle className="w-5 h-5" />
          {error}
        </div>
      )}

      {/* 搜索结果 */}
      {hasSearched && !isLoading && results.length === 0 && !error && (
        <div className="text-center py-12 text-muted-foreground">
          <Search className="w-12 h-12 mx-auto mb-4 opacity-50" />
          <p>未找到与 &quot;{query}&quot; 相关的结果</p>
          <p className="text-sm mt-2">尝试使用其他关键词或检查知识库中是否有相关文档</p>
        </div>
      )}

      {results.length > 0 && (
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            找到 {results.length} 条相关结果
          </p>
          <ScrollArea className="h-[calc(100vh-280px)]">
            <div className="space-y-4 pr-4">
              {results.map((result, index) => (
                <Card key={result.id || index}>
                  <CardHeader className="pb-3">
                    <div className="flex items-center justify-between">
                      <CardTitle className="text-base flex items-center gap-2">
                        <FileText className="w-4 h-4 text-muted-foreground" />
                        {result.source || '未知来源'}
                      </CardTitle>
                      <Badge variant="secondary">
                        相似度: {(result.score * 100).toFixed(1)}%
                      </Badge>
                    </div>
                  </CardHeader>
                  <CardContent>
                    <p className="text-sm text-muted-foreground line-clamp-4">
                      {result.content}
                    </p>
                    {result.metadata && Object.keys(result.metadata).length > 0 && (
                      <div className="mt-3 flex flex-wrap gap-2">
                        {Object.entries(result.metadata).map(([key, value]) => (
                          <Badge key={key} variant="outline" className="text-xs">
                            {key}: {String(value)}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </CardContent>
                </Card>
              ))}
            </div>
          </ScrollArea>
        </div>
      )}
    </div>
  )
}

export default SearchResults
