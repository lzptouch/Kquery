import { Page, expect } from '@playwright/test'

export class ChatPage {
  constructor(private page: Page) {}

  get input() { return this.page.locator('input[placeholder*="输入问题"]').first() }
  get sendButton() { return this.page.locator('form button[type="submit"]').first() }
  get messages() { return this.page.locator('.chat-message, [data-testid="chat-message"]').first() }

  async goto() {
    await this.page.goto('/chat')
    await expect(this.page).toHaveURL(/chat/)
  }

  async send(text: string) {
    await this.input.fill(text)
    await this.input.press('Enter')
  }

  async expectMessage(text: string) {
    await expect(this.page.locator('.chat-message').filter({ hasText: text }).first()).toBeVisible()
  }
}
