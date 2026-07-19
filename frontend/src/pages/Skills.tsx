/**
 * Skills 市场/管理页面 (P2-2)
 */

import React, { useState, useEffect } from 'react'
import {
  Zap,
  Search,
  Plus,
  Settings2,
  Bolt,
  Shield,
  FileCode,
  ClipboardList,
  Loader2,
  ToggleLeft,
  ToggleRight,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { useSkillStore } from '@/stores/useSkillStore'
import { cn } from '@/lib/utils'

const SKILL_ICONS: Record<string, React.ReactNode> = {
  legal_assistant: <Shield className="h-8 w-8 text-amber-500" />,
  code_review: <FileCode className="h-8 w-8 text-emerald-500" />,
  weekly_report: <ClipboardList className="h-8 w-8 text-blue-500" />,
}

export function SkillsPage() {
  const { skills, isLoading, error, fetchSkills, toggleSkill } = useSkillStore()
  const [searchTerm, setSearchTerm] = useState('')

  useEffect(() => {
    fetchSkills(true) // include disabled
  }, [fetchSkills])

  const filteredSkills = skills.filter(
    (s) =>
      s.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
      s.description.toLowerCase().includes(searchTerm.toLowerCase())
  )

  const handleToggle = async (id: string, currentEnabled: boolean) => {
    await toggleSkill(id, !currentEnabled)
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Skills 市场</h1>
          <p className="text-sm text-muted-foreground mt-1">
            启用/禁用 AI Skills，让你的助手更专业
          </p>
        </div>
        <div className="flex items-center gap-3">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="搜索 Skill..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="pl-9 w-64"
            />
          </div>
          <Button variant="outline">
            <Plus className="h-4 w-4 mr-2" />
            创建 Skill
          </Button>
        </div>
      </div>

      {error && (
        <div className="bg-destructive/10 text-destructive px-4 py-3 rounded-lg text-sm">{error}</div>
      )}

      {isLoading && skills.length === 0 ? (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {[1, 2, 3].map((i) => (
            <Card key={i}>
              <CardHeader>
                <Skeleton className="h-8 w-8 rounded-lg mb-2" />
                <Skeleton className="h-5 w-3/4" />
              </CardHeader>
              <CardContent>
                <Skeleton className="h-12" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : filteredSkills.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
          <Zap className="h-12 w-12 mb-4" />
          <p className="text-lg font-medium">暂无可用的 Skill</p>
          <p className="text-sm mt-1">创建或启用 Skills 来增强 AI 能力</p>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {filteredSkills.map((skill) => (
            <Card
              key={skill.id}
              className={cn(
                'group transition-shadow hover:shadow-md',
                !skill.is_enabled && 'opacity-60'
              )}
            >
              <CardHeader className="pb-2">
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-3">
                    <div className="p-2 rounded-lg bg-accent">
                      {SKILL_ICONS[skill.id] || <Bolt className="h-8 w-8 text-primary" />}
                    </div>
                    <div>
                      <CardTitle className="text-base">{skill.name}</CardTitle>
                      <div className="flex items-center gap-2 mt-1">
                        {skill.is_builtin && (
                          <Badge variant="secondary" className="text-xs">内置</Badge>
                        )}
                        <span className="text-xs text-muted-foreground">v{skill.version}</span>
                      </div>
                    </div>
                  </div>
                  <Switch
                    checked={skill.is_enabled}
                    onCheckedChange={() => handleToggle(skill.id, skill.is_enabled)}
                  />
                </div>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground line-clamp-2">
                  {skill.description}
                </p>
                {skill.tools.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-3">
                    {skill.tools.map((tool) => (
                      <Badge key={tool} variant="outline" className="text-xs">
                        {tool}
                      </Badge>
                    ))}
                  </div>
                )}
                {skill.example_questions.length > 0 && (
                  <div className="mt-3 pt-3 border-t">
                    <p className="text-xs text-muted-foreground mb-1">示例问题：</p>
                    {skill.example_questions.slice(0, 2).map((q, i) => (
                      <p key={i} className="text-xs text-muted-foreground/70 truncate">
                        "{q}"
                      </p>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}

export default SkillsPage
