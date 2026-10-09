/** 角色 × 能力矩阵（stage-9 §2.3）。
 *
 * 此前矩阵只存在于文档里，代码里既没有后端端点也没有前端组件——管理员
 * 只能靠猜来授权。现在数据源是 `GET /admin/users/permission-matrix`，
 * 策略声明只有后端一份，改权限不用改两处。
 *
 * 可访问性硬约束（stage-9 §12）：
 * - 可访问性**不能只靠颜色表达**，每格同时给 ✓ / ✗ 文字标记；
 * - 用真 `<table>` + `<th scope>`，屏幕阅读器能读出表头与行列关系；
 * - `enforced` 如实标注：declared = 管理台约定，当前后端未做角色门禁。
 *   不标出来，管理员会把"纸面权限"当成"已强制"。
 */
import { useEffect, useState } from "react";

export interface PermissionCapability {
  key: string;
  label: string;
  enforced: "backend" | "acl" | "declared";
}

export interface PermissionRow {
  role: string;
  user_count: number;
  capabilities: Record<string, boolean>;
  granted: string[];
  denied: string[];
}

export interface PermissionMatrixData {
  capabilities: PermissionCapability[];
  roles: string[];
  matrix: PermissionRow[];
  default_role: string;
  unassigned_capabilities: string[];
}

const ENFORCED_LABEL: Record<PermissionCapability["enforced"], string> = {
  backend: "后端强制",
  acl: "ACL 表达式控制",
  declared: "仅管理台约定",
};

export function PermissionMatrix() {
  const [data, setData] = useState<PermissionMatrixData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const resp = await fetch("/admin/users/permission-matrix");
        const body = await resp.json();
        if (!alive) return;
        if (body?.status === "ok" && body.data) {
          setData(body.data);
          setError(null);
        } else {
          setError(body?.error?.message ?? "权限矩阵加载失败");
        }
      } catch {
        if (alive) setError("权限矩阵加载失败，请检查网络");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="panel" role="status" aria-live="polite">
        正在加载权限矩阵…
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="panel" role="alert">
        {error ?? "权限矩阵不可用"}
      </div>
    );
  }

  return (
    <section className="panel" aria-labelledby="perm-matrix-title">
      <h3 id="perm-matrix-title">角色能力矩阵</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        ✓ / ✗ 为能力开关；「{ENFORCED_LABEL.backend}」表示后端端点真正把守，
        「{ENFORCED_LABEL.declared}」表示当前只是管理台约定、后端未做角色门禁。
        「{data.default_role}」默认只有 {data.unassigned_capabilities.length} 项能力。
      </p>
      <div className="table-wrap">
        <table className="files-table">
          <caption className="sr-only">角色与能力对照表，共 {data.matrix.length} 个角色</caption>
          <thead>
            <tr>
              <th scope="col">角色</th>
              <th scope="col">成员数</th>
              {data.capabilities.map((cap) => (
                <th key={cap.key} scope="col" title={ENFORCED_LABEL[cap.enforced]}>
                  {cap.label}
                  <span className="mono" style={{ display: "block", fontSize: 10, opacity: 0.7 }}>
                    {ENFORCED_LABEL[cap.enforced]}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.matrix.map((row) => (
              <tr key={row.role}>
                <th scope="row">{row.role}</th>
                <td className="mono">{row.user_count}</td>
                {data.capabilities.map((cap) => {
                  const granted = !!row.capabilities[cap.key];
                  return (
                    <td
                      key={cap.key}
                      className={granted ? "perm-yes" : "perm-no"}
                      aria-label={`${cap.label}：${granted ? "允许" : "不允许"}`}
                    >
                      <span aria-hidden>{granted ? "✓" : "✗"}</span>
                      <span className="perm-word">{granted ? "允许" : "不允许"}</span>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}