# 半导体条款生效索引

面向功率器件车规标准导入的本地服务：维护标准条款在草案、勘误、正式版本之间的谱系与生效区间，把条款义务映射到料号配置、供应商声明、试验结论与偏离批准，并在标准变化时追踪受影响的放行快照与全部待复核事项。数据文件采用 SQLite，迁移脚本可重复执行，接口进程默认监听 8080 端口。

## 数据模型要点

- **追加式事实记录**：所有事实表携带 `recorded_at` / `retracted_at`，不物理删除；`GET /asof` 据此按任意历史时点重现当时适用的条款、证据与未决异议。
- **条款谱系**：`clause_versions` 维护生效区间 `[valid_from, valid_to)`；`clause_edges` 记录替代、勘误、拆分、合并、撤回关系，边永久保留，原关系不因后续改写而丢失。
- **放行快照**：`release_snapshots` 签署后由数据库触发器冻结（条目、证据、摘要均不可改）；迟到的更正登记为 `snapshot_divergences` 并生成复核任务，而不是悄悄改写快照。
- **评议并发**：`review_decisions` 记录所基于的条款版本；提交时检测是否已有更新的版本或谱系边，旧版本上的未决决定在新版本登记时被主动标记 `stale`。
- **保密附件**：`attachments.confidential=1` 的内容仅向 `attachment_grants` 授权的项目开放（请求头 `X-Project-Id`），授权可撤销。

## 接口概览

| 能力 | 接口 |
| --- | --- |
| 条款谱系 | `POST /clauses/{id}/versions`、`POST /clause-versions/{id}/withdraw`、`GET /clauses/{id}/lineage`、`GET /clauses/{id}/applicable?at=` |
| 义务映射 | `POST /obligations`、`POST /obligations/{id}/links`、`POST /obligation-links/{id}/retract` |
| 映射对象 | `POST /product-configs`、`/supplier-declarations`、`/test-conclusions`、`/deviation-approvals` |
| 放行快照 | `POST /snapshots`、`POST /snapshots/{id}/sign`、`GET /snapshots/{id}` |
| 评议与异议 | `POST /decisions`、`GET /decisions?status=`、`POST /objections`、`POST /objections/{id}/resolve` |
| 保密附件 | `POST /attachments`、`POST /attachments/{id}/grants`、`POST /attachments/{id}/grants/{project}/revoke`、`GET /attachments/{id}/content` |
| 影响追踪 | `GET /clause-versions/{id}/impact`、`GET /review-tasks?status=`、`GET /standards/{id}/changes` |
| 历史重现 | `GET /asof?standard_id=&at=` |

## 启动与测试

启动：`docker build -t standards-index . && docker run --rm -p 8080:8080 standards-index`。本地测试：`pytest`。

## 开发检查

- 安装依赖：`python3 -m pip install -r requirements.txt`
- 运行测试：`python3 -m pytest`
- 编译检查：`python3 -m compileall -q app.py db.py domain.py`
