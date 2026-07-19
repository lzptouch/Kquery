import React from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { 
  LayoutDashboard,
  Database,
  FileText,
  Network,
  MessageSquare,
  Settings,
  ChevronLeft,
  ChevronRight,
  X,
  BrainCircuit,
  PenLine,
  Zap,
  ListTodo,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

// ========================================
// 导航菜单配置
// ========================================

interface NavItem {
  title: string
  href: string
  icon: React.ComponentType<{ className?: string }>
  badge?: string
  description?: string
}

interface NavGroup {
  label: string
  items: NavItem[]
}

const navigationGroups: NavGroup[] = [
  {
    label: '概览',
    items: [
      {
        title: '首页概览',
        href: '/',
        icon: LayoutDashboard,
        description: '查看系统总览和数据统计',
      },
    ],
  },
  {
    label: '知识管理',
    items: [
      {
        title: '知识库管理',
        href: '/knowledge-bases',
        icon: Database,
        description: '管理和配置知识库',
      },
      {
        title: '文档管理',
        href: '/documents',
        icon: FileText,
        description: '上传和管理文档',
      },
      {
        title: '知识图谱',
        href: '/knowledge-graph',
        icon: Network,
        badge: 'v2.0',
        description: '浏览和分析知识图谱',
      },
    ],
  },
  {
    label: 'AI 能力',
    items: [
      {
        title: '智能问答',
        href: '/chat',
        icon: MessageSquare,
        description: 'GraphRAG 智能对话助手',
      },
      {
        title: 'AI 笔记',
        href: '/notes',
        icon: PenLine,
        badge: 'v3.0',
        description: 'AI 写作笔记：续写、润色、总结',
      },
      {
        title: 'Skills',
        href: '/skills',
        icon: Zap,
        badge: 'v3.0',
        description: 'Skills 市场：启用/禁用 AI 技能',
      },
      {
        title: '任务 Agent',
        href: '/tasks',
        icon: ListTodo,
        badge: 'v3.0',
        description: 'AI 任务式 Agent：自动规划并执行',
      },
    ],
  },
  {
    label: '系统',
    items: [
      {
        title: '系统设置',
        href: '/settings',
        icon: Settings,
        description: '用户权限和系统配置',
      },
    ],
  },
]

interface SidebarProps {
  collapsed: boolean
  isMobileOpen: boolean
  onToggle: () => void
  onCloseMobile: () => void
}

/**
 * 侧边栏导航组件
 * 
 * 特性:
 * - 响应式设计（桌面可折叠，移动端抽屉式）
 * - 当前页面高亮
 * - 图标+文字显示（折叠时只显示图标，悬停显示Tooltip）
 * - 新功能徽章标记
 */
export function Sidebar({ collapsed, isMobileOpen, onToggle, onCloseMobile }: SidebarProps) {
  const location = useLocation()

  return (
    <>
      {/* 桌面端侧边栏 */}
      <aside
        className={cn(
          "fixed left-0 top-16 z-30 h-[calc(100vh-4rem)] bg-card border-r transition-all duration-300 ease-in-out hidden lg:flex flex-col",
          collapsed ? "w-16" : "w-64"
        )}
      >
        {/* Logo区域 */}
        <div className={cn(
          "flex items-center h-14 px-3 border-b border-border/50",
          collapsed ? "justify-center" : "justify-between px-4"
        )}>
          {!collapsed && (
            <div className="flex items-center gap-2.5">
              <div className="w-1.5 h-5 rounded-full bg-gradient-to-b from-primary to-purple-500" />
              <span className="text-sm font-semibold">导航菜单</span>
            </div>
          )}
          
          {/* 折叠/展开按钮 */}
          <Button
            variant="ghost"
            size="icon"
            onClick={onToggle}
            className={cn(
              "h-8 w-8 hover:bg-muted rounded-lg transition-colors",
              collapsed && "mx-auto"
            )}
            aria-label={collapsed ? "展开侧边栏" : "折叠侧边栏"}
          >
            {collapsed ? (
              <ChevronRight className="h-4 w-4" />
            ) : (
              <ChevronLeft className="h-4 w-4" />
            )}
          </Button>
        </div>

        {/* 导航菜单 */}
        <nav className="flex-1 overflow-y-auto py-3">
          {navigationGroups.map((group, gi) => (
            <div key={group.label} className={gi > 0 ? 'mt-3' : ''}>
              {!collapsed && (
                <div className="px-4 py-1.5">
                  <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60">
                    {group.label}
                  </span>
                </div>
              )}
              <ul className="space-y-0.5 px-2">
                {group.items.map((item) => (
                  <NavItemComponent
                    key={item.href}
                    item={item}
                    collapsed={collapsed}
                    isActive={
                      item.href === '/'
                        ? location.pathname === '/'
                        : location.pathname.startsWith(item.href)
                    }
                  />
                ))}
              </ul>
            </div>
          ))}
        </nav>

        {/* 底部信息 */}
        {!collapsed && (
          <div className="p-3 border-t">
            <div className="flex items-center gap-3 px-2 py-2 rounded-lg bg-gradient-to-r from-blue-500/5 to-purple-500/5">
              <div className="w-2 h-2 rounded-full bg-gradient-to-r from-blue-500 to-purple-500 animate-pulse" />
              <div className="text-xs text-muted-foreground space-y-0.5">
                <div className="font-medium text-foreground">KQuery Pro v3.0.0</div>
                <div>Powered by GraphRAG</div>
              </div>
            </div>
          </div>
        )}
      </aside>

      {/* 移动端侧边栏（抽屉式） */}
      {isMobileOpen && (
        <aside className="fixed inset-0 z-50 lg:hidden">
          {/* 遮罩层由MainLayout处理 */}  
          
          <div className="absolute left-0 top-0 bottom-0 w-72 bg-background shadow-xl animate-slide-in">
            {/* 头部 */}
            <div className="flex items-center justify-between h-16 px-4 border-b">
              <div className="flex items-center gap-2">
                <BrainCircuit className="h-6 w-6 text-primary" />
                <span className="font-bold gradient-text">KQuery Pro</span>
              </div>
              
              <Button variant="ghost" size="icon" onClick={onCloseMobile}>
                <X className="h-5 w-5" />
              </Button>
            </div>

            {/* 导航菜单 */}
            <nav className="py-4">
              {navigationGroups.map((group, gi) => (
                <div key={group.label} className={gi > 0 ? 'mt-3' : ''}>
                  <div className="px-4 py-1.5">
                    <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60">
                      {group.label}
                    </span>
                  </div>
                  <ul className="space-y-0.5 px-2">
                    {group.items.map((item) => (
                      <li key={item.href}>
                        <NavLink
                          to={item.href}
                          onClick={onCloseMobile}
                          className={cn(
                            "flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-all",
                            (item.href === '/'
                              ? location.pathname === '/'
                              : location.pathname.startsWith(item.href))
                              ? "bg-primary/10 text-primary"
                              : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                          )}
                        >
                          <item.icon className="h-5 w-5 shrink-0" />
                          <span>{item.title}</span>
                          {item.badge && (
                            <span className="ml-auto text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground">
                              {item.badge}
                            </span>
                          )}
                        </NavLink>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </nav>
          </div>
        </aside>
      )}
    </>
  )
}

/**
 * 单个导航项组件（桌面端使用）
 */
function NavItemComponent({ 
  item, 
  collapsed, 
  isActive 
}: { 
  item: NavItem
  collapsed: boolean
  isActive: boolean
}) {
  const content = (
    <NavLink
      to={item.href}
      className={cn(
        "group relative flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-all duration-200",
        isActive
          ? "bg-primary/10 text-primary"
          : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
      )}
    >
      {/* 活跃指示器 */}
      {isActive && (
        <span className="absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-5 rounded-full bg-primary" />
      )}

      {/* 图标 */}
      <item.icon 
        className={cn(
          "h-5 w-5 shrink-0 transition-colors",
          isActive ? "text-primary" : "text-muted-foreground group-hover:text-accent-foreground"
        )} 
      />

      {/* 文字标签 */}
      {!collapsed && (
        <>
          <span className="truncate flex-1">{item.title}</span>
          
          {/* 徽章 */}
          {item.badge && (
            <span className={cn(
              "shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded-full",
              isActive
                ? "bg-primary/20 text-primary"
                : "bg-muted text-muted-foreground"
            )}>
              {item.badge}
            </span>
          )}
        </>
      )}
    </NavLink>
  )

  // 折叠时显示Tooltip
  if (collapsed) {
    return (
      <Tooltip delayDuration={0}>
        <TooltipTrigger asChild>
          {content}
        </TooltipTrigger>
        <TooltipContent side="right" className="flex items-center gap-2">
          {item.title}
          {item.description && (
            <span className="text-xs opacity-70 ml-2">{item.description}</span>
          )}
        </TooltipContent>
      </Tooltip>
    )
  }

  return content
}

export default Sidebar
