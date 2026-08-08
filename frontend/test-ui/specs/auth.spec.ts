import { expect, test } from '@playwright/test'
import { LoginPage } from '../pages/LoginPage'
import { loginAsNewUser } from '../fixtures/auth'

test.describe('auth', () => {
  test('login success navigates to home', async ({ page, baseURL }) => {
    await loginAsNewUser(page, baseURL ?? 'http://localhost:5173')
    await expect(page).toHaveURL(/\/$/)
    await expect(page.locator('text=Kquery').first()).toBeVisible()
  })

  test('login with wrong password shows error', async ({ page }) => {
    const login = new LoginPage(page)
    await login.goto()
    await login.login(`nonexistent_${Date.now()}`, 'wrong-password')
    await login.expectError()
    await expect(page).toHaveURL(/login/)
  })
})
