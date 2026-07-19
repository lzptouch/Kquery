import React, { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  Menu,
  Search,
  Moon,
  Sun,
  Settings,
  BrainCircuit,
  Bell,
  User,
  ChevronDown,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

interface HeaderProps {
  onToggleSidebar: () => void
  sidebarCollapsed: boolean
}

/**
 * 顶部导航栏组件
 * 功能:
 * - Logo和品牌名
 * - 全局搜索框
 * - 暗黑模式切换
 * - 系统设置入口
 */
export function Header({ onToggleSidebar, sidebarCollapsed }: HeaderProps) {
  const navigate = useNavigate()

  // UI状态
  const [isDarkMode, setIsDarkMode] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [showSearch, setShowSearch] = useState(false)


  // 初始化暗黑模式
  useEffect(() => {
    // 检查系统偏好或本地存储
    const isDark = localStorage.getItem('theme') === 'dark' ||
      (!localStorage.getItem('theme') && window.matchMedia('(prefers-color-scheme: dark)').matches)

    setIsDarkMode(isDark)
    document.documentElement.classList.toggle('dark', isDark)
  }, [])

  // 切换暗黑模式
  const toggleDarkMode = () => {
    const newMode = !isDarkMode
    setIsDarkMode(newMode)
    document.documentElement.classList.toggle('dark', newMode)
    localStorage.setItem('theme', newMode ? 'dark' : 'light')
  }

  // 处理搜索
  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    if (searchQuery.trim()) {
      navigate(`/search?q=${encodeURIComponent(searchQuery)}`)
      setShowSearch(false)
      setSearchQuery('')
    }
  }

  return (
    <header className="fixed top-0 left-0 right-0 h-16 bg-background/80 backdrop-blur-xl border-b border-border/50 z-50">
      <div className="flex items-center justify-between h-full px-4 lg:px-6">
        {/* 左侧：Logo + 折叠按钮 */}
        <div className="flex items-center gap-3">
          {/* 移动端菜单按钮 */}
          <Button
            variant="ghost"
            size="icon"
            className="lg:hidden rounded-lg"
            onClick={onToggleSidebar}
            aria-label="切换侧边栏"
          >
            <Menu className="h-5 w-5" />
          </Button>
          
          {/* 桌面端折叠按钮 */}
          <Button
            variant="ghost"
            size="icon"
            className="hidden lg:flex rounded-lg"
            onClick={onToggleSidebar}
            aria-label={sidebarCollapsed ? "展开侧边栏" : "折叠侧边栏"}
          >
            <Menu className="h-5 w-5" />
          </Button>

          {/* Logo和品牌名 */}
          <Link to="/" className="flex items-center gap-2.5 hover:opacity-80 transition-opacity">
            <div className="relative">
              <div className="absolute inset-0 bg-gradient-to-br from-blue-500 to-purple-600 rounded-lg blur-sm opacity-30" />
              <BrainCircuit className="h-7 w-7 text-primary relative" />
            </div>
            <span className="text-lg font-bold gradient-text hidden sm:inline-block">
              KQuery Pro
            </span>
          </Link>
          
          {/* 桌面端搜索框 */}
          <form onSubmit={handleSearch} className="hidden md:flex items-center relative ml-6">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              type="search"
              placeholder="搜索知识库、文档、实体..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-9 w-56 lg:w-72 h-9 rounded-lg border-border/50 bg-muted/50 focus:bg-background transition-all focus:w-64 lg:focus:w-80"
            />
          </form>
        </div>

        {/* 右侧：操作区 */}
        <div className="flex items-center gap-1">
          {/* 通知铃铛 */}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="relative rounded-lg"
                aria-label="通知"
              >
                <Bell className="h-5 w-5" />
                <span className="absolute top-1.5 right-1.5 w-2 h-2 rounded-full bg-red-500 ring-1 ring-background" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>通知</TooltipContent>
          </Tooltip>

          {/* 暗黑模式切换 */}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                onClick={toggleDarkMode}
                className="rounded-lg"
                aria-label={isDarkMode ? '切换到亮色模式' : '切换到暗色模式'}
              >
                {isDarkMode ? (
                  <Sun className="h-5 w-5 text-amber-500" />
                ) : (
                  <Moon className="h-5 w-5" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              {isDarkMode ? '亮色模式' : '暗色模式'}
            </TooltipContent>
          </Tooltip>

          {/* 系统设置入口 */}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="ghost" size="icon" className="rounded-lg" asChild aria-label="系统设置">
                <Link to="/settings">
                  <Settings className="h-5 w-5" />
                </Link>
              </Button>
            </TooltipTrigger>
            <TooltipContent>系统设置</TooltipContent>
          </Tooltip>

          {/* 分隔线 */}
          <div className="w-px h-6 bg-border/50 mx-1" />

          {/* 用户头像 */}
          <button className="flex items-center gap-2.5 px-2 py-1 rounded-lg hover:bg-muted transition-colors">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center text-white text-xs font-bold ring-2 ring-background">
              <User className="h-4 w-4" />
            </div>
            <div className="hidden sm:flex flex-col items-start">
              <span className="text-sm font-medium leading-tight">管理员</span>
              <span className="text-xs text-muted-foreground leading-tight">admin@kquery.io</span>
            </div>
            <ChevronDown className="hidden sm:block h-3.5 w-3.5 text-muted-foreground" />
          </button>
        </div>
      </div>

      {/* 移动端搜索框（展开式） */}
      {showSearch && (
        <div className="absolute top-full left-0 right-0 bg-background border-b p-4 md:hidden">
          <form onSubmit={handleSearch}>
            <Input
              type="search"
              placeholder="搜索..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              autoFocus
              onBlur={() => !searchQuery && setShowSearch(false)}
              className="rounded-lg"
            />
          </form>
        </div>
      )}
    </header>
  )
}

export default Header
