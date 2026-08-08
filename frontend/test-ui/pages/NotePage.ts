import { Page, expect } from '@playwright/test'

export class NotePage {
  constructor(private page: Page) {}

  async goto() {
    await this.page.goto('/notes')
    await expect(this.page).toHaveURL(/\/notes/)
  }

  async create(title: string, content: string) {
    await this.page.locator('input[placeholder="输入笔记标题..."]').first().fill(title)
    await this.page.locator('button').filter({ hasText: '新建笔记' }).first().click()
  }

  async expectNote(title: string) {
    await expect(this.page.locator('text=' + title).first()).toBeVisible()
  }
}
