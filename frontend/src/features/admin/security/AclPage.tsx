/** 文档 ACL 管理。
 *
 * 风险分级提示：
 * - 扩大授权 → 红色"将新增 N 人可访问"；
 * - 缩小授权 → 黄色"将减少 N 人访问权限"；
 * - 移除拒绝名单 / 改为公开 → 最高风险，需输入文档名二次确认。
 */
import { useCallback, useEffect, useState } from "react";
import { fetchAclList, setDocAcl } from "../../../lib/api-client";
import type { DocAcl } from "../../../types";

const RISK_TEXT: Record<string, { label: string; className: string }> = {
  critical: { label: "最高风险", className: "st-fail" },
  high: { label: "高风险", className: "st-warn" },
  low: { label: "低风险", className: "st-pass" },
};

export function AclPage() {
  const [docs, setDocs] = useState<DocAcl[]>([]);
  const [editing, setEditing] = useState<DocAcl | null>(null);
  const [allow, setAllow] = useState("");
  const [deny, setDeny] = useState("");
  const [isPublic, setIsPublic] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [result, setResult] = useState<{ impact: { added_count: number; removed_count: number; added_users: string[]; removed_users: string[] }; risk: string } | null>(null);

  const load = useCallback(async () => {
    const envelope = await fetchAclList();
    if (envelope.data) setDocs(envelope.data);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openEdit = (d: DocAcl) => {
    setEditing(d);
    setAllow((d.allow ?? []).join(","));
    setDeny((d.deny ?? []).join(","));
    setIsPublic(d.effective_public);
    setConfirmText("");
    setResult(null);
  };

  // 风险预判（保存前展示）：移除拒绝名单 / 改为公开需输入文档名确认
  const removingDeny = editing ? (editing.deny ?? []).length > (deny ? deny.split(",").filter(Boolean).length : 0) : false;
  const makingPublic = editing ? !editing.effective_public && isPublic : false;
  const needConfirm = removingDeny || makingPublic;
  const confirmOk = !needConfirm || confirmText.trim() === editing?.filename;

  const save = async () => {
    if (!editing || !confirmOk) return;
    const envelope = await setDocAcl(editing.doc_id, {
      allow: allow.split(",").map((s) => s.trim()).filter(Boolean),
      deny: deny.split(",").map((s) => s.trim()).filter(Boolean),
      is_public: isPublic,
    });
    if (envelope.data) {
      setResult(envelope.data);
      void load();
    }
  };

  return (
    <div className="page">
      <div className="page-inner">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>文档 ACL 管理</h3>
          <table className="files-table">
            <thead>
              <tr><th>文档</th><th>租户</th><th>授权名单</th><th>拒绝名单</th><th>公开</th><th>操作</th></tr>
            </thead>
            <tbody>
              {docs.map((d) => (
                <tr key={d.doc_id}>
                  <td>{d.filename}</td>
                  <td className="mono">{d.tenant_id}</td>
                  <td>{(d.allow ?? []).join(", ") || "-"}</td>
                  <td>{(d.deny ?? []).join(", ") || "-"}</td>
                  <td>{d.effective_public ? <span className="status-tag st-pass">✓ 公开</span> : <span className="status-tag st-none">✗ 受限</span>}</td>
                  <td><button type="button" className="btn-text" onClick={() => openEdit(d)}>编辑</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {editing && (
          <div className="modal-mask" onClick={() => setEditing(null)}>
            <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="ACL 编辑">
              <h3>ACL：{editing.filename}</h3>
              <div className="config-row">
                <label style={{ flex: 1 }}>
                  授权名单（user:/role:/group: 逗号分隔）
                  <input className="probe-input" style={{ width: "100%", marginTop: 4 }} value={allow} onChange={(e) => setAllow(e.target.value)} placeholder="role:hr, group:exec" />
                </label>
              </div>
              <div className="config-row">
                <label style={{ flex: 1 }}>
                  拒绝名单（优先于授权）
                  <input className="probe-input" style={{ width: "100%", marginTop: 4 }} value={deny} onChange={(e) => setDeny(e.target.value)} placeholder="user:zhangsan" />
                </label>
              </div>
              <div className="config-row">
                <label className="graph-filter">
                  <input type="checkbox" checked={isPublic} onChange={(e) => setIsPublic(e.target.checked)} />
                  租户内公开（全体可见）
                </label>
              </div>

              {needConfirm && (
                <div className="error-inline" role="alert" style={{ margin: "8px 0" }}>
                  ⚠ {removingDeny && "移除拒绝名单后该用户将重新获得访问权。"}
                  {makingPublic && "该文档将对本租户全体可见。"}
                  请输入文档名 <strong>{editing.filename}</strong> 确认：
                  <input className="probe-input" style={{ width: "100%", marginTop: 4 }} value={confirmText} onChange={(e) => setConfirmText(e.target.value)} />
                </div>
              )}

              {result && (
                <div className={result.risk === "low" ? "pending-note" : "degrade-note"} style={{ margin: "8px 0" }}>
                  <span className={`status-tag ${RISK_TEXT[result.risk]?.className}`}>{RISK_TEXT[result.risk]?.label}</span>
                  {" "}新增 {result.impact.added_count} 人可访问，减少 {result.impact.removed_count} 人
                  {result.impact.added_users.length > 0 && `（新增：${result.impact.added_users.join("、")}）`}
                </div>
              )}

              <div style={{ marginTop: 12, textAlign: "right", display: "flex", gap: 8, justifyContent: "flex-end" }}>
                <button type="button" className="btn" onClick={() => setEditing(null)}>关闭</button>
                <button type="button" className="btn primary" disabled={!confirmOk} onClick={() => void save()}>
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
