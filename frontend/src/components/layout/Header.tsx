import React, { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  Menu,
  Search,
  Moon,
  Sun,
  Settings,
  BrainCircuit,
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
    <header className="fixed top-0 left-0 right-0 h-16 bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60 border-b z-50">
      <div className="flex items-center justify-between h-full px-4 lg:px-6">
        {/* 左侧：Logo + 折叠按钮 */}
        <div className="flex items-center gap-4">
          {/* 移动端菜单按钮 */}
          <Button
            variant="ghost"
            size="icon"
            className="lg:hidden"
            onClick={onToggleSidebar}
            aria-label="切换侧边栏"
          >
            <Menu className="h-5 w-5" />
          </Button>
          
          {/* Logo和品牌名 */}
          <Link to="/" className="flex items-center gap-2 hover:opacity-80 transition-opacity">
            <BrainCircuit className="h-8 w-8 text-primary" />
            <span className="text-xl font-bold gradient-text hidden sm:inline-block">
              KQuery Pro
            </span>
          </Link>
          
          {/* 桌面端搜索框 */}
          <form onSubmit={handleSearch} className="hidden md:flex items-center relative ml-8">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              type="search"
              placeholder="搜索知识库、文档、实体..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-9 w-64 lg:w-96 transition-all focus:w-72 lg:focus:w-[400px]"
            />
          </form>
        </div>

        {/* 右侧：操作区 */}
        <div className="flex items-center gap-2">
          {/* 暗黑模式切换 */}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                onClick={toggleDarkMode}
                aria-label={isDarkMode ? '切换到亮色模式' : '切换到暗色模式'}
              >
                {isDarkMode ? (
                  <Sun className="h-5 w-5 text-yellow-500" />
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
              <Button variant="ghost" size="icon" asChild aria-label="系统设置">
                <Link to="/settings">
                  <Settings className="h-5 w-5" />
                </Link>
              </Button>
            </TooltipTrigger>
            <TooltipContent>系统设置</TooltipContent>
          </Tooltip>
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
            />
          </form>
        </div>
      )}
    </header>
  )
}

export default Header
