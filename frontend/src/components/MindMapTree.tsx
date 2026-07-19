/**
 * 思维导图树组件 (P0-3)
 * 使用 React + CSS 实现层级树形思维导图
 */

import React, { useState, useMemo, useCallback } from 'react'
import { ChevronRight, ChevronDown, Maximize2, Minimize2, Download } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

export interface MindMapNode {
  id: string
  label: string
  children?: MindMapNode[]
  collapsed?: boolean
  color?: string
}

interface MindMapTreeProps {
  data: MindMapNode
  title?: string
  onExport?: () => void
}

const COLORS = [
  '#6366f1', '#8b5cf6', '#06b6d4', '#10b981',
  '#f59e0b', '#ef4444', '#ec4899', '#3b82f6',
]

function TreeNode({
  node,
  depth,
  onToggle,
  globalCollapsed,
}: {
  node: MindMapNode
  depth: number
  onToggle: (id: string) => void
  globalCollapsed: boolean
}) {
  const [localCollapsed, setLocalCollapsed] = useState(node.collapsed || false)
  const isCollapsed = globalCollapsed || localCollapsed
  const hasChildren = node.children && node.children.length > 0
  const borderColor = node.color || COLORS[depth % COLORS.length]

  const toggle = useCallback(() => {
    setLocalCollapsed(!localCollapsed)
    onToggle(node.id)
  }, [localCollapsed, node.id, onToggle])

  return (
    <div className="relative">
      <div
        className={cn(
          'flex items-center gap-2 px-3 py-2 rounded-lg border-2 transition-all cursor-pointer group',
          'hover:shadow-sm hover:scale-[1.02]'
        )}
        style={{
          borderColor,
          marginLeft: `${depth * 24}px`,
          backgroundColor: `${borderColor}08`,
        }}
        onClick={toggle}
      >
        {hasChildren ? (
          isCollapsed ? (
            <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
          )
        ) : (
          <span className="w-4 shrink-0" />
        )}
        <span
          className="w-3 h-3 rounded-full shrink-0"
          style={{ backgroundColor: borderColor }}
        />
        <span className="text-sm font-medium truncate">{node.label}</span>
        {hasChildren && (
          <span className="text-xs text-muted-foreground ml-auto">
            {node.children!.length}
          </span>
        )}
      </div>

      {hasChildren && !isCollapsed && (
        <div className="relative">
          {/* 连接线 */}
          <div
            className="absolute top-0 bottom-0 w-px"
            style={{
              left: `${depth * 24 + 18}px`,
              backgroundColor: `${borderColor}40`,
            }}
          />
          {node.children!.map((child) => (
            <div key={child.id} className="relative">
              {/* 水平连接线 */}
              <div
                className="absolute top-1/2 h-px"
                style={{
                  left: `${depth * 24 + 18}px`,
                  width: '20px',
                  backgroundColor: `${borderColor}40`,
                }}
              />
              <TreeNode
                node={child}
                depth={depth + 1}
                onToggle={onToggle}
                globalCollapsed={globalCollapsed}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function MindMapTree({ data, title = '思维导图', onExport }: MindMapTreeProps) {
  const [globalCollapsed, setGlobalCollapsed] = useState(false)
  const [_, setToggleKey] = useState(0) // 触发重渲染

  const handleToggle = useCallback(() => {
    setToggleKey((k) => k + 1)
  }, [])

  return (
    <Card className="w-full">
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base">{title}</CardTitle>
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setGlobalCollapsed(!globalCollapsed)}
              title={globalCollapsed ? '全部展开' : '全部折叠'}
            >
              {globalCollapsed ? (
                <Maximize2 className="h-4 w-4" />
              ) : (
                <Minimize2 className="h-4 w-4" />
              )}
            </Button>
            {onExport && (
              <Button variant="ghost" size="icon" onClick={onExport} title="导出">
                <Download className="h-4 w-4" />
              </Button>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="max-h-[500px] overflow-auto pb-4">
        <TreeNode
          node={data}
          depth={0}
          onToggle={handleToggle}
          globalCollapsed={globalCollapsed}
        />
      </CardContent>
    </Card>
  )
}

export default MindMapTree
