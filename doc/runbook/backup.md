# 备份与恢复

> 备份的价值只在**恢复成功**时才成立。本页给的是「照做就能恢复」的步骤。

## 备了什么

| 内容 | 路径 | 丢了会怎样 | 必需 |
|---|---|---|---|
| 文档元数据 | `data/metadata.json` | 文档清单丢失 → 知识库「看起来空了」 | ✅ |
| 图谱（SQLite） | `data/graph/graph.db` | 关系检索与社区摘要失效 | ❌ |
| 索引别名状态 | `data/graph/resolver.json` | 索引切换记录丢失 → 回滚路径断掉 | ❌ |
| 会话 / 检查点 | `data/sessions/`、`data/agent_checkpoints.db` | 历史会话与「续跑」失效（可接受丢） | ❌ |

## 怎么备

```bash
python scripts/backup.py                      # → data/backups/<时间戳>/
python scripts/backup.py --out /mnt/backup/kquery --keep 14
```

每次备份产出：

| 文件 | 作用 |
|---|---|
| `files/` | 数据快照 |
| `manifest.json` | 每个文件的 sha256 + 大小（**恢复前先校验**） |
| `RESTORE.md` | 恢复指引（随备份走，值班时不用回头翻文档） |

**保留策略**：默认保留最近 7 份，超出自动删旧（`--keep` 可调）。

## 怎么恢复

1. **停服务**（避免恢复过程中有写入）：`docker compose stop backend`
2. **校验备份**（务必先做）：`python scripts/backup.py --verify data/backups/<时间戳>`
   - 校验失败 → **换更早的备份**，不要硬恢复（损坏的备份比没备份更危险）
3. **恢复文件**：`cp -a data/backups/<时间戳>/files/metadata.json data/metadata.json`
4. **起服务并验证**：`curl -s localhost:8000/healthz | jq`
   - 若报 **5209（真相源损坏）** → 恢复的仍不完整 → 换更早的备份
5. **记时间线**（复盘要用）

## 演练

**没演练过的恢复流程等于没有恢复流程。** 演练脚本在临时目录里做完整闭环，
不碰生产数据：

```bash
python scripts/restore_drill.py     # 备份 → 清空 → 恢复 → 校验
```

建议纳入值班轮值：每周一次，或每次数据相关发布前一次。

## 已知边界

| 边界 | 说明 |
|---|---|
| **向量索引不在备份里** | 索引可从块重建（`POST /knowledge/rebuild/{doc_id}`）；备份索引会让备份体积暴涨 |
| **没有异地备份** | 备份目录在**同一台机器**上——机器级故障时备份一起没了（**这是当前最大的缺口**，见 `OP-01` 遗留项） |
| **备份不含 Redis 状态** | 共享状态（限流 / 临时态）可丢，重启后重建 |
