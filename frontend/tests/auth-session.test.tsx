/** 凭证失效判定：只有真正的"凭证无效/过期"才允许登出。
 *
 * 回归：此前 AuthContext 拿到 /auth/me 的任何非 ok 响应都会登出，
 * 于是后端限流（429）或短暂抖动（5xx）会把所有在线用户一起踢回登录页——
 * 故障被放大而不是被隔离。
 */
import { describe, expect, it } from "vitest";
import { isInvalidCredential } from "../src/lib/auth";

describe("isInvalidCredential", () => {
  it("treats_1101_as_invalid_credential", () => {
    // 1101 = 后端 ErrCode.UNAUTHORIZED（未认证或凭证过期）
    expect(isInvalidCredential(1101)).toBe(true);
  });

  it("treats_raw_401_as_invalid_credential", () => {
    // api-client 遇到非 JSON 响应时直接回填 HTTP 状态码
    expect(isInvalidCredential(401)).toBe(true);
  });

  it("does_not_treat_rate_limit_as_invalid_credential", () => {
    // 429：请求被拒只说明"现在太挤"，令牌照样有效
    expect(isInvalidCredential(429)).toBe(false);
  });

  it("does_not_treat_server_error_as_invalid_credential", () => {
    // 5xx：后端在抖，令牌照样有效
    expect(isInvalidCredential(500)).toBe(false);
    expect(isInvalidCredential(503)).toBe(false);
  });

  it("does_not_treat_forbidden_as_invalid_credential", () => {
    // 1201 = FORBIDDEN：恰恰说明身份有效，只是权限不够
    expect(isInvalidCredential(1201)).toBe(false);
  });

  it("handles_missing_code", () => {
    expect(isInvalidCredential(null)).toBe(false);
    expect(isInvalidCredential(undefined)).toBe(false);
  });
});
