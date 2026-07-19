/**
 * 知识库共享对话框组件 (P2-1)
 * 管理成员、生成邀请链接、权限设置
 */

import React, { useState, useEffect } from 'react'
import {
  Users,
  Share2,
  Trash2,
  Shield,
  Copy,
  Check,
  Link,
  Loader2,
  UserPlus,
  Globe,
  Lock,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import { ScrollArea } from '@/components/ui/scroll-area'
import { kbService, type KBMember, type KBInvite } from '@/services/kbService'
import { cn } from '@/lib/utils'

interface KnowledgeBaseShareDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  kbId: string
  kbName: string
  visibility: 'public' | 'private'
  isOwner: boolean
}

const ROLE_OPTIONS = [
  { value: 'admin', label: '管理员' },
  { value: 'editor', label: '编辑者' },
  { value: 'viewer', label: '阅读者' },
]

export function KnowledgeBaseShareDialog({
  open,
  onOpenChange,
  kbId,
  kbName,
  visibility,
  isOwner,
}: KnowledgeBaseShareDialogProps) {
  const [members, setMembers] = useState<KBMember[]>([])
  const [invite, setInvite] = useState<KBInvite | null>(null)
  const [newUsername, setNewUsername] = useState('')
  const [newRole, setNewRole] = useState('viewer')
  const [copied, setCopied] = useState(false)
  const [isLoading, setIsLoading] = useState(false)

  useEffect(() => {
    if (open) {
      loadMembers()
    }
  }, [open, kbId])

  const loadMembers = async () => {
    try {
      const data = await kbService.listMembers(kbId)
      setMembers(data)
    } catch { /* ignore */ }
  }

  const handleAddMember = async () => {
    if (!newUsername.trim()) return
    setIsLoading(true)
    try {
      await kbService.addMember(kbId, { username: newUsername, role: newRole })
      setNewUsername('')
      await loadMembers()
    } catch { /* ignore */ }
    setIsLoading(false)
  }

  const handleRemoveMember = async (userId: string) => {
    if (!confirm('确定移除该成员？')) return
    try {
      await kbService.removeMember(kbId, userId)
      await loadMembers()
    } catch { /* ignore */ }
  }

  const handleUpdateRole = async (memberId: string, newRole: string) => {
    try {
      await kbService.updateRole(kbId, memberId, { role: newRole })
      setMembers((prev) =>
        prev.map((m) => (m.id === memberId ? { ...m, role: newRole as any } : m))
      )
    } catch { /* ignore */ }
  }

  const handleGenerateInvite = async () => {
    try {
      const data = await kbService.generateInvite(kbId)
      setInvite(data)
    } catch { /* ignore */ }
  }

  const handleCopyLink = () => {
    if (invite) {
      const url = `${window.location.origin}/knowledge-bases/${kbId}/join?code=${invite.code}`
      navigator.clipboard.writeText(url)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Share2 className="h-5 w-5" />
            共享设置 - {kbName}
          </DialogTitle>
          <DialogDescription>
            管理知识库成员和访问权限
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* 可见性 */}
          <div className="flex items-center justify-between p-3 bg-accent rounded-lg">
            <div className="flex items-center gap-2">
              {visibility === 'public' ? (
                <Globe className="h-4 w-4 text-emerald-600" />
              ) : (
                <Lock className="h-4 w-4 text-muted-foreground" />
              )}
              <span className="text-sm font-medium">
                {visibility === 'public' ? '公开' : '私有'}
              </span>
            </div>
            <Badge variant={visibility === 'public' ? 'default' : 'secondary'}>
              {visibility === 'public' ? '所有人可见' : '仅成员可见'}
            </Badge>
          </div>

          <Separator />

          {/* 邀请链接 */}
          <div>
            <h4 className="text-sm font-medium mb-2 flex items-center gap-2">
              <Link className="h-4 w-4" />
              邀请链接
            </h4>
            {invite ? (
              <div className="flex items-center gap-2">
                <Input
                  value={`${window.location.origin}/knowledge-bases/${kbId}/join?code=${invite.code}`}
                  readOnly
                  className="text-xs"
                />
                <Button variant="outline" size="icon" onClick={handleCopyLink}>
                  {copied ? <Check className="h-4 w-4 text-emerald-500" /> : <Copy className="h-4 w-4" />}
                </Button>
              </div>
            ) : (
              <Button variant="outline" size="sm" onClick={handleGenerateInvite} className="w-full">
                <Link className="h-4 w-4 mr-2" />
                生成邀请链接
              </Button>
            )}
          </div>

          <Separator />

          {/* 添加成员 */}
          <div>
            <h4 className="text-sm font-medium mb-2 flex items-center gap-2">
              <UserPlus className="h-4 w-4" />
              添加成员
            </h4>
            <div className="flex items-center gap-2">
              <Input
                placeholder="输入用户名"
                value={newUsername}
                onChange={(e) => setNewUsername(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleAddMember()}
                className="flex-1"
              />
              <Select value={newRole} onValueChange={setNewRole}>
                <SelectTrigger className="w-28">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ROLE_OPTIONS.map((r) => (
                    <SelectItem key={r.value} value={r.value}>
                      {r.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button size="sm" onClick={handleAddMember} disabled={isLoading || !newUsername.trim()}>
                {isLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : '添加'}
              </Button>
            </div>
          </div>

          {/* 成员列表 */}
          <div>
            <h4 className="text-sm font-medium mb-2 flex items-center gap-2">
              <Users className="h-4 w-4" />
              成员 ({members.length})
            </h4>
            <ScrollArea className="max-h-[200px]">
              <div className="space-y-1">
                {members.map((member) => (
                  <div
                    key={member.id}
                    className="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-accent"
                  >
                    <div className="flex items-center gap-2">
                      <Shield
                        className={cn(
                          'h-4 w-4',
                          member.role === 'owner' && 'text-amber-500',
                          member.role === 'admin' && 'text-blue-500',
                          member.role === 'editor' && 'text-emerald-500',
                          member.role === 'viewer' && 'text-muted-foreground'
                        )}
                      />
                      <span className="text-sm">{member.username}</span>
                      {member.role === 'owner' && (
                        <Badge variant="outline" className="text-xs">所有者</Badge>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      {isOwner && member.role !== 'owner' && (
                        <>
                          <Select
                            value={member.role}
                            onValueChange={(v) => handleUpdateRole(member.id, v)}
                          >
                            <SelectTrigger className="h-7 w-24 text-xs">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {ROLE_OPTIONS.map((r) => (
                                <SelectItem key={r.value} value={r.value}>
                                  {r.label}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7"
                            onClick={() => handleRemoveMember(member.user_id)}
                          >
                            <Trash2 className="h-3 w-3 text-destructive" />
                          </Button>
                        </>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </ScrollArea>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export default KnowledgeBaseShareDialog
