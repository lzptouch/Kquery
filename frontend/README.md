# KQuery Pro — 前端应用

KQuery Pro 前端是基于 **React 19 + TypeScript + Vite** 构建的现代化单页应用，提供知识库管理、文档处理、智能问答和知识图谱可视化等交互界面。

## 技术栈

| 技术 | 版本 | 用途 |
|------|------|------|
| React | 19 | UI 框架 |
| TypeScript | 5.7+ | 类型系统 |
| Vite | 6+ | 构建工具与开发服务器 |
| Tailwind CSS | 3.4+ | 实用优先的 CSS 框架 |
| shadcn/ui | - | 基于 Radix UI 的高质量组件库 |
| Radix UI | - | 底层无障碍组件原语 |
| Zustand | - | 轻量级状态管理 |
| React Router | v6 | 客户端路由 |
| React Query (TanStack) | v5 | 服务端状态缓存与同步 |
| Axios | - | HTTP 客户端 |
| Lucide React | - | 图标库 |

## 项目结构

```
frontend/
├── src/
│   ├── App.tsx                 # 根应用组件（QueryClient + RouterProvider）
│   ├── router.tsx              # React Router v6 路由配置
│   ├── main.tsx                # 应用入口（React DOM 渲染）
│   ├── App.css                 # 全局样式
│   ├── index.css               # Tailwind 基础导入 + 自定义样式
│   ├── vite-env.d.ts           # Vite 环境类型声明
│   │
│   ├── lib/                    # 工具库
│   │   ├── utils.ts            # 通用工具函数（cn、formatDate、formatFileSize）
│   │   └── api.ts              # API 客户端（axios 封装 + 类型定义）
│   │
│   ├── components/             # 组件层
│   │   ├── layout/             # 布局组件
│   │   │   ├── MainLayout.tsx  # 主布局（Header + Sidebar + Content）
│   │   │   ├── Header.tsx      # 顶部导航栏（Logo + 用户菜单 + 搜索）
│   │   │   └── Sidebar.tsx     # 侧边栏导航
│   │   └── ui/                 # shadcn/ui 组件库
│   │       ├── alert.tsx       # 警告提示
│   │       ├── avatar.tsx      # 头像
│   │       ├── badge.tsx       # 徽章标签
│   │       ├── button.tsx      # 按钮
│   │       ├── card.tsx        # 卡片容器
│   │       ├── dialog.tsx      # 对话框/模态框
│   │       ├── dropdown-menu.tsx # 下拉菜单
│   │       ├── input.tsx       # 输入框
│   │       ├── progress.tsx    # 进度条
│   │       ├── scroll-area.tsx # 滚动区域
│   │       ├── select.tsx      # 选择器
│   │       ├── table.tsx       # 表格
│   │       ├── tabs.tsx        # 标签页
│   │       ├── toast.tsx       # 消息提示
│   │       └── tooltip.tsx     # 工具提示
│   │
│   ├── pages/                  # 页面组件（按路由划分）
│   │   ├── Dashboard.tsx       # 首页概览（关键指标 + 快捷操作 + 最近活动）
│   │   ├── Chat.tsx            # GraphRAG 智能问答（多轮对话界面）
│   │   ├── KnowledgeBases.tsx  # 知识库管理（创建/配置/删除集合）
│   │   ├── Documents.tsx       # 文档管理（上传/查看/处理文档）
│   │   ├── KnowledgeGraph.tsx  # 知识图谱可视化（实体浏览/关系探索）
│   │   ├── SearchResults.tsx   # 全局搜索结果页
│   │   └── Settings.tsx        # 系统设置（参数配置）
│   │
│   ├── stores/                 # Zustand 状态管理
│   │   ├── useChatStore.ts     # 聊天状态（消息/会话/知识库选择）
│   │   ├── useDocumentStore.ts # 文档状态（文档列表/上传进度）
│   │   ├── useGraphStore.ts    # 图谱状态（实体/关系/可视化配置）
│   │   └── useKbStore.ts       # 知识库状态（集合列表/统计）
│   │
│   └── assets/                 # 静态资源（图片/字体等）
│
├── index.html                  # HTML 入口模板
├── package.json                # 依赖声明与脚本
├── vite.config.ts              # Vite 构建配置
├── tsconfig.json               # TypeScript 根配置
├── tsconfig.app.json           # 应用编译配置
├── tsconfig.node.json          # Node 编译配置
├── tailwind.config.js          # Tailwind CSS 配置
├── postcss.config.js           # PostCSS 配置
├── eslint.config.js            # ESLint 配置
└── Dockerfile                  # 前端容器镜像构建
```

## 页面路由

| 路由 | 页面组件 | 布局 | 说明 |
|------|---------|------|------|
| `/` | `Dashboard` | `MainLayout` | 首页概览，展示系统关键指标和快捷操作入口 |
| `/chat` | `Chat` | `MainLayout` | GraphRAG 智能问答，支持知识库选择和多轮对话 |
| `/knowledge-bases` | `KnowledgeBases` | `MainLayout` | 知识库管理，创建/配置/查看集合 |
| `/documents` | `Documents` | `MainLayout` | 文档管理，上传/查看/处理文档 |
| `/knowledge-graph` | `KnowledgeGraph` | `MainLayout` | 知识图谱可视化浏览和实体关系探索 |
| `/search` | `SearchResults` | `MainLayout` | 全局搜索结果展示 |
| `/settings` | `Settings` | `MainLayout` | 系统设置和参数配置 |
| `*` | - | `MainLayout` | 404 重定向到首页 |

### 路由配置（`router.tsx`）

```tsx
export const router = createBrowserRouter([
  {
    path: '/',
    element: <MainLayout />,
    children: [
      { index: true, lazy: lazyComponent('@/pages/Dashboard') },
      { path: 'knowledge-bases', lazy: lazyComponent('@/pages/KnowledgeBases') },
      { path: 'documents', lazy: lazyComponent('@/pages/Documents') },
      { path: 'knowledge-graph', lazy: lazyComponent('@/pages/KnowledgeGraph') },
      { path: 'chat', lazy: lazyComponent('@/pages/Chat') },
      { path: 'settings', lazy: lazyComponent('@/pages/Settings') },
      { path: 'search', lazy: lazyComponent('@/pages/SearchResults') },
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
])
```

所有业务页面均采用 **React.lazy 懒加载**，优化首屏性能。

## 组件体系

### 布局组件（`components/layout/`）

#### `MainLayout.tsx`
- 组合 `Header` + `Sidebar` + `Outlet`
- 提供统一的页面框架结构
- 响应式布局：移动端自动隐藏侧边栏

#### `Header.tsx`
- 系统 Logo 和品牌名称
- 全局搜索框
- 系统菜单入口（设置）
- 系统状态指示器

#### `Sidebar.tsx`
- 主导航菜单（Dashboard / Chat / Knowledge Bases / Documents / Knowledge Graph）
- 当前路由高亮
- 可折叠的二级菜单

### UI 组件（`components/ui/`）

基于 **shadcn/ui** 的组件库，所有组件均支持：
- 暗色/亮色主题适配
- 无障碍访问（ARIA 属性）
- 键盘导航
- Tailwind CSS 自定义样式

| 组件 | 用途 | 关键 props |
|------|------|-----------|
| `Alert` | 警告/提示消息 | `variant`（default/destructive） |
| `Avatar` | 用户头像 | `src`, `fallback` |
| `Badge` | 状态标签 | `variant`（default/secondary/destructive/outline/success/info） |
| `Button` | 按钮 | `variant`, `size`, `disabled` |
| `Card` | 卡片容器 | - |
| `Dialog` | 模态对话框 | `open`, `onOpenChange` |
| `DropdownMenu` | 下拉菜单 | - |
| `Input` | 文本输入框 | `type`, `placeholder`, `disabled` |
| `Progress` | 进度条 | `value`, `max` |
| `ScrollArea` | 自定义滚动区域 | - |
| `Select` | 选择器 | `value`, `onValueChange` |
| `Table` | 表格 | - |
| `Tabs` | 标签页 | `defaultValue` |
| `Toast` | 消息提示 | 通过 `Toaster` 全局管理 |
| `Tooltip` | 工具提示 | `content` |

## 状态管理

采用 **Zustand** 进行状态管理，按业务域划分为多个 Store：

### `useChatStore` — 聊天状态

```typescript
interface ChatState {
  messages: Message[]
  currentSessionId: string | null
  sessions: ConversationSession[]
  isLoading: boolean
  isStreaming: boolean
  error: string | null
  activeCollectionName: string

  sendMessage: (content: string) => Promise<void>
  clearMessages: () => void
  createSession: (collectionName?: string) => ConversationSession
  setActiveCollection: (collectionName: string) => void
  retryLastMessage: () => Promise<void>
}
```

**核心流程：**
1. 用户选择知识库（`setActiveCollection`）
2. 输入问题并发送（`sendMessage`）
3. 调用后端 `/agent/chat` API
4. 流式更新消息列表（加载态 → 完整回复）
5. 显示使用的工具（如 `rag_search`）

### `useKbStore` — 知识库状态

管理知识库集合列表、统计信息和当前选中集合。

### `useDocumentStore` — 文档状态

管理文档上传队列、处理进度和文档列表。

### `useGraphStore` — 图谱状态

管理实体查询结果、关系数据和可视化配置。

## API 客户端（`lib/api.ts`）

基于 **Axios** 封装的 API 客户端，提供：

- 基础 URL 配置
- 请求/响应拦截器（错误处理）
- 类型安全的 API 方法

### 主要 API 模块

```typescript
// RAG API
ragApi.processDocument(file, collectionName)    // 处理文档
ragApi.queryDocuments(query, collectionName)    // 查询文档
ragApi.listCollections()                        // 列出集合
ragApi.getCollectionStats(name)                 // 集合统计

// Agent API
agentApi.chat({ query, collection_name })       // 智能问答
agentApi.uploadDocument(file, collectionName)   // 上传文档
agentApi.getSession(sessionId)                  // 会话信息
agentApi.clearSession(sessionId)                // 清空会话

```

## React Query 配置

```typescript
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5 * 60 * 1000,     // 数据在 5 分钟内视为新鲜
      gcTime: 30 * 60 * 1000,       // 缓存保留 30 分钟
      retry: 2,                     // 失败重试 2 次
      refetchOnWindowFocus: false,  // 窗口聚焦时不重新请求
    },
    mutations: {
      retry: 1,
    },
  },
})
```

## 快速开始

### 环境要求

- Node.js 20+
- npm 10+ 或 pnpm 9+

### 安装依赖

```bash
cd frontend
npm install
```

### 开发服务器

```bash
npm run dev
```

启动在 `http://localhost:5173`，热重载已启用。

### 构建生产版本

```bash
npm run build
```

输出到 `dist/` 目录，可直接部署到静态服务器。

### 预览生产构建

```bash
npm run preview
```

### 代码检查

```bash
npm run lint        # ESLint 检查
```

## 开发指南

### 添加新页面

1. 在 `src/pages/` 下创建页面组件：

```tsx
// src/pages/MyPage.tsx
export function MyPage() {
  return <div>My Page</div>
}
export default MyPage
```

2. 在 `src/router.tsx` 中添加路由：

```tsx
{ path: 'my-page', lazy: lazyComponent('@/pages/MyPage') },
```

3. 在 `Sidebar.tsx` 中添加导航链接。

### 添加新组件

**业务组件**：放在 `src/components/` 下合适的子目录。

**UI 组件**：使用 shadcn/ui CLI 添加：

```bash
npx shadcn add <component-name>
```

### 添加新 Store

```typescript
// src/stores/useMyStore.ts
import { create } from 'zustand'

interface MyState {
  data: string[]
  setData: (data: string[]) => void
}

export const useMyStore = create<MyState>((set) => ({
  data: [],
  setData: (data) => set({ data }),
}))
```

### 调用后端 API

```typescript
import { useQuery, useMutation } from '@tanstack/react-query'
import { ragApi } from '@/lib/api'

// 查询
const { data } = useQuery({
  queryKey: ['collections'],
  queryFn: () => ragApi.listCollections(),
})

// 变更
const mutation = useMutation({
  mutationFn: (file: File) => ragApi.processDocument(file, 'default'),
  onSuccess: () => {
    // 成功后刷新列表
    queryClient.invalidateQueries({ queryKey: ['collections'] })
  },
})
```

## 样式体系

### Tailwind CSS

基于 **Tailwind CSS 3.4+**，使用 `tailwind.config.js` 自定义主题：

- 颜色系统：primary、secondary、destructive、muted、accent 等
- 圆角系统：sm、md、lg、xl、full
- 阴影系统：sm、md、lg、xl

### CSS 变量（暗色/亮色主题）

```css
:root {
  --background: 0 0% 100%;
  --foreground: 222.2 84% 4.9%;
  --primary: 221.2 83.2% 53.3%;
  --primary-foreground: 210 40% 98%;
  /* ... */
}

.dark {
  --background: 222.2 84% 4.9%;
  --foreground: 210 40% 98%;
  /* ... */
}
```

### 常用工具类

| 类名 | 说明 |
|------|------|
| `animate-fade-in` | 淡入动画 |
| `text-muted-foreground` | 次要文字颜色 |
| `bg-accent` | 强调背景色 |
| `hover:shadow-lg` | 悬停阴影效果 |
| `transition-all` | 全部属性过渡 |

## 构建与部署

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `VITE_API_BASE_URL` | 后端 API 地址 | `http://localhost:8000/api/v1` |
| `VITE_APP_TITLE` | 应用标题 | `KQuery Pro` |

在 `.env` 或 `.env.production` 中配置：

```bash
VITE_API_BASE_URL=https://api.kquery.example.com/api/v1
```

### Docker 部署

```bash
# 构建镜像
docker build -t kquery-frontend .

# 运行容器
docker run -d -p 3000:80 -e VITE_API_BASE_URL=https://api.example.com kquery-frontend
```

### Nginx 配置示例

```nginx
server {
    listen 80;
    server_name kquery.example.com;
    root /var/www/kquery-frontend/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## 浏览器兼容性

- Chrome 120+
- Firefox 120+
- Safari 17+
- Edge 120+

## 性能优化

- **路由懒加载**：所有页面组件使用 `React.lazy` 动态导入
- **React Query 缓存**：服务端数据智能缓存，减少重复请求
- **图片优化**：使用 Vite 内置的资源处理
- **Tree Shaking**：Vite 自动剔除未使用代码
- **代码分割**：按路由自动分割代码块

## 待办事项

- [ ] 实现 SSE 流式输出（Chat 页面）
- [ ] 知识图谱 D3.js/G6 可视化组件
- [ ] 文档上传进度实时显示
- [ ] 移动端适配优化
