import { RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from '@/components/ui/toast'
import { TooltipProvider } from '@/components/ui/tooltip'
import { router } from '@/router'

// 创建 React Query 客户端实例（用于服务端状态管理）
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5 * 60 * 1000,     // 数据在5分钟内视为新鲜
      gcTime: 30 * 60 * 1000,       // 缓存保留30分钟
      retry: 2,                     // 失败重试2次
      refetchOnWindowFocus: false,  // 窗口聚焦时不重新请求
    },
    mutations: {
      retry: 1,
    },
  },
})

/**
 * 根应用组件
 */
export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Toaster>
        <TooltipProvider delayDuration={0}>
          <RouterProvider router={router} />
        </TooltipProvider>
      </Toaster>
    </QueryClientProvider>
  )
}
