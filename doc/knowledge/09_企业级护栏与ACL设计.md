# 企业级护栏与 ACL 设计 —— SaaS 多租户安全参考

> 本文档为原理性说明文档，回答三个通用问题：
> 1. SaaS 多租户如何做权限隔离？
> 2. 文档级 ACL 如何设计（默认拒绝 / deny 优先）？
> 3. 敏感词扫描与审计最小化如何平衡安全与合规？
>
> 文末以 `Kquery` 项目（阶段 9 企业级护栏）为完整案例。

**文档结构**：
- 第一章：SaaS 多租户的三大支柱（认证 / 配额 / ACL）；
- 第二章：文档级 ACL 设计；
- 第三章：敏感词扫描（长词优先）；
- 第四章：审计最小化（仅存哈希）；
- 第五章：Kquery 项目案例；
- 第六章：反模式清单与落地检查清单。

---

## 一、SaaS 多租户的三大支柱

### 1.1 多租户的核心挑战

| 挑战 | 表现 | 解法 |
|---|---|---|
| **身份认证** | 用户来自多个组织 | 4 要素 Principal（user_id / tenant_id / roles / groups） |
| **配额隔离** | 不同租户的 QPS / 配额 | 租户滑动窗口配额 |
| **数据隔离** | 文档级访问控制 | ACL（默认拒绝 / deny 优先） |

### 1.2 认证 Principal 四要素

```python
class Principal:
    user_id: str       # 用户唯一标识
    tenant_id: str     # 所属租户
    roles: list[str]   # 角色列表（如 ["hr", "finance"]）
    groups: list[str]  # 用户组（额外维度）
    enabled: bool      # 是否启用
```

**关键设计**：
- **user_id + tenant_id** 联合唯一
- **roles 决定 RBAC**（admin / hr / sales / finance）
- **groups 决定 ABAC**（如 "exec" 组可看所有文档）

### 1.3 租户配额 vs 平台配额

| 层 | 对象 | 工具 |
|---|---|---|
| **平台配额** | 千问账号级 | `PlatformQuotaMonitor` |
| **应用配额** | 租户级 | `SlidingWindowQuota` |
| **API 限流** | 用户 / IP 级 | `TokenBucketLimiter` |

**关键**：三层独立，任一超额即独立反馈。

---

## 二、文档级 ACL 设计

### 2.1 三大原则

```
1. 默认拒绝（Default Deny）
   用户不在 allowed → ❌ 拒绝
   
2. deny 优先（Deny Priority）
   用户同时在 allowed + denied → ❌ 拒绝
   
3. 显式允许（Explicit Allow）
   用户在 allowed（且不在 denied） → ✅ 允许
```

### 2.2 数据结构

```python
class ACLService:
    def __init__(self, acl_path, user_directory):
        # doc_id → allowed user_ids
        self._allowed: dict[str, list[str]] = {}
        # doc_id → denied user_ids
        self._denied: dict[str, list[str]] = {}
    
    def check(self, user_id, doc_id, principal) -> bool:
        """检查用户是否有权访问文档"""
        # 1. 默认拒绝
        if user_id not in self._allowed.get(doc_id, []):
            return False
        
        # 2. deny 优先
        if user_id in self._denied.get(doc_id, []):
            return False
        
        # 3. group 检查（ABAC）
        for group in principal.groups:
            if group in self._allowed.get(doc_id, []):
                if group not in self._denied.get(doc_id, []):
                    return True
        
        return True
```

### 2.3 检索结果过滤

```python
class RetrievalService:
    async def retrieve_with_acl(self, query, principal):
        # 1. 检索
        chunks = await hybrid_retriever.retrieve(query, top_k=50)
        
        # 2. ACL 过滤
        filtered = [c for c in chunks if acl_service.check(principal.user_id, c["doc_id"], principal)]
        
        # 3. 取 Top-K
        return filtered[:top_k]
```

### 2.4 Kquery 实现

`app/services/security/acl.py`：

```python
class ACLService:
    def __init__(self, acl_path, user_directory):
        self.acl_path = Path(acl_path)
        self.user_directory = {u["user_id"]: u for u in user_directory}
        self._load()
    
    def _load(self):
        if self.acl_path.exists():
            data = json.loads(self.acl_path.read_text())
            self._allowed = data.get("allowed", {})
            self._denied = data.get("denied", {})
    
    def check(self, user_id: str, doc_id: str) -> bool:
        # 默认拒绝
        if doc_id not in self._allowed:
            return False
        allowed_users = self._allowed[doc_id]
        if user_id not in allowed_users:
            return False
        
        # deny 优先
        if doc_id in self._denied and user_id in self._denied[doc_id]:
            return False
        
        return True
    
    def filter_docs(self, doc_ids: list[str], user_id: str) -> list[str]:
        """批量过滤"""
        return [d for d in doc_ids if self.check(user_id, d)]
```

---

## 三、敏感词扫描（长词优先）

### 3.1 为什么需要长词优先

```python
# 错误：短词优先扫描
rules = ["身份证", "中华人民共和国居民身份证"]
# "中华人民共和国居民身份证 1234567890..." → "身份证" 先匹配，误掩码
```

```python
# 正确：长词优先扫描
rules = sorted(["身份证", "中华人民共和国居民身份证"], key=len, reverse=True)
# 先匹配 "中华人民共和国居民身份证" → 完整掩码
```

### 3.2 Kquery 实现

`app/services/security/sensitive.py`：

```python
class SensitiveScanner:
    def __init__(self, words_path):
        self._rules = self._load_rules(words_path)
    
    def _load_rules(self, path):
        rules = json.loads(Path(path).read_text())
        # 长词优先排序
        return sorted(rules, key=len, reverse=True)
    
    def scan(self, text: str) -> dict:
        """扫描文本，返回 hits + redacted_text + blocked"""
        hits = []
        redacted = text
        
        for rule in self._rules:
            pattern = rule["pattern"]
            rule_type = rule["type"]
            action = rule.get("action", "redact")
            
            if pattern in redacted:
                hits.append({
                    "rule": rule_type,
                    "start": redacted.index(pattern),
                    "end": redacted.index(pattern) + len(pattern),
                    "type": rule_type,
                })
                
                if action == "block":
                    # 私钥等高危项 → 拒绝
                    return {
                        "hits": hits,
                        "blocked": True,
                        "reason": f"sensitive content blocked: {rule_type}",
                    }
                
                # redact
                redacted = redacted.replace(pattern, f"[REDACTED:{rule_type}]")
        
        return {
            "hits": hits,
            "redacted_text": redacted,
            "blocked": False,
        }
```

### 3.3 9 默认规则

```python
DEFAULT_RULES = [
    {"pattern": r"AKIA[0-9A-Z]{16}", "type": "aws_key", "action": "redact"},
    {"pattern": r"sk-[a-zA-Z0-9]{32,}", "type": "openai_key", "action": "redact"},
    {"pattern": r"ghp_[a-zA-Z0-9]{36}", "type": "github_key", "action": "redact"},
    {"pattern": r"xox[abp]-[0-9a-zA-Z-]{10,}", "type": "slack_key", "action": "redact"},
    {"pattern": r"-----BEGIN [A-Z ]+PRIVATE KEY-----", "type": "private_key", "action": "block"},
    {"pattern": r"password\s*[:=]\s*[\w!@#$%^&*]+", "type": "password", "action": "redact"},
    {"pattern": r"1[3-9]\d{9}", "type": "phone", "action": "redact"},
    {"pattern": r"\d{17}[\dXx]", "type": "id_card", "action": "redact"},
    {"pattern": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "type": "email", "action": "redact"},
]
```

### 3.4 集成到 LLM 调用

```python
class Pipeline:
    async def run(self, question, principal):
        # 1. 敏感词扫描
        scan_result = self.scanner.scan(question)
        if scan_result["blocked"]:
            raise ApiError("E_PRIVACY_BLOCKED", "检测到敏感内容", 403)
        question_safe = scan_result["redacted_text"]
        
        # 2. ACL 检查
        chunks = await self.retrieve(question_safe)
        chunks_filtered = [c for c in chunks if self.acl.check(principal.user_id, c["doc_id"])]
        
        # 3. 生成
        ...
```

---

## 四、审计最小化（仅存哈希）

### 4.1 GDPR / 等保 2.0 最小化原则

> **审计最小化**：仅记录**必要字段** + payload 哈希，原始 payload 不落盘。

### 4.2 三大审计事件

```python
class AuditStore:
    def audit(self, event: str, principal: Principal, payload: dict = None):
        """仅存哈希"""
        if event == "auth.login":
            self._append({
                "event": event,
                "user_id": principal.user_id,
                "tenant_id": principal.tenant_id,
                "timestamp": now(),
            })
        elif event == "acl.denied":
            payload_hash = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            self._append({
                "event": event,
                "user_id": principal.user_id,
                "doc_id": payload.get("doc_id"),  # 必要字段
                "reason": payload.get("reason"),
                "payload_hash": payload_hash,
                "timestamp": now(),
            })
        elif event == "quota.exceeded":
            payload_hash = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            self._append({
                "event": event,
                "tenant_id": principal.tenant_id,
                "user_id": principal.user_id,
                "payload_hash": payload_hash,
                "timestamp": now(),
            })
```

**关键设计**：
- 仅存必要字段（user_id / tenant_id / doc_id / timestamp）
- payload 用 SHA-256 哈希（可关联同事件的不同阶段）
- 原始 payload 不落盘（避免敏感信息泄露）

### 4.3 持久化

```python
# 审计日志 → JSONL 追加
def _append(self, record: dict):
    line = json.dumps(record, ensure_ascii=False)
    with open(self.audit_path, "a") as f:
        f.write(line + "\n")
```

---

## 五、Kquery 项目案例

### 5.1 认证流程

```http
POST /api/security/auth/login
{ "user_id": "zhangsan", "password": "..." }

// 响应
{
  "token": "...",
  "principal": {
    "user_id": "zhangsan",
    "tenant_id": "default",
    "roles": ["hr"],
    "groups": [],
    "enabled": true
  }
}
```

### 5.2 端点 Depends

```python
@router.post("/api/security/acl/check")
async def acl_check(
    req: AclCheckRequest,
    principal: Principal = Depends(get_current_principal)
):
    allowed = app.state.acl_service.check(principal.user_id, req.doc_id)
    return {"allowed": allowed, "reason": ...}
```

### 5.3 平台配额监控

```python
class PlatformQuotaMonitor:
    def __init__(self, per_day, warn_ratio=0.8):
        self._limit = per_day
        self._warn_ratio = warn_ratio
        self._usage = 0
    
    def report(self, tokens: int):
        self._usage += tokens
        ratio = self._usage / self._limit
        if ratio >= self._warn_ratio:
            logger.warning(f"平台配额接近上限: {ratio:.1%}")
    
    def get_usage_ratio(self) -> float:
        return self._usage / self._limit
```

### 5.4 评测门禁（阶段 9）

```python
# app/services/security/evalrun.py
class EvalRunner:
    async def compare(self, baseline_id, current_id) -> dict:
        base = self.store.get_eval_result(baseline_id)
        cur = self.store.get_eval_result(current_id)
        
        pass_rate_delta = cur.pass_rate - base.pass_rate
        cost_ratio = cur.cost_ratio
        latency_ratio = cur.latency_ratio
        
        verdict = "pass"
        if pass_rate_delta < -0.02: verdict = "fail"  # 回归 > 2%
        if cost_ratio > 1.2: verdict = "fail"
        if latency_ratio > 1.1: verdict = "fail"
        
        return {"verdict": verdict, "pass_rate_delta": ..., "cost_ratio": ..., "latency_ratio": ...}
```

### 5.5 评测必须绑定版本

```python
class EvalResult(BaseModel):
    eval_id: str
    eval_set_version: str  # 评测集版本
    model_version: str     # 模型档位版本
    pass_rate: float
    cost_ratio: float
    latency_ratio: float
    verdict: str
```

> **关键**：千问平台模型会升级（应用侧无法拒绝），评测必须绑定版本才能追溯。

---

## 六、反模式清单

### ❌ 反模式 1：默认允许

```python
# 错误：默认允许，仅 deny list
if user_id in self._denied.get(doc_id, []):
    return False
return True  # 默认通过
```

**正确**：默认拒绝 + 显式 allow。
```python
if user_id not in self._allowed.get(doc_id, []):
    return False
```

### ❌ 反模式 2：短词优先扫描

```python
# 错误
rules = ["身份证", "中华人民共和国居民身份证"]
# 长字符串误掩码
```

**正确**：长词优先。
```python
rules = sorted(rules, key=len, reverse=True)
```

### ❌ 反模式 3：审计存原始 payload

```python
# 错误：原始 payload 落盘
self._append({
    "event": "acl.denied",
    "user_id": user_id,
    "doc_content": doc.content,  # 泄露
})
```

**正确**：仅存哈希 + 必要字段。
```python
payload_hash = sha256(json.dumps(payload).encode()).hexdigest()
self._append({"event": ..., "user_id": ..., "payload_hash": payload_hash})
```

### ❌ 反模式 4：无版本绑定的评测

```python
# 错误：不绑定版本
class EvalResult:
    pass_rate: float
    verdict: str
```

**正确**：绑定评测集版本 + 模型版本。
```python
class EvalResult:
    eval_set_version: str
    model_version: str
    pass_rate: float
```

---

## 七、落地检查清单

- [ ] **认证 4 要素**：user_id / tenant_id / roles / groups
- [ ] **租户隔离**：每租户独立配额 / 数据
- [ ] **文档级 ACL**：默认拒绝 + deny 优先
- [ ] **敏感词长词优先**：按词长倒序扫描
- [ ] **9 默认规则**：AWS / OpenAI / GitHub / Slack key、私钥、密码、手机、身份证、邮箱
- [ ] **私钥 block**：检测到私钥 → `E_PRIVACY_BLOCKED`
- [ ] **审计最小化**：仅存必要字段 + payload 哈希
- [ ] **平台配额监控**：千问账号级，接近上限告警
- [ ] **评测门禁**：pass_rate / cost / latency 三指标
- [ ] **评测版本绑定**：`eval_set_version` + `model_version`

---

## 八、附录：核心代码位置速查（Kquery）

- `AuthProvider.build_auth_provider`（`app/services/security/auth.py`）：认证入口
- `ACLService.check`（`app/services/security/acl.py`）：文档级 ACL
- `SlidingWindowQuota.check`（`app/services/security/quota.py`）：租户配额
- `SensitiveScanner.scan`（`app/services/security/sensitive.py`）：敏感词扫描
- `AuditStore.audit`（`app/services/security/audit.py`）：审计
- `PlatformQuotaMonitor`（`app/services/security/platform_quota.py`）：平台配额
- `EvalRunner.compare`（`app/services/security/evalrun.py`）：评测门禁
- `Keychain`（`app/services/security/keychain.py`）：密钥管理
