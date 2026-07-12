import React from 'react'
import { Settings as SettingsIcon, Database } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

/**
 * 系统设置页面
 */
export function Settings() {
  return (
    <div className="space-y-6 animate-fade-in">
      {/* 页面标题 */}
      <div>
        <h1 className="text-3xl font-bold tracking-tight">系统设置</h1>
        <p className="text-muted-foreground mt-1">
          管理系统配置和运行参数
        </p>
      </div>

      {/* 设置选项卡 */}
      <Tabs defaultValue="general" className="space-y-4">
        <TabsList className="grid w-full grid-cols-1 lg:grid-cols-2">
          <TabsTrigger value="general" className="gap-2">
            <SettingsIcon className="h-4 w-4" />
            基本设置
          </TabsTrigger>
          <TabsTrigger value="data" className="gap-2">
            <Database className="h-4 w-4" />
            数据管理
          </TabsTrigger>
        </TabsList>

        {/* 基本设置 */}
        <TabsContent value="general">
          <Card>
            <CardHeader>
              <CardTitle>基本设置</CardTitle>
              <CardDescription>配置系统的基本信息和全局参数</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="text-sm font-medium">系统名称</label>
                  <input 
                    type="text" 
                    defaultValue="KQuery Pro"
                    className="mt-1 input-base"
                  />
                </div>
                <div>
                  <label className="text-sm font-medium">默认语言</label>
                  <select className="mt-1 input-base">
                    <option>简体中文</option>
                    <option>English</option>
                  </select>
                </div>
              </div>
              
              <Button>保存更改</Button>
            </CardContent>
          </Card>
        </TabsContent>

        {/* 数据管理 */}
        <TabsContent value="data">
          <Card>
            <CardHeader>
              <CardTitle>数据源配置</CardTitle>
              <CardDescription>管理数据库、向量库、图数据库连接</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {[
                { name: 'PostgreSQL', status: 'connected', color: 'success' },
                { name: 'Milvus (向量库)', status: 'connected', color: 'success' },
                { name: 'Neo4j (图谱)', status: 'connected', color: 'success' },
                { name: 'Redis (缓存)', status: 'connected', color: 'success' },
              ].map((db) => (
                <div key={db.name} className="flex items-center justify-between p-3 border rounded-lg">
                  <span className="font-medium">{db.name}</span>
                  <Badge variant={db.color as any}>{db.status === 'connected' ? '已连接' : db.status}</Badge>
                </div>
              ))}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  )
}

export default Settings
