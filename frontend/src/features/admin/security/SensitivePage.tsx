/** 敏感词管理：词表 + 新增 + 删除 + 逐行导入。
 *
 * 词表按租户隔离展示；动作三态（掩码/拒答/标记）。
 */
import { useCallback, useEffect, useState } from "react";
import { addSensitiveWord, deleteSensitiveWord, fetchSensitiveWords } from "../../../lib/api-client";
import type { SensitiveWord } from "../../../types";

const ACTION_LABEL = { mask: "掩码替换", refuse: "整体拒答", tag: "仅标记" } as const;
const CATEGORY_COLOR: Record<string, string> = {
  机密: "st-fail", 隐私: "st-warn", 禁语: "st-fail", 内部: "st-none",
};

export function SensitivePage() {
  const [words, setWords] = useState<SensitiveWord[]>([]);
  const [tenant, setTenant] = useState("default");
  const [word, setWord] = useState("");
  const [category, setCategory] = useState("机密");
  const [action, setAction] = useState<"mask" | "refuse" | "tag">("mask");
  const [importText, setImportText] = useState("");

  const load = useCallback(async () => {
    const envelope = await fetchSensitiveWords();
    if (envelope.data) setWords(envelope.data);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const add = async (w: string, cat: string, act: string, t: string) => {
    if (!w.trim()) return;
    await addSensitiveWord({ word: w.trim(), category: cat, action: act as SensitiveWord["action"], tenant_id: t, enabled: true });
    void load();
  };

  const importLines = async () => {
    const lines = importText.split("\n").map((l) => l.trim()).filter(Boolean);
    for (const line of lines) {
      await add(line, category, action, tenant);
    }
    setImportText("");
  };

  return (
    <div className="page">
      <div className="page-inner">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>敏感词表</h3>
          <table className="files-table">
            <thead>
              <tr><th>词条</th><th>类别</th><th>动作</th><th>租户</th><th>操作</th></tr>
            </thead>
            <tbody>
              {words.map((w) => (
                <tr key={`${w.tenant_id}-${w.word}`}>
                  <td>{w.word}</td>
                  <td><span className={`status-tag ${CATEGORY_COLOR[w.category] ?? "st-none"}`}>{w.category}</span></td>
                  <td>{ACTION_LABEL[w.action]}</td>
                  <td className="mono">{w.tenant_id}</td>
                  <td>
                    <button
                      type="button" className="btn-text danger"
                      onClick={() => {
                        if (window.confirm(`删除词条「${w.word}」？`)) {
                          void deleteSensitiveWord(w.word, w.tenant_id).then(() => load());
                        }
                      }}
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
              {words.length === 0 && (
                <tr><td colSpan={5} className="trace-id" style={{ textAlign: "center" }}>词表为空</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h3 style={{ marginTop: 0 }}>新增词条</h3>
          <div className="config-row" style={{ flexWrap: "wrap" }}>
            <input className="probe-input" style={{ width: 180 }} placeholder="词条（≤50 字）" value={word} onChange={(e) => setWord(e.target.value)} />
            <select className="probe-input" value={category} onChange={(e) => setCategory(e.target.value)} aria-label="类别">
              <option>机密</option><option>隐私</option><option>禁语</option><option>内部</option>
            </select>
            <select className="probe-input" value={action} onChange={(e) => setAction(e.target.value as typeof action)} aria-label="处置动作">
              <option value="mask">掩码替换</option>
              <option value="refuse">整体拒答</option>
              <option value="tag">仅标记</option>
            </select>
            <input className="probe-input" style={{ width: 110 }} value={tenant} onChange={(e) => setTenant(e.target.value)} placeholder="租户" />
            <button type="button" className="btn primary" onClick={() => { void add(word, category, action, tenant); setWord(""); }}>
              添加
            </button>
          </div>
          <div className="trace-id" style={{ marginTop: 6 }}>
            词表按租户严格隔离；改动即时生效（热更新）；扫描结果只回报类别与数量，不回传具体词。
          </div>
        </div>

        <div className="card">
          <h3 style={{ marginTop: 0 }}>批量导入（逐行）</h3>
          <textarea
            className="probe-input" style={{ width: "100%", minHeight: 80, fontFamily: "inherit" }}
            placeholder={"每行一个词条，使用上方选择的类别与动作\n例如：\n绝密项目\n内部薪酬"}
            value={importText}
            onChange={(e) => setImportText(e.target.value)}
          />
          <div style={{ marginTop: 8 }}>
            <button type="button" className="btn" disabled={!importText.trim()} onClick={() => void importLines()}>
              导入 {importText.split("\n").filter((l) => l.trim()).length} 条
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
