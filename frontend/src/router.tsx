import { createBrowserRouter, Navigate, type RouteObject } from 'react-router-dom'
import { MainLayout } from '@/components/layout/MainLayout'

/**
 * 辅助函数：包装 lazy import 以适配 React Router v6 的类型要求
 * 注意：运行时 import() 不认识 Vite alias，需将 @/ 替换为 /src/
 */
function lazyComponent(path: string) {
  return async () => {
    const resolvedPath = path.replace(/^@\//, '/src/')
    const module = await import(/* @vite-ignore */ resolvedPath)
    return { Component: module.default || module[path.split('/').pop()!.replace('.tsx', '')] }
  }
}

/**
 * 路由配置
 */
export const router = createBrowserRouter([
  {
    path: '/login',
    lazy: lazyComponent('@/pages/Login'),
  },
  {
    path: '/',
    element: <MainLayout />,
    children: [
      {
        index: true,
        lazy: lazyComponent('@/pages/Dashboard'),
      },
      {
        path: 'knowledge-bases',
        lazy: lazyComponent('@/pages/KnowledgeBases'),
      },
      {
        path: 'documents',
        lazy: lazyComponent('@/pages/Documents'),
      },
      {
        path: 'knowledge-graph',
        lazy: lazyComponent('@/pages/KnowledgeGraph'),
      },
      {
        path: 'chat',
        lazy: lazyComponent('@/pages/Chat'),
      },
      {
        path: 'settings',
        lazy: lazyComponent('@/pages/Settings'),
      },
      {
        path: 'analytics',
        lazy: lazyComponent('@/pages/Analytics'),
      },
      {
        path: 'multimodal',
        lazy: lazyComponent('@/pages/MultimodalViewer'),
      },
      {
        path: 'search',
        lazy: lazyComponent('@/pages/SearchResults'),
      },
      // v3.0 新增页面
      {
        path: 'notes',
        lazy: lazyComponent('@/pages/Notes'),
      },
      {
        path: 'notes/:id',
        lazy: lazyComponent('@/pages/NoteEditor'),
      },
      {
        path: 'skills',
        lazy: lazyComponent('@/pages/Skills'),
      },
      {
        path: 'tasks',
        lazy: lazyComponent('@/pages/Tasks'),
      },
      {
        path: 'tasks/:id',
        lazy: lazyComponent('@/pages/Tasks'),
      },
      // 运维可观测性中心（新增）
      {
        path: 'ops',
        lazy: lazyComponent('@/pages/OpsCenter'),
      },
      {
        path: '*',
        element: <Navigate to="/" replace />,
      },
    ],
  },
] as RouteObject[])

export default router
