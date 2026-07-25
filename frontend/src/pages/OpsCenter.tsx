import { useEffect, useState } from 'react'

/**
 * 运维可观测性中心
 *
 * 通过 iframe 嵌入独立部署的运维控制台（ops-ui，默认 http://localhost:8081），
 * 该控制台调用后端 /api/v1/ops 提供的 7 大运维能力：
 *   健康巡检 / 指标采集 / 告警管理 / 日志聚合 / 故障诊断 / SLO 追踪 / 运维智能体
 *
 * 后端同时承载原项目的知识库、知识图谱、智能问答等能力，
 * 因此本页面与原有功能统一在同一个应用中，互不冲突。
 */
export default function OpsCenter() {
  // 运维控制台地址：与 vite 代理同源时优先使用相对路径，否则回退到独立部署地址
  const [src] = useState(() => {
    const direct = `${window.location.protocol}//${window.location.hostname}:8081/`
    return direct
  })
  const [errored, setErrored] = useState(false)

  useEffect(() => {
    setErrored(false)
  }, [src])

  return (
    <div className="h-[calc(100vh-4rem)] w-full flex flex-col">
      <div className="flex items-center justify-between px-4 py-2 border-b bg-card/50">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">运维可观测性中心</span>
          <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-primary/20 text-primary">
            NEW
          </span>
        </div>
        <a
          href={src}
          target="_blank"
          rel="noreferrer"
          className="text-xs text-muted-foreground hover:text-primary underline-offset-2 hover:underline"
        >
          在新标签页打开 ↗
        </a>
      </div>

      <div className="flex-1 relative">
        {errored ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 text-center px-6">
            <p className="text-sm text-muted-foreground">
              无法连接到运维控制台（{src}）。
            </p>
            <p className="text-xs text-muted-foreground/70">
              请确认运维控制台已启动：<br />
              <code className="px-1.5 py-0.5 rounded bg-muted">
                cd Kquery/ops-ui &amp;&amp; python3 -m http.server 8081
              </code>
            </p>
          </div>
        ) : (
          <iframe
            src={src}
            title="运维可观测性中心"
            className="absolute inset-0 w-full h-full border-0"
            onError={() => setErrored(true)}
          />
        )}
      </div>
    </div>
  )
}
