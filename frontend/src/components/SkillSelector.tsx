/**
 * Skill 选择器组件 (P2-2)
 * 用于 Chat 页面选择/切换 Skill
 */

import React, { useState, useEffect } from 'react'
import { Zap, ChevronDown, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu'
import { useSkillStore } from '@/stores/useSkillStore'
import { cn } from '@/lib/utils'

interface SkillSelectorProps {
  onSelect?: (skillId: string | null) => void
  className?: string
}

export function SkillSelector({ onSelect, className }: SkillSelectorProps) {
  const { skills, activeSkillId, fetchSkills, setActiveSkill } = useSkillStore()
  const [isOpen, setIsOpen] = useState(false)

  useEffect(() => {
    fetchSkills()
  }, [fetchSkills])

  const activeSkill = skills.find((s) => s.id === activeSkillId)
  const enabledSkills = skills.filter((s) => s.is_enabled)

  const handleSelect = (skillId: string | null) => {
    setActiveSkill(skillId)
    onSelect?.(skillId)
    setIsOpen(false)
  }

  return (
    <DropdownMenu open={isOpen} onOpenChange={setIsOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className={cn('gap-2', activeSkill && 'border-primary/50', className)}
        >
          <Zap className="h-3.5 w-3.5" />
          {activeSkill ? (
            <>
              <span className="max-w-[100px] truncate">{activeSkill.name}</span>
              <X
                className="h-3 w-3 ml-1 hover:text-destructive"
                onClick={(e) => {
                  e.stopPropagation()
                  handleSelect(null)
                }}
              />
            </>
          ) : (
            <span>选择技能</span>
          )}
          <ChevronDown className="h-3.5 w-3.5 ml-1" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-64">
        <DropdownMenuItem onClick={() => handleSelect(null)}>
          <div className="flex items-center gap-2">
            <div className={cn('w-2 h-2 rounded-full', !activeSkillId ? 'bg-primary' : 'bg-muted')} />
            <span>自动匹配</span>
          </div>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {enabledSkills.map((skill) => (
          <DropdownMenuItem key={skill.id} onClick={() => handleSelect(skill.id)}>
            <div className="flex items-center gap-2">
              <div
                className={cn(
                  'w-2 h-2 rounded-full',
                  activeSkillId === skill.id ? 'bg-primary' : 'bg-muted'
                )}
              />
              <div className="flex-1">
                <p className="text-sm font-medium">{skill.name}</p>
                <p className="text-xs text-muted-foreground line-clamp-1">{skill.description}</p>
              </div>
            </div>
          </DropdownMenuItem>
        ))}
        {enabledSkills.length === 0 && (
          <div className="px-2 py-4 text-center text-sm text-muted-foreground">
            暂无可用的 Skill
          </div>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export default SkillSelector
