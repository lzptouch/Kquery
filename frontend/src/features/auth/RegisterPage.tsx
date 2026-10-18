/**
 * 注册页。
 *
 * 注册只负责建号；成功后仍走一次登录流程（而不是"注册即登录"），
 * 这样"建号成功"与"拿到令牌"两件事在代码上是分开的，
 * 不会出现"建号成功但令牌拿不到"时前端却以为已经登录的状态。
 *
 * 角色由后端硬编码为最小权限，本页不提供角色选择——
 * 给了选择框而实际忽略，比不给更容易让人误解。
 */
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "./AuthContext";

export function RegisterPage() {
  const { register, login } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    if (password !== confirm) {
      setError("两次输入的密码不一致");
      return;
    }
    setBusy(true);
    try {
      const message = await register(username.trim(), password);
      if (message) {
        setError(message);
        return;
      }
      // 建号成功后自动登录，省掉用户再输一遍同样的口令
      const loginError = await login(username.trim(), password);
      if (loginError) {
        setError(`注册成功，但自动登录失败：${loginError}`);
        return;
      }
      navigate("/", { replace: true });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <h1 className="auth-title">注册账号</h1>
        <p className="auth-subtitle">新账号默认为最小权限，仅可使用对话功能</p>

        <label className="field">
          <span className="field-label">用户名</span>
          <input
            className="input"
            name="username"
            autoComplete="username"
            minLength={3}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
          />
        </label>

        <label className="field">
          <span className="field-label">密码</span>
          <input
            className="input"
            type="password"
            name="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          <span className="auth-hint">至少 8 位，需同时包含字母和数字</span>
        </label>

        <label className="field">
          <span className="field-label">确认密码</span>
          <input
            className="input"
            type="password"
            name="confirm"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            required
          />
        </label>

        {error && (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}

        <button className="btn btn-primary auth-submit" type="submit" disabled={busy}>
          {busy ? "提交中…" : "注册"}
        </button>

        <p className="auth-hint">
          已有账号？<Link to="/login">登录</Link>
        </p>
      </form>
    </div>
  );
}
