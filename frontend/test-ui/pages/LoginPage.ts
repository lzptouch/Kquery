import { Page, expect } from '@playwright/test'

export class LoginPage {
  constructor(private page: Page) {}

  get username() { return this.page.locator('input[placeholder="输入用户名"]').first() }
  get password() { return this.page.locator('input[placeholder="输入密码"]').first() }
  get submitButton() { return this.page.locator('button[type="submit"]').first() }

  async goto() {
    await this.page.goto('/login')
    await expect(this.page).toHaveURL(/\/login/)
  }

  async login(username: string, password: string) {
    await this.username.fill(username)
    await this.password.fill(password)
    await this.submitButton.click()
  }

  async expectError() {
    await expect(
      this.page.locator('div').filter({ hasText: /失败|错误|Error|401|不存在|Invalid|Incorrect/ }).first()
    ).toBeVisible()
  }
}
