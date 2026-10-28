/** 用户与权限：列表 + 行内编辑角色/组/状态 + 重置口令 + 可访问知识库（只读）。 */
import { useCallback, useEffect, useState } from "react";
import { editUser, fetchUserKbs, fetchUsers, setUserPassword } from "../../../lib/api-client";
import type { UserRecord } from "../../../types";
import { PermissionMatrix } from "./PermissionMatrix";

export function UsersPage() {
  const [users, setUsers] = useState<UserRecord[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ roles: string; groups: string; enabled: boolean }>({ roles: "", groups: "", enabled: true });
  // 口令重置：没有它就只有 admin 能登录（只有 admin 拿得到初始口令），
  // 管理员无法给其他账号开通使用
  const [pwdFor, setPwdFor] = useState<string | null>(null);
  const [pwd, setPwd] = useState("");
  const [pwdMsg, setPwdMsg] = useState("");
  // 可访问知识库：只读反查（授权的写入路径唯一在知识库管理页，防止双写不一致）
  const [kbsFor, setKbsFor] = useState<string | null>(null);
  const [kbsOf, setKbsOf] = useState<{ kb_id: string; name: string; is_public: boolean }[]>([]);
  // 四态补齐：此前本页面连 error 态都没有，请求失败时静默显示空表格，
  // 管理员会以为"库里就是一个用户都没有"
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const envelope = await fetchUsers();
      if (envelope.data) setUsers(envelope.data);
      else setError(envelope.error?.message ?? "用户列表加载失败");
    } catch (err) {
      setError((err as Error).message || "用户列表加载失败，请检查网络");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <div className="page"><div className="page-inner">
        <div className="card" role="status" aria-live="polite">正在加载用户列表…</div>
      </div></div>
    );
  }
  if (error) {
    return (
      <div className="page"><div className="page-inner">
        <div className="card" role="alert">
          <p>{error}</p>
          <button type="button" className="btn" onClick={() => void load()}>重试</button>
        </div>
      </div></div>
    );
  }

  return (
    <div className="page">
      <div className="page-inner">
        <PermissionMatrix />
        <div className="card">
          <h3 style={{ marginTop: 0 }}>用户与权限</h3>
          {users.length === 0 ? (
            <p className="muted">暂无用户。</p>
          ) : (
          <table className="files-table">
            <thead>
              <tr><th>用户</th><th>租户</th><th>角色</th><th>组</th><th>状态</th><th>可访问知识库</th><th>操作</th></tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.user_id}>
                  <td className="mono">{u.user_id}</td>
                  <td>{u.tenant_id}</td>
                  <td>
                    {editing === u.user_id ? (
                      <input
                        className="probe-input" style={{ width: 140 }}
                        value={draft.roles}
                        onChange={(e) => setDraft({ ...draft, roles: e.target.value })}
                        placeholder="逗号分隔，如 hr,finance"
                      />
                    ) : (
                      u.roles.map((r) => <span key={r} className="issue-tag resolved" style={{ marginRight: 4 }}>{r}</span>)
                    )}
                  </td>
                  <td>
                    {editing === u.user_id ? (
                      <input
                        className="probe-input" style={{ width: 120 }}
                        value={draft.groups}
                        onChange={(e) => setDraft({ ...draft, groups: e.target.value })}
                        placeholder="逗号分隔"
                      />
                    ) : (
                      u.groups.join(", ") || "-"
                    )}
                  </td>
                  <td>
                    {editing === u.user_id ? (
                      <label className="graph-filter">
                        <input
                          type="checkbox" checked={draft.enabled}
                          onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}
                        />
                        启用
                      </label>
                    ) : (
                      <span className={`status-tag ${u.enabled ? "st-pass" : "st-fail"}`}>{u.enabled ? "启用" : "禁用"}</span>
                    )}
                  </td>
                  <td>
                    <button
                      type="button" className="btn-text"
                      onClick={() => {
                        if (kbsFor === u.user_id) {
                          setKbsFor(null);
                          return;
                        }
                        setKbsFor(u.user_id);
                        void fetchUserKbs(u.user_id).then((env) => setKbsOf(env.data ?? []));
                      }}
                    >
                      查看
                    </button>
                    {kbsFor === u.user_id && (
                      <div className="muted" style={{ marginTop: 4 }}>
                        {kbsOf.length === 0
                          ? "仅默认公开知识库"
                          : kbsOf.map((k) => (
                              <div key={k.kb_id}>{k.name}{k.is_public ? "（公开）" : ""}</div>
                            ))}
                        <a href="/admin/kb">去授权</a>
                      </div>
                    )}
                  </td>
                  <td>
                    {editing === u.user_id ? (
                      <>
                        <button
                          type="button" className="btn-text"
                          onClick={() => {
                            void editUser(u.user_id, {
                              roles: draft.roles.split(",").map((s) => s.trim()).filter(Boolean),
                              groups: draft.groups.split(",").map((s) => s.trim()).filter(Boolean),
                              enabled: draft.enabled,
                            }).then(() => { setEditing(null); void load(); });
                          }}
                        >
                          保存
                        </button>
                        <button type="button" className="btn-text" onClick={() => setEditing(null)}>取消</button>
                      </>
                    ) : (
                      <>
                        <button
                          type="button" className="btn-text"
                          onClick={() => {
                            setEditing(u.user_id);
                            setDraft({ roles: u.roles.join(","), groups: u.groups.join(","), enabled: u.enabled });
                          }}
                        >
                          编辑
                        </button>
                        <button
                          type="button" className="btn-text"
                          style={{ marginLeft: 8 }}
                          onClick={() => {
                            setPwdFor(pwdFor === u.user_id ? null : u.user_id);
                            setPwd("");
                            setPwdMsg("");
                          }}
                        >
                          重置口令
                        </button>
                      </>
                    )}
                    {pwdFor === u.user_id && (
                      <div className="password-reset">
                        <input
                          className="input"
                          type="password"
                          style={{ width: 150 }}
                          autoComplete="new-password"
                          placeholder="至少 8 位，含字母与数字"
                          value={pwd}
                          onChange={(e) => setPwd(e.target.value)}
                        />
                        <button
                          type="button"
                          className="btn btn-primary"
                          style={{ marginLeft: 6 }}
                          onClick={() => {
                            void setUserPassword(u.user_id, pwd).then((env) => {
                              if (env.status === "ok") {
                                setPwdMsg("已重置");
                                setPwd("");
                                setPwdFor(null);
                              } else {
                                setPwdMsg(env.error?.message ?? "重置失败");
                              }
                            });
                          }}
                        >
                          确认
                        </button>
                        {pwdMsg && <span className="auth-error" style={{ marginLeft: 6 }}>{pwdMsg}</span>}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          )}
        </div>
      </div>
    </div>
  );
}
