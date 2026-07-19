/**
 * 任务式 Agent 页面 (P1-3)
 */

import React, { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Play,
  Square,
  Clock,
  CheckCircle2,
  XCircle,
  AlertCircle,
  Loader2,
  Plus,
  ListTodo,
  ArrowRight,
  RefreshCw,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Progress } from '@/components/ui/progress'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { useTaskStore } from '@/stores/useTaskStore'
import { cn } from '@/lib/utils'
import type { Task, TaskStep } from '@/services/taskService'

const STATUS_BADGE: Record<string, { label: string; className: string }> = {
  pending: { label: '待执行', className: 'bg-gray-100 text-gray-700' },
  planning: { label: '规划中', className: 'bg-blue-100 text-blue-700' },
  executing: { label: '执行中', className: 'bg-amber-100 text-amber-700' },
  completed: { label: '已完成', className: 'bg-emerald-100 text-emerald-700' },
  failed: { label: '失败', className: 'bg-red-100 text-red-700' },
  cancelled: { label: '已取消', className: 'bg-gray-100 text-gray-500' },
}

const STEP_ICONS: Record<string, React.ReactNode> = {
  pending: <Clock className="h-4 w-4 text-muted-foreground" />,
  running: <Loader2 className="h-4 w-4 text-amber-500 animate-spin" />,
  completed: <CheckCircle2 className="h-4 w-4 text-emerald-500" />,
  failed: <XCircle className="h-4 w-4 text-red-500" />,
}

export function TasksPage() {
  const navigate = useNavigate()
  const { tasks, isLoading, error, fetchTasks, createTask, executeTask, cancelTask, streamProgress, currentTask } =
    useTaskStore()

  const [showCreate, setShowCreate] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newDescription, setNewDescription] = useState('')

  useEffect(() => {
    fetchTasks()
  }, [fetchTasks])

  const handleCreate = async () => {
    if (!newTitle.trim()) return
    const task = await createTask({ title: newTitle, description: newDescription })
    if (task) {
      setShowCreate(false)
      setNewTitle('')
      setNewDescription('')
      await executeTask(task.id)
      streamProgress(task.id)
    }
  }

  const handleExecute = async (id: string) => {
    await executeTask(id)
    streamProgress(id)
  }

  const handleCancel = async (id: string) => {
    await cancelTask(id)
  }

  const taskList = Array.isArray(tasks) ? tasks : []

  const getProgress = (task: Task): number => {
    const steps = Array.isArray(task?.steps) ? task.steps : []
    if (steps.length === 0) return 0
    const completed = steps.filter((s) => s.status === 'completed').length
    return Math.round((completed / steps.length) * 100)
  }

  const formatDate = (dateStr: string) => {
    const d = new Date(dateStr)
    return d.toLocaleString('zh-CN')
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">任务 Agent</h1>
          <p className="text-sm text-muted-foreground mt-1">
            AI 自动规划并执行复杂任务，实时查看进度
          </p>
        </div>
        <Dialog open={showCreate} onOpenChange={setShowCreate}>
          <DialogTrigger asChild>
            <Button>
              <Plus className="h-4 w-4 mr-2" />
              新建任务
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>创建新任务</DialogTitle>
            </DialogHeader>
            <div className="space-y-4 pt-4">
              <div>
                <label className="text-sm font-medium">任务标题</label>
                <Input
                  value={newTitle}
                  onChange={(e) => setNewTitle(e.target.value)}
                  placeholder="例如：分析竞品官网并输出报告"
                  className="mt-1"
                />
              </div>
              <div>
                <label className="text-sm font-medium">任务描述（可选）</label>
                <Textarea
                  value={newDescription}
                  onChange={(e) => setNewDescription(e.target.value)}
                  placeholder="详细描述任务要求..."
                  className="mt-1"
                  rows={3}
                />
              </div>
              <Button onClick={handleCreate} className="w-full" disabled={!newTitle.trim()}>
                创建并执行
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      {error && (
        <div className="bg-destructive/10 text-destructive px-4 py-3 rounded-lg text-sm">{error}</div>
      )}

      {isLoading && taskList.length === 0 ? (
        <div className="space-y-4">
          {[1, 2, 3].map((i) => (
            <Card key={i}>
              <CardContent className="py-4">
                <Skeleton className="h-6 w-1/3 mb-2" />
                <Skeleton className="h-4 w-2/3" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : taskList.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
          <ListTodo className="h-12 w-12 mb-4" />
          <p className="text-lg font-medium">暂无任务</p>
          <p className="text-sm mt-1">创建你的第一个 AI 任务</p>
        </div>
      ) : (
        <div className="space-y-4">
          {taskList.map((task) => (
            <Card
              key={task.id}
              className="hover:shadow-md transition-shadow cursor-pointer"
              onClick={() => navigate(`/tasks/${task.id}`)}
            >
              <CardContent className="py-4">
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-3">
                      <h3 className="font-semibold">{task.title}</h3>
                      <Badge className={cn('text-xs', STATUS_BADGE[task.status]?.className)}>
                        {STATUS_BADGE[task.status]?.label || task.status}
                      </Badge>
                    </div>
                    {task.description && (
                      <p className="text-sm text-muted-foreground mt-1 line-clamp-1">{task.description}</p>
                    )}

                    {/* 步骤进度条 */}
                    {Array.isArray(task.steps) && task.steps.length > 0 && (
                      <div className="mt-3 space-y-1.5">
                        <Progress value={getProgress(task)} className="h-1.5" />
                        <div className="flex items-center gap-2 flex-wrap">
                          {task.steps.map((step) => (
                            <div key={step.id} className="flex items-center gap-1 text-xs">
                              {STEP_ICONS[step.status]}
                              <span className="text-muted-foreground truncate max-w-[120px]">
                                {step.description}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    <div className="flex items-center gap-3 mt-2 text-xs text-muted-foreground">
                      <span>{formatDate(task.created_at)}</span>
                      {task.result && <span className="text-emerald-600">有结果</span>}
                      {task.error && <span className="text-red-600">有错误</span>}
                    </div>
                  </div>

                  {/* 操作按钮 */}
                  <div className="flex items-center gap-1 ml-3" onClick={(e) => e.stopPropagation()}>
                    {(task.status === 'pending' || task.status === 'failed') && (
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => handleExecute(task.id)}
                        title="执行"
                      >
                        <Play className="h-4 w-4 text-emerald-600" />
                      </Button>
                    )}
                    {task.status === 'executing' && (
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => handleCancel(task.id)}
                        title="取消"
                      >
                        <Square className="h-4 w-4 text-red-500" />
                      </Button>
                    )}
                    <Button variant="ghost" size="icon" onClick={() => navigate(`/tasks/${task.id}`)}>
                      <ArrowRight className="h-4 w-4" />
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}

export default TasksPage
