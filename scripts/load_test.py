"""Kquery 基线压测 v2（异步版；对应设计项 C-02 / C-03，基线留档见 doc/analysis/并发容量基线.md）。

与 v1 的区别：httpx.AsyncClient 真实持有连接（urllib 线程模型测不了"500 条 SSE 长连接"）。

场景：
1. healthz 探针阶梯（1→10→30 并发，测框架上限）
2. /knowledge/files 列表阶梯（鉴权读路径）
3. /ask 定速（限流以内，测真实问答延迟与缓存行为）
4. /ask 超流（验证 60/min 闸门与 429 快速失败，以及背压语义）
5. /ask/stream 连接保持（N 条 SSE 同时打开，验证异步桥接后连接不占线程）

运行：
    cd backend && uv run python ../scripts/load_test.py \
        --api http://localhost:8100 --password Admin12345 --connections 100
"""
import argparse
import asyncio
import subprocess
import sys
import time

import httpx

QUESTION = "Kquery 支持哪些文档格式？"


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * p / 100))
    return sorted_values[idx]


class Stat:
    def __init__(self) -> None:
        self.lat: list[float] = []
        self.status: dict[int, int] = {}
        self.errors = 0
        self.elapsed_s: float = 0.0

    def add(self, status: int, latency_ms: float) -> None:
        self.status[status] = self.status.get(status, 0) + 1
        self.lat.append(latency_ms)


async def bench(name: str, concurrency: int, duration_s: float, fn) -> Stat:
    stat = Stat()
    stop_at = time.perf_counter() + duration_s

    async def worker() -> None:
        while time.perf_counter() < stop_at:
            status, ms = await fn()
            if status < 0:
                stat.errors += 1
            else:
                stat.add(status, ms)

    started = time.perf_counter()
    await asyncio.gather(*[worker() for _ in range(concurrency)])
    elapsed = time.perf_counter() - started
    stat.elapsed_s = elapsed
    total = sum(stat.status.values()) + stat.errors
    lat = sorted(stat.lat)
    status_str = ", ".join(f"{s}:{n}" for s, n in sorted(stat.status.items()))
    print(
        f"  {name:<30} 并发={concurrency:<3} 总量={total:<5} QPS={total / elapsed:6.1f}"
        f" P50={percentile(lat, 50):8.1f}ms P95={percentile(lat, 95):8.1f}ms"
        f" P99={percentile(lat, 99):8.1f}ms | {status_str or '无成功'} | 异常={stat.errors}"
    )
    return stat


def make_get(client: httpx.AsyncClient, path: str):
    async def call() -> tuple[int, float]:
        t0 = time.perf_counter()
        try:
            resp = await client.get(path, timeout=30.0)
            await resp.aread()
            return resp.status_code, (time.perf_counter() - t0) * 1000
        except Exception:
            return -1, (time.perf_counter() - t0) * 1000
    return call


def make_ask(client: httpx.AsyncClient):
    async def call() -> tuple[int, float]:
        t0 = time.perf_counter()
        try:
            resp = await client.post("/ask", json={"question": QUESTION}, timeout=180.0)
            await resp.aread()
            return resp.status_code, (time.perf_counter() - t0) * 1000
        except Exception:
            return -1, (time.perf_counter() - t0) * 1000
    return call


async def paced_ask(client: httpx.AsyncClient, rps: float, duration_s: float) -> Stat:
    stat = Stat()
    interval = 1.0 / rps
    started = time.perf_counter()
    next_at = started
    call = make_ask(client)
    while time.perf_counter() < started + duration_s:
        next_at += interval
        status, ms = await call()
        if status < 0:
            stat.errors += 1
        else:
            stat.add(status, ms)
        delay = next_at - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
    elapsed = time.perf_counter() - started
    total = sum(stat.status.values()) + stat.errors
    lat = sorted(stat.lat)
    status_str = ", ".join(f"{s}:{n}" for s, n in sorted(stat.status.items()))
    print(
        f"  ask 定速 {rps} QPS ×{duration_s:.0f}s        总量={total:<5} QPS={total / elapsed:6.1f}"
        f" P50={percentile(lat, 50):8.1f}ms P95={percentile(lat, 95):8.1f}ms | {status_str} | 异常={stat.errors}"
    )
    return stat


async def stream_hold(client: httpx.AsyncClient, connections: int, hold_s: float) -> dict:
    """同时打开 N 条 SSE 并保持 hold_s 秒，统计接纳/拒绝/完成。"""
    results = {"opened_200": 0, "rejected_429": 0, "rejected_503": 0, "rejected_other": 0, "errors": 0}
    first_token_ms: list[float] = []

    async def one() -> None:
        t0 = time.perf_counter()
        try:
            async with client.stream(
                "POST", "/ask/stream", json={"question": QUESTION}, timeout=hold_s + 60
            ) as resp:
                if resp.status_code == 200:
                    results["opened_200"] += 1
                    async for line in resp.aiter_lines():
                        if line.startswith("event: token") and not first_token_ms:
                            first_token_ms.append((time.perf_counter() - t0) * 1000)
                        if line.startswith("event: done"):
                            break
                        if time.perf_counter() - t0 > hold_s:
                            break
                elif resp.status_code == 429:
                    results["rejected_429"] += 1
                    await resp.aread()
                elif resp.status_code == 503:
                    results["rejected_503"] += 1
                    await resp.aread()
                else:
                    results["rejected_other"] += 1
                    await resp.aread()
        except Exception:
            results["errors"] += 1

    started = time.perf_counter()
    await asyncio.gather(*[one() for _ in range(connections)])
    elapsed = time.perf_counter() - started
    ttft = sorted(first_token_ms)
    print(
        f"  SSE 连接保持 ×{connections}（{hold_s:.0f}s）  "
        f"200:{results['opened_200']} 429:{results['rejected_429']} 503:{results['rejected_503']}"
        f" 其它:{results['rejected_other']} 异常:{results['errors']} 用时={elapsed:.1f}s"
    )
    if ttft:
        print(f"  TTFT: P50={percentile(ttft, 50):.0f}ms P95={percentile(ttft, 95):.0f}ms（n={len(ttft)}）")
    return results


def docker_stats() -> None:
    try:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}} CPU={{.CPUPerc}} MEM={{.MemUsage}}",
             "kquery-backend"],
            capture_output=True, text=True, timeout=15,
        )
        if out.stdout.strip():
            print(f"  容器资源: {out.stdout.strip()}")
    except Exception:  # noqa: BLE001
        pass


async def amain(args) -> int:
    # 认证已下线：不再登录，直接压。
    headers: dict[str, str] = {}
    limits = httpx.Limits(max_connections=1000, max_keepalive_connections=1000)
    failed = 0

    async with httpx.AsyncClient(base_url=args.api, headers=headers, limits=limits) as client:
        # `C-02`：单场景模式——只跑一次并输出**基线表的一行**。
        if args.markdown_row:
            stat = await bench(
                f"ask ×{args.concurrency}", args.concurrency, args.duration, make_ask(client)
            )
            date = args.date or time.strftime("%Y-%m-%d")
            print(
                markdown_row(
                    date, args.tag, args.scenario, args.concurrency, stat, args.note
                )
            )
            return 0
        print(f"\n压测目标: {args.api}（httpx 异步，阶梯/保持场景）\n")
        print("[1] healthz 阶梯")
        for c in (1, 10, 30):
            await bench(f"healthz ×{c}", c, args.duration, make_get(client, "/healthz"))

        print("[2] /knowledge/files 阶梯")
        for c in (5, 20):
            await bench(f"knowledge/files ×{c}", c, args.duration, make_get(client, "/knowledge/files"))

        print("[3] /ask 定速（限流以内）")
        stat = await paced_ask(client, 0.5, max(20.0, args.duration * 2))
        if stat.status.get(200, 0) == 0:
            print("  ⚠️ 限流以内应全部成功")
            failed += 1

        print("[4] /ask 超流（验证闸门）")
        stat = await bench("ask ×8", 8, args.duration, make_ask(client))
        if any(s >= 500 and s != 503 for s in stat.status):
            print("  ⚠️ 出现非背压 5xx")
            failed += 1

        print("[5] /ask/stream 连接保持")
        results = await stream_hold(client, args.connections, 10.0)
        if results["errors"] > args.connections * 0.05:
            print("  ⚠️ 连接异常率 >5%")
            failed += 1

    docker_stats()
    print("\n" + "=" * 60)
    if failed:
        print(f"压测发现 {failed} 项异常")
        return 1
    print("压测基线无异常（429/503 为限流与背压设计的预期行为）")
    return 0


def markdown_row(
    date: str,
    tag: str,
    scenario: str,
    concurrency: int,
    stat: Stat,
    note: str = "",
) -> str:
    """把一次压测产出成**基线表的一行**（`C-02`）。

    ## 为什么要脚本产出

    手抄数字会抄错，而基线表是用来**比趋势**的——抄错一次就毁掉一整段趋势。
    脚本产出 → 数字与压测结果同源。

    ## 纪律：追加不覆盖

    每次压测**追加一行**，旧的行不删：单点会被环境噪声支配，趋势才是结论。
    """
    lat = sorted(stat.lat)
    total = sum(stat.status.values()) + stat.errors
    qps = (total / stat.elapsed_s) if stat.elapsed_s > 0 else 0.0
    n_429 = stat.status.get(429, 0)
    # 503 是**背压**（并发闸门满），是设计内行为，不算 5xx 故障——
    # 与压测自身的判定口径一致（`any(s >= 500 and s != 503)`）。
    n_5xx = sum(n for s, n in stat.status.items() if s >= 500 and s != 503)
    return (
        f"| {date} | {tag} | {scenario} | {concurrency} | {qps:.1f} | "
        f"{percentile(lat, 50):.0f} | {percentile(lat, 95):.0f} | {n_429} | {n_5xx} | {note} |"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8100")
    parser.add_argument("--password", default="Admin12345")
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--connections", type=int, default=100)
    # `C-02`：基线留档——把结果产出成基线表的一行（追加，不覆盖）
    parser.add_argument(
        "--markdown-row", action="store_true",
        help="只输出 Markdown 行（供追加到 doc/analysis/并发容量基线.md）",
    )
    parser.add_argument("--date", default="", help="基线行的日期（默认今天）")
    parser.add_argument("--tag", default="local", help="镜像 tag / 环境标识")
    parser.add_argument("--note", default="", help="备注")
    parser.add_argument(
        "--scenario", default="ask",
        help="场景名（基线行的「场景」列）",
    )
    parser.add_argument(
        "--concurrency", type=int, default=8,
        help="单场景模式的并发数（基线行的「并发」列）",
    )
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
