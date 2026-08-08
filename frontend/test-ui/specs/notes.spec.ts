import { expect, test } from '@playwright/test'
import { NotePage } from '../pages/NotePage'
import { loginAsNewUser } from '../fixtures/auth'

test.describe('notes', () => {
  test.beforeEach(async ({ page, baseURL }) => {
    await loginAsNewUser(page, baseURL ?? 'http://localhost:5173')
  })

  test('notes page loads with input', async ({ page }) => {
    const note = new NotePage(page)
    await note.goto()
    await expect(page.locator('input[placeholder="输入笔记标题..."]').first()).toBeVisible()
  })
})
