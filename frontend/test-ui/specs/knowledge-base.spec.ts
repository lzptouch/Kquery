import { expect, test } from '@playwright/test'
import { KnowledgeBasePage } from '../pages/KnowledgeBasePage'
import { loginAsNewUser } from '../fixtures/auth'

test.describe('knowledge base', () => {
  test.beforeEach(async ({ page, baseURL }) => {
    await loginAsNewUser(page, baseURL ?? 'http://localhost:5173')
  })

  test('knowledge base page loads with create button', async ({ page }) => {
    const kb = new KnowledgeBasePage(page)
    await kb.goto()
    await expect(page.locator('button').filter({ hasText: '创建知识库' }).first()).toBeVisible()
  })
})
