# API 接口设计规范

## RESTful 原则

- **资源导向**：URL 表示资源而非操作
- **无状态**：每个请求包含所有必要信息
- **统一接口**：使用标准 HTTP 方法和状态码
- **分层系统**：客户端无需关心中间件细节

**URL 格式**：`/api/v1/{资源名}[/{资源ID}][/{子资源}]`

**命名规范**：URL 小写+连字符，JSON 字段 camelCase，枚举 UPPER_SNAKE_CASE。

## HTTP 方法与状态码

| 方法 | 操作 | 幂等 | 安全 |
|------|------|------|------|
| GET | 查询资源 | 是 | 是 |
| POST | 创建资源 | 否 | 否 |
| PUT | 全量更新 | 是 | 否 |
| PATCH | 部分更新 | 是 | 否 |
| DELETE | 删除资源 | 是 | 否 |

**常用状态码**：
- 200 OK / 201 Created / 204 No Content
- 400 Bad Request / 401 Unauthorized / 403 Forbidden / 404 Not Found / 429 Too Many
- 500 Internal Server Error / 503 Service Unavailable

## 标准响应格式

```json
{
  "success": true,
  "data": {},
  "message": "操作成功",
  "timestamp": "2026-07-04T12:00:00Z",
  "requestId": "req_abc123"
}

// 错误响应
{
  "success": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "参数校验失败",
    "details": { "name": "名称不能为空" }
  },
  "requestId": "req_def456"
}
```

## 分页规范

```
请求：GET /api/v1/documents?page=1&pageSize=20&sortBy=uploadedAt&sortOrder=desc

响应：
{
  "data": [...],
  "pagination": {
    "page": 1,
    "pageSize": 20,
    "total": 156,
    "totalPages": 8
  }
}
```

## 安全规范

- **认证**：使用 JWT Token 进行身份认证
- **授权**：基于 RBAC 的细粒度权限控制
- **限流**：API 级别限流，防止滥用
- **输入验证**：所有输入必须进行严格校验
- **敏感数据**：敏感字段必须加密传输和存储
- **CORS**：配置严谨的跨域访问策略
