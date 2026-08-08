import { expect, test } from '@playwright/test'
import { ChatPage } from '../pages/ChatPage'
import { loginAsNewUser } from '../fixtures/auth'

test.describe('chat', () => {
  test.beforeEach(async ({ page, baseURL }) => {
    await loginAsNewUser(page, baseURL ?? 'http://localhost:5173')
  })

  test('chat page loads and input is visible', async ({ page }) => {
    const chat = new ChatPage(page)
    await chat.goto()
    await expect(chat.input).toBeVisible()
  })
})
