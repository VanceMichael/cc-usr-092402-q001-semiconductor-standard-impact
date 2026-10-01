# 半导体标准变更影响闭环

工程用于保存标准条款与产品适用性判定的本地数据。数据文件采用 SQLite，迁移脚本可重复执行，接口进程默认监听 8080 端口。

服务在健康检查之上提供完整的标准索引：条款谱系按生效区间维护，义务映射到产品配置、供应商声明、试验结论与偏离批准，放行快照签署后不可改写，标准变化可一路追到全部待复核事项，并可按任意历史时点重现当时适用的条款、证据与未决异议。

启动：`docker build -t standards-index . && docker run --rm -p 8080:8080 standards-index`。本地测试：`pytest`。

## 时间语义（双时态）

- **生效时间**：`clause_versions.valid_from/valid_to` 为条款版本的生效区间（前闭后开）；继承关系（替代/拆分/合并/撤回）记录于 `clause_relations`，自 `effective_at` 起终止旧版本的适用性。草案（draft）只是提案，不进入适用集合。
- **获知时间**：所有记录带 `recorded_at`。查询用 `at`（生效时点）与 `knowledge_at`（获知时点，缺省等于 `at`）两条轴——迟到的更正（`recorded_at` 晚于 `valid_from`）只改变"现在回头看"的结论，不改写当时签署的快照与决定。

## 闭环规则

- **谱系**：`POST /clauses/{id}/versions` 登记版本时可内嵌替代/拆分/合并关系；撤回用 `POST /relations`（无目标版本）。原映射始终保留在原版本上，`POST /mappings/{id}/carry` 结转到新版本并记录来源链。
- **复核任务**：版本或关系登记时自动扇出——影响开放中的映射与已签署快照；生效点追溯到决定/签署之前的标记为 `late_correction`（快照内容保持不变）。任务随变化的 `recorded_at` 开立，同一来源不重复生成。
- **评议并发**：`POST /mappings` 时若依据版本不是当前最新非草案版本（或条款已撤回），返回 409 并携带最新版本；显式 `allow_stale=true` 可留痕放行并生成 `stale_decision` 任务。
- **快照**：`POST /snapshots` 冻结签署时点适用条款与该料号的开放映射，内容哈希存证（`/snapshots/{id}/verify`）；`/snapshots/{id}/diff` 对比出迟到更正与后续变化。快照不提供任何修改入口。
- **保密附件**：保密附件仅归属项目及被授权项目的成员可读（`X-User-Name` 请求头标识用户）；上传与授权限归属项目成员。
- **影响追踪**：`GET /changes/{version_id}/impact` 从一次标准变化追到受影响的料号、映射、快照、客户承诺与未决异议。
- **历史重现**：`GET /as-of?at=...&knowledge_at=...&standard_id=...` 重现当时适用的条款、证据、未决异议与待复核任务。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /health | 健康检查 |
| POST | /standards, /standards/{id}/clauses, /clauses/{id}/versions | 标准、条款、版本登记（版本可内嵌继承关系） |
| POST | /relations | 单独登记替代/拆分/合并/撤回 |
| GET | /clauses/{id}/lineage, /clauses/{id}/applicable | 谱系查询、时点适用版本 |
| POST | /products, /supplier-declarations, /test-reports, /deviation-approvals, /customer-commitments | 映射目标与客户承诺登记 |
| POST | /mappings, /mappings/{id}/carry, /mappings/{id}/close | 义务映射、结转、关闭 |
| GET | /mappings?stale=&part_number= | 映射查询（含旧版本标记） |
| POST | /snapshots | 签署放行快照 |
| GET | /snapshots/{id}, /snapshots/{id}/verify, /snapshots/{id}/diff | 快照详情、哈希校验、差异 |
| GET | /review-tasks?status=&kind=；POST /review-tasks/{id}/close | 复核任务 |
| POST | /objections, /objections/{id}/resolve；GET /objections | 异议 |
| POST | /attachments, /attachments/{id}/grants；GET /attachments/{id} | 保密附件与授权 |
| GET | /changes, /changes/{id}/impact | 变化 feed 与影响追踪 |
| GET | /as-of | 历史时点重现 |

时间字段接受 ISO 8601（`2026-03-01` 或 `2026-03-01T08:00:00Z`），缺省为服务器当前时间；导入历史数据时可显式指定 `recorded_at`/`decided_at`/`signed_at`。

## 开发检查

- 安装依赖：`python3 -m pip install -r requirements.txt`
- 运行测试：`python3 -m pytest`
- 编译检查：`python3 -m compileall -q app.py db.py domain.py`
