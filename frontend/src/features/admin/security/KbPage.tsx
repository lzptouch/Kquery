/**
 * 知识库管理：KB 实体 CRUD + 按用户授权。
 *
 * 授权交互复用文档 ACL 页的三件套（风险等级 / 影响人数反查 / 二次确认），
 * 字段名与 acl.set_acl 完全一致（后端 kb.set_grants 保证），结果回显区零改动复用。
 */
import { useCallback, useEffect, useState } from "react";
import {
  createKnowledgeBase,
  deleteKnowledgeBase,
  fetchKbGrants,
  fetchKnowledgeBases,
  fetchUsers,
  setKbGrants,
  updateKnowledgeBase,
} from "../../../lib/api-client";
import type { KbGrantResult, KnowledgeBase, UserRecord } from "../../../types";

const RISK_TEXT: Record<string, { label: string; className: string }> = {
  critical: { label: "最高风险", className: "st-fail" },
  high: { label: "高风险", className: "st-warn" },
  low: { label: "低风险", className: "st-pass" },
};

export function KbPage() {
  const [kbs, setKbs] = useState<KnowledgeBase[] | null>(null);
  const [users, setUsers] = useState<UserRecord[]>([]);
  const [error, setError] = useState("");
  // 新建
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [newPublic, setNewPublic] = useState(false);
  // 编辑
  const [editing, setEditing] = useState<KnowledgeBase | null>(null);
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [editPublic, setEditPublic] = useState(false);
  // 授权
  const [granting, setGranting] = useState<KnowledgeBase | null>(null);
  const [grantedIds, setGrantedIds] = useState<Set<string>>(new Set());
  const [confirmText, setConfirmText] = useState("");
  const [result, setResult] = useState<KbGrantResult | null>(null);

  const load = useCallback(async () => {
    setError("");
    const [kbEnv, userEnv] = await Promise.all([fetchKnowledgeBases(), fetchUsers()]);
    if (kbEnv.data) {
      setKbs(kbEnv.data);
    } else {
      setError(kbEnv.error?.message ?? "知识库列表加载失败");
      setKbs([]);
    }
    if (userEnv.data) setUsers(userEnv.data.filter((u) => u.enabled));
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onCreate = async () => {
    const name = newName.trim();
    if (!name) return;
    const envelope = await createKnowledgeBase({ name, description: newDesc.trim(), is_public: newPublic });
    if (envelope.data) {
      setCreating(false);
      setNewName("");
      setNewDesc("");
      setNewPublic(false);
      void load();
    } else {
      setError(envelope.error?.message ?? "创建失败");
    }
  };

  const openEdit = (kb: KnowledgeBase) => {
    setEditing(kb);
    setEditName(kb.name);
    setEditDesc(kb.description);
    setEditPublic(kb.is_public);
  };

  const onSaveEdit = async () => {
    if (!editing) return;
    const envelope = await updateKnowledgeBase(editing.kb_id, {
      name: editName.trim(), description: editDesc.trim(), is_public: editPublic,
    });
    if (envelope.data) {
      setEditing(null);
      void load();
    } else {
      setError(envelope.error?.message ?? "保存失败");
    }
  };

  const onDelete = async (kb: KnowledgeBase) => {
    if (kb.doc_count > 0) {
      setError(`「${kb.name}」内仍有 ${kb.doc_count} 个文档，请先在文档页移出或删除`);
      return;
    }
    if (!window.confirm(`确认删除知识库「${kb.name}」？授权关系将一并清除。`)) return;
    const envelope = await deleteKnowledgeBase(kb.kb_id);
    if (envelope.data) void load();
    else setError(envelope.error?.message ?? "删除失败");
  };

  const openGrant = async (kb: KnowledgeBase) => {
    setGranting(kb);
    setConfirmText("");
    setResult(null);
    const envelope = await fetchKbGrants(kb.kb_id);
    if (envelope.data) setGrantedIds(new Set(envelope.data.map((g) => g.user_id)));
    else setError(envelope.error?.message ?? "授权名单加载失败");
  };

  const toggleGrant = (userId: string) => {
    setGrantedIds((prev) => {
      const next = new Set(prev);
      if (next.has(userId)) next.delete(userId);
      else next.add(userId);
      return next;
    });
  };

  // 扩权（勾选人数变多）需二次确认：与文档 ACL 的口径一致
  const expanding = granting ? grantedIds.size > granting.granted_count : false;
  const needConfirm = Boolean(granting) && !granting!.is_public && expanding;
  const confirmOk = !needConfirm || confirmText.trim() === granting?.name;

  const onSaveGrants = async () => {
    if (!granting || !confirmOk) return;
    const envelope = await setKbGrants(granting.kb_id, [...grantedIds]);
    if (envelope.data) {
      setResult(envelope.data);
      void load();
    } else {
      setError(envelope.error?.message ?? "授权保存失败");
    }
  };

  if (kbs === null) {
    return (
      <div className="card" role="status" aria-live="polite">
        正在加载知识库列表…
      </div>
    );
  }

  return (
    <div className="page">
      <div className="page-inner">
        {error && (
          <div className="error-inline" role="alert">
            {error}
            <button type="button" onClick={() => void load()}>重试</button>
          </div>
        )}
        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <h3>知识库管理</h3>
            <button type="button" onClick={() => setCreating(true)}>新建知识库</button>
          </div>
          {kbs.length === 0 ? (
            <p className="muted">暂无知识库。</p>
          ) : (
            <table className="files-table">
              <thead>
                <tr><th>名称</th><th>可见性</th><th>文档数</th><th>授权人数</th><th>操作</th></tr>
              </thead>
              <tbody>
                {kbs.map((kb) => (
                  <tr key={kb.kb_id}>
                    <td>
                      <strong>{kb.name}</strong>
                      {kb.description && <div className="muted">{kb.description}</div>}
                    </td>
                    <td>
                      <span className={`status-tag ${kb.is_public ? "st-pass" : "st-warn"}`}>
                        {kb.is_public ? "租户内公开" : "仅授权"}
                      </span>
                    </td>
                    <td>{kb.doc_count}</td>
                    <td>{kb.is_public ? "全员" : `${kb.granted_count} 人`}</td>
                    <td>
                      <button type="button" onClick={() => void openGrant(kb)}>授权</button>{" "}
                      <button type="button" onClick={() => openEdit(kb)}>编辑</button>{" "}
                      {kb.kb_id !== "default" && (
                        <button type="button" onClick={() => void onDelete(kb)}>删除</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {creating && (
          <div className="modal-mask" onClick={() => setCreating(false)}>
            <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="新建知识库">
              <h3>新建知识库</h3>
              <label style={{ display: "block", marginBottom: 8 }}>
                名称
                <input className="probe-input" style={{ width: "100%", marginTop: 4 }}
                       value={newName} onChange={(e) => setNewName(e.target.value)} />
              </label>
              <label style={{ display: "block", marginBottom: 8 }}>
                描述（可选）
                <input className="probe-input" style={{ width: "100%", marginTop: 4 }}
                       value={newDesc} onChange={(e) => setNewDesc(e.target.value)} />
              </label>
              <label className="graph-filter" style={{ marginBottom: 8 }}>
                <input type="checkbox" checked={newPublic} onChange={(e) => setNewPublic(e.target.checked)} />
                租户内公开（全员可见，无需逐个授权）
              </label>
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                <button type="button" onClick={() => setCreating(false)}>取消</button>
                <button type="button" onClick={() => void onCreate()} disabled={!newName.trim()}>创建</button>
              </div>
            </div>
          </div>
        )}

        {editing && (
          <div className="modal-mask" onClick={() => setEditing(null)}>
            <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="编辑知识库">
              <h3>编辑：{editing.name}</h3>
              <label style={{ display: "block", marginBottom: 8 }}>
                名称
                <input className="probe-input" style={{ width: "100%", marginTop: 4 }}
                       value={editName} onChange={(e) => setEditName(e.target.value)} />
              </label>
              <label style={{ display: "block", marginBottom: 8 }}>
                描述
                <input className="probe-input" style={{ width: "100%", marginTop: 4 }}
                       value={editDesc} onChange={(e) => setEditDesc(e.target.value)} />
              </label>
              <label className="graph-filter" style={{ marginBottom: 8 }}>
                <input type="checkbox" checked={editPublic} onChange={(e) => setEditPublic(e.target.checked)} />
                租户内公开（全体可见）
              </label>
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                <button type="button" onClick={() => setEditing(null)}>取消</button>
                <button type="button" onClick={() => void onSaveEdit()} disabled={!editName.trim()}>保存</button>
              </div>
            </div>
          </div>
        )}

        {granting && (
          <div className="modal-mask" onClick={() => setGranting(null)}>
            <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="授权">
              <h3>授权：{granting.name}</h3>
              {granting.is_public && (
                <p className="muted">该库为租户内公开，全员可见；下方名单仅用于显式登记。</p>
              )}
              <div style={{ maxHeight: 280, overflow: "auto", marginBottom: 8 }}>
                {users.length === 0 ? (
                  <p className="muted">暂无启用用户。</p>
                ) : (
                  users.map((u) => (
                    <label key={u.user_id} className="graph-filter" style={{ display: "block" }}>
                      <input
                        type="checkbox"
                        checked={grantedIds.has(u.user_id)}
                        onChange={() => toggleGrant(u.user_id)}
                      />
                      {u.user_id}（{u.roles.join(",") || "未分配角色"}）
                    </label>
                  ))
                )}
              </div>
              {needConfirm && (
                <div className="error-inline" role="alert" style={{ margin: "8px 0" }}>
                  扩权操作：新增 {grantedIds.size - granting.granted_count} 人可访问。
                  请输入知识库名 <strong>{granting.name}</strong> 确认：
                  <input className="probe-input" style={{ width: "100%", marginTop: 4 }}
                         value={confirmText} onChange={(e) => setConfirmText(e.target.value)} />
                </div>
              )}
              {result && (
                <div className={result.risk === "low" ? "pending-note" : "degrade-note"}>
                  <span className={`status-tag ${RISK_TEXT[result.risk]?.className}`}>
                    {RISK_TEXT[result.risk]?.label}
                  </span>{" "}
                  新增 {result.impact.added_count} 人可访问，减少 {result.impact.removed_count} 人
                </div>
              )}
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                <button type="button" onClick={() => setGranting(null)}>关闭</button>
                <button type="button" onClick={() => void onSaveGrants()} disabled={!confirmOk}>
                  保存（显示影响人数）
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
