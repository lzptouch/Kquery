import { Page } from '@playwright/test'

export async function loginAsNewUser(page: Page, baseURL: string): Promise<{ username: string; password: string }> {
  const username = `uitest_${Date.now()}_${Math.floor(Math.random() * 1000)}`
  const password = 'Test123456'

  const apiUrl = `${baseURL.replace(/\/$/, '')}/api/v1`
  // 注册新用户
  const registerResp = await page.request.post(`${apiUrl}/auth/register`, {
    data: { username, password, email: `${username}@example.com` },
  })
  if (!registerResp.ok() && registerResp.status() !== 409) {
    const text = await registerResp.text()
    throw new Error(`注册测试用户失败: ${registerResp.status()} ${text}`)
  }

  // 通过 UI 登录
  await page.goto('/login')
  await page.locator('input[placeholder="输入用户名"]').first().fill(username)
  await page.locator('input[placeholder="输入密码"]').first().fill(password)
  await page.locator('button[type="submit"]').first().click()
  await page.waitForURL(/\/$/, { timeout: 10000 })

  return { username, password }
}
