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
  badge?: number | string  // 右上角徽章（如未读数）
  description?: string     // 悬停提示文字
}

const navigationItems: NavItem[] = [
  {
    title: '首页概览',
    href: '/',
    icon: LayoutDashboard,
    description: '查看系统总览和数据统计',
  },
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
  {
    title: '智能问答',
    href: '/chat',
    icon: MessageSquare,
    description: 'GraphRAG智能对话助手',
  },
  {
    title: '系统设置',
    href: '/settings',
    icon: Settings,
    description: '用户权限和系统配置',
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
          "flex items-center h-14 px-3 border-b",
          collapsed ? "justify-center" : "justify-between px-4"
        )}>
          {!collapsed && (
            <span className="text-sm font-semibold text-muted-foreground">导航菜单</span>
          )}
          
          {/* 折叠/展开按钮 */}
          <Button
            variant="ghost"
            size="icon"
            onClick={onToggle}
            className={cn(
              "h-8 w-8",
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
        <nav className="flex-1 overflow-y-auto py-2">
          <ul className="space-y-1 px-2">
            {navigationItems.map((item) => (
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
        </nav>

        {/* 底部信息 */}
        {!collapsed && (
          <div className="p-4 border-t">
            <div className="text-xs text-muted-foreground space-y-1">
              <div>KQuery Pro v1.0.0</div>
              <div>Powered by GraphRAG</div>
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
              <ul className="space-y-1 px-2">
                {navigationItems.map((item) => (
                  <li key={item.href}>
                    <NavLink
                      to={item.href}
                      onClick={onCloseMobile}
                      className={cn(
                        "flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-all",
                        (item.href === '/'
                          ? location.pathname === '/'
                          : location.pathname.startsWith(item.href))
                          ? "bg-primary text-primary-foreground shadow-md"
                          : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                      )}
                    >
                      <item.icon className="h-5 w-5 shrink-0" />
                      <span>{item.title}</span>
                      {item.badge && (
                        <span className="ml-auto">{item.badge}</span>
                      )}
                    </NavLink>
                  </li>
                ))}
              </ul>
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
        "group flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-all",
        isActive
          ? "bg-primary text-primary-foreground shadow-md"
          : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
      )}
    >
      {/* 图标 */}
      <item.icon 
        className={cn(
          "h-5 w-5 shrink-0",
          isActive ? "text-primary-foreground" : "text-muted-foreground group-hover:text-accent-foreground"
        )} 
      />

      {/* 文字标签 */}
      {!collapsed && (
        <>
          <span className="truncate flex-1">{item.title}</span>
          
          {/* 徽章 */}
          {item.badge && (
            <span className="shrink-0 text-xs">{item.badge}</span>
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
