/**
 * 登录/注册页面 (P2-1)
 */

import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { BrainCircuit, Eye, EyeOff, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { authService } from '@/services/authService'

export function LoginPage() {
  const navigate = useNavigate()
  const [isLogin, setIsLogin] = useState(true)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [email, setEmail] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setIsLoading(true)

    try {
      if (isLogin) {
        await authService.login({ username, password })
      } else {
        await authService.register({ username, password, email: email || undefined })
        await authService.login({ username, password })
      }
      navigate('/')
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message || '操作失败')
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-background to-accent/30 p-4">
      <Card className="w-full max-w-md shadow-lg">
        <CardHeader className="text-center pb-2">
          <div className="flex justify-center mb-3">
            <div className="p-3 rounded-2xl bg-primary/10">
              <BrainCircuit className="h-8 w-8 text-primary" />
            </div>
          </div>
          <CardTitle className="text-2xl">{isLogin ? '欢迎回来' : '创建账号'}</CardTitle>
          <CardDescription>
            {isLogin ? '登录你的 KQuery Pro 账号' : '注册新的 KQuery Pro 账号'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            {error && (
              <div className="bg-destructive/10 text-destructive px-4 py-3 rounded-lg text-sm">{error}</div>
            )}

            <div>
              <label className="text-sm font-medium">用户名</label>
              <Input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="输入用户名"
                required
                minLength={3}
                className="mt-1"
              />
            </div>

            {!isLogin && (
              <div>
                <label className="text-sm font-medium">邮箱（选填）</label>
                <Input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="your@email.com"
                  className="mt-1"
                />
              </div>
            )}

            <div>
              <label className="text-sm font-medium">密码</label>
              <div className="relative mt-1">
                <Input
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="输入密码"
                  required
                  minLength={6}
                  className="pr-10"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground"
                >
                  {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>

            <Button type="submit" className="w-full" disabled={isLoading}>
              {isLoading && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              {isLogin ? '登录' : '注册'}
            </Button>

            <p className="text-center text-sm text-muted-foreground">
              {isLogin ? '还没有账号？' : '已有账号？'}
              <button
                type="button"
                onClick={() => { setIsLogin(!isLogin); setError(''); }}
                className="ml-1 text-primary hover:underline font-medium"
              >
                {isLogin ? '立即注册' : '立即登录'}
              </button>
            </p>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}

export default LoginPage
