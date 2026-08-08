import { Page, expect } from '@playwright/test'

export class KnowledgeBasePage {
  constructor(private page: Page) {}

  async goto() {
    await this.page.goto('/knowledge-bases')
    await expect(this.page).toHaveURL(/\/knowledge-bases/)
  }

  async create(name: string, description: string) {
    await this.page.locator('button').filter({ hasText: '创建知识库' }).first().click()
    await this.page.locator('input#kb-name').fill(name)
    await this.page.locator('input#kb-desc').fill(description)
    await this.page.locator('button').filter({ hasText: '创建' }).last().click()
  }

  async expectKb(name: string) {
    await expect(this.page.locator('text=' + name).first()).toBeVisible()
  }

  async uploadDocument(filePath: string) {
    const fileInput = this.page.locator('input[type="file"]').first()
    await fileInput.setInputFiles(filePath)
  }
}
