-- 条款谱系与影响闭环：生效区间、继承关系、义务映射、放行快照、复核任务、异议、保密附件。
-- 时间语义（双时态）：
--   valid_from/valid_to  条款版本在现实世界中的生效区间（前闭后开），valid_to 为计划终点；
--   recorded_at          系统获知该记录的时间。迟到的更正 = recorded_at 晚于 valid_from；
--   clause_relations.effective_at  继承关系（替代/拆分/合并/撤回）的生效日。
-- 所有会随认知变化的状态都用“关系 + 时间”表达，不回写历史行，
-- 因此任意历史时点都能重现当时所知的条款、证据与未决异议，已签署快照不被改写。
BEGIN;

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL DEFAULT 'engineer'
);

CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_members (
  project_id INTEGER NOT NULL REFERENCES projects(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  role TEXT NOT NULL DEFAULT 'member',
  PRIMARY KEY (project_id, user_id)
);

CREATE TABLE IF NOT EXISTS standards (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clauses (
  id INTEGER PRIMARY KEY,
  standard_id INTEGER NOT NULL REFERENCES standards(id),
  clause_no TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  UNIQUE (standard_id, clause_no)
);

CREATE TABLE IF NOT EXISTS clause_versions (
  id INTEGER PRIMARY KEY,
  clause_id INTEGER NOT NULL REFERENCES clauses(id),
  version_label TEXT NOT NULL,                 -- 如 2024-D1（草案）/ 2024-E1（勘误）/ 2025-F（正式）
  stage TEXT NOT NULL CHECK (stage IN ('draft', 'errata', 'official')),
  content TEXT NOT NULL,
  obligation TEXT NOT NULL DEFAULT '',         -- 条款义务摘要，供映射引用
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  recorded_at TEXT NOT NULL,
  recorded_by TEXT NOT NULL,
  UNIQUE (clause_id, version_label)
);

CREATE TABLE IF NOT EXISTS clause_relations (
  id INTEGER PRIMARY KEY,
  from_version_id INTEGER NOT NULL REFERENCES clause_versions(id),
  to_version_id INTEGER REFERENCES clause_versions(id),   -- 撤回时为空
  relation TEXT NOT NULL CHECK (relation IN ('replaces', 'splits_into', 'merges_into', 'withdraws')),
  effective_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  recorded_at TEXT NOT NULL,
  recorded_by TEXT NOT NULL,
  CHECK (relation = 'withdraws' OR to_version_id IS NOT NULL)
);

-- 义务映射的四类目标
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  part_number TEXT NOT NULL UNIQUE,            -- 料号
  config TEXT NOT NULL DEFAULT '{}'            -- 产品配置（JSON）
);

CREATE TABLE IF NOT EXISTS supplier_declarations (
  id INTEGER PRIMARY KEY,
  part_number TEXT NOT NULL,
  supplier TEXT NOT NULL,
  content TEXT NOT NULL,
  project_id INTEGER REFERENCES projects(id),
  recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_reports (
  id INTEGER PRIMARY KEY,
  report_no TEXT NOT NULL UNIQUE,
  part_number TEXT NOT NULL,
  conclusion TEXT NOT NULL CHECK (conclusion IN ('pass', 'fail', 'conditional')),
  project_id INTEGER REFERENCES projects(id),
  recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deviation_approvals (
  id INTEGER PRIMARY KEY,
  deviation_no TEXT NOT NULL UNIQUE,
  part_number TEXT NOT NULL,
  scope TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  expires_at TEXT,
  project_id INTEGER REFERENCES projects(id),
  recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_commitments (
  id INTEGER PRIMARY KEY,
  commitment_no TEXT NOT NULL UNIQUE,
  customer TEXT NOT NULL,
  part_number TEXT NOT NULL,
  content TEXT NOT NULL,
  project_id INTEGER REFERENCES projects(id),
  promised_at TEXT NOT NULL
);

-- 义务映射：条款版本 -> 目标。basis_version_id 记录决定所依据的版本，用于旧版本检测。
CREATE TABLE IF NOT EXISTS obligation_mappings (
  id INTEGER PRIMARY KEY,
  clause_version_id INTEGER NOT NULL REFERENCES clause_versions(id),
  target_type TEXT NOT NULL CHECK (target_type IN ('product_config', 'supplier_declaration', 'test_conclusion', 'deviation_approval')),
  target_id INTEGER NOT NULL,
  disposition TEXT NOT NULL CHECK (disposition IN ('compliant', 'deviation', 'not_applicable', 'pending')),
  basis_version_id INTEGER NOT NULL REFERENCES clause_versions(id),
  decided_by TEXT NOT NULL,
  decided_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  carried_from_id INTEGER REFERENCES obligation_mappings(id),  -- 结转来源，保留原关系链
  closed_at TEXT,
  closed_reason TEXT
);

-- 放行快照：签署后只增不改，digest 为内容哈希，迟到的更正只能生成复核任务。
CREATE TABLE IF NOT EXISTS release_snapshots (
  id INTEGER PRIMARY KEY,
  part_number TEXT NOT NULL,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  standard_id INTEGER NOT NULL REFERENCES standards(id),
  signed_by TEXT NOT NULL,
  signed_at TEXT NOT NULL,
  digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS release_snapshot_items (
  id INTEGER PRIMARY KEY,
  snapshot_id INTEGER NOT NULL REFERENCES release_snapshots(id),
  clause_id INTEGER NOT NULL REFERENCES clauses(id),
  clause_version_id INTEGER NOT NULL REFERENCES clause_versions(id),
  mapping_id INTEGER REFERENCES obligation_mappings(id),
  disposition TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS review_tasks (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('clause_change', 'late_correction', 'stale_decision', 'withdrawal')),
  clause_version_id INTEGER REFERENCES clause_versions(id),  -- 触发复核的变化主体
  relation_id INTEGER REFERENCES clause_relations(id),
  mapping_id INTEGER REFERENCES obligation_mappings(id),
  snapshot_id INTEGER REFERENCES release_snapshots(id),
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  closed_by TEXT,
  resolution TEXT
);

CREATE TABLE IF NOT EXISTS objections (
  id INTEGER PRIMARY KEY,
  mapping_id INTEGER REFERENCES obligation_mappings(id),
  snapshot_id INTEGER REFERENCES release_snapshots(id),
  raised_by TEXT NOT NULL,
  raised_at TEXT NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
  resolved_at TEXT,
  resolved_by TEXT,
  CHECK (mapping_id IS NOT NULL OR snapshot_id IS NOT NULL)
);

-- 保密附件：归属项目，可通过授权表向其他项目开放。
CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL,
  content TEXT NOT NULL,
  confidential INTEGER NOT NULL DEFAULT 1,
  uploaded_by TEXT NOT NULL,
  uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachment_grants (
  attachment_id INTEGER NOT NULL REFERENCES attachments(id),
  project_id INTEGER NOT NULL REFERENCES projects(id),
  granted_by TEXT NOT NULL,
  granted_at TEXT NOT NULL,
  PRIMARY KEY (attachment_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_cv_clause ON clause_versions(clause_id);
CREATE INDEX IF NOT EXISTS idx_rel_from ON clause_relations(from_version_id);
CREATE INDEX IF NOT EXISTS idx_rel_to ON clause_relations(to_version_id);
CREATE INDEX IF NOT EXISTS idx_map_cv ON obligation_mappings(clause_version_id);
CREATE INDEX IF NOT EXISTS idx_map_target ON obligation_mappings(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_snap_items ON release_snapshot_items(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_snap_items_cv ON release_snapshot_items(clause_version_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON review_tasks(status);
CREATE INDEX IF NOT EXISTS idx_obj_status ON objections(status);

INSERT OR IGNORE INTO schema_version(version) VALUES (2);
COMMIT;
