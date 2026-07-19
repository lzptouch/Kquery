import React, { useState } from 'react'
import { Outlet } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { Header } from './Header'
import { Sidebar } from './Sidebar'

/**
 * 主布局组件
 * 
 * 结构:
 * ┌──────────────────────────────────────┐
 * │              Header                   │
 * ├────────┬─────────────────────────────┤
 * │        │                             │
 │ Sidebar│      Main Content           │
 * │ (可折叠)│      (Outlet)              │
 * │        │                             │
 * │        │                             │
 * └────────┴─────────────────────────────┘
 */

export function MainLayout() {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [isMobileSidebarOpen, setIsMobileSidebarOpen] = useState(false)

  // 切换侧边栏折叠状态
  const toggleSidebar = () => {
    if (window.innerWidth < 1024) {
      setIsMobileSidebarOpen(!isMobileSidebarOpen)
    } else {
      setSidebarCollapsed(!sidebarCollapsed)
    }
  }

  // 关闭移动端侧边栏
  const closeMobileSidebar = () => {
    setIsMobileSidebarOpen(false)
  }

  return (
    <div className="min-h-screen bg-background">
      {/* 顶部导航栏 */}
      <Header 
        onToggleSidebar={toggleSidebar}
        sidebarCollapsed={sidebarCollapsed}
      />
      
      <div className="flex pt-16"> {/* pt-16 为Header留出空间 */}
        {/* 侧边栏 */}
        <Sidebar 
          collapsed={sidebarCollapsed}
          isMobileOpen={isMobileSidebarOpen}
          onToggle={toggleSidebar}
          onCloseMobile={closeMobileSidebar}
        />
        
        {/* 主内容区域 */}
        <main 
          className={cn(
            "flex-1 min-h-[calc(100vh-4rem)] transition-all duration-300 ease-in-out bg-gradient-to-b from-transparent to-muted/20",
            // 桌面端：根据侧边栏状态调整左边距，避免遮挡
            sidebarCollapsed ? "lg:ml-16" : "lg:ml-64",
            // 移动端：全宽
            "w-full"
          )}
        >
          <div className="container-app py-6">
            {/* 子路由出口 */}
            <Outlet />
          </div>
        </main>
      </div>
      
      {/* 移动端遮罩层 */}
      {isMobileSidebarOpen && (
        <div 
          className="fixed inset-0 z-40 bg-black/50 lg:hidden"
          onClick={closeMobileSidebar}
          aria-hidden="true"
        />
      )}
    </div>
  )
}

export default MainLayout
