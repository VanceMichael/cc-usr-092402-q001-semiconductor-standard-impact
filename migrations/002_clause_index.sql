PRAGMA foreign_keys = ON;

-- 条款谱系与影响闭环。
-- 所有事实表携带 recorded_at（系统得知时刻）与 retracted_at（撤回时刻），
-- 采用追加式记录：除 retracted_at 与状态列外不做 UPDATE，不物理删除，
-- 以支持按任意历史时点重现当时适用的条款、证据与未决异议。

CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    retracted_at TEXT
);

CREATE TABLE IF NOT EXISTS standards (
    standard_id  TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    retracted_at TEXT
);

CREATE TABLE IF NOT EXISTS clauses (
    clause_id    TEXT PRIMARY KEY,
    standard_id  TEXT NOT NULL REFERENCES standards(standard_id),
    clause_no    TEXT NOT NULL,
    title        TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    retracted_at TEXT
);

-- 条款版本：生效区间 [valid_from, valid_to) 描述有效时间；
-- 版本内容一旦登记不可更改，错误的登记只能 retract 后重新登记。
CREATE TABLE IF NOT EXISTS clause_versions (
    clause_version_id TEXT PRIMARY KEY,
    clause_id    TEXT NOT NULL REFERENCES clauses(clause_id),
    stage        TEXT NOT NULL CHECK (stage IN ('draft', 'errata', 'corrigendum', 'official')),
    content      TEXT NOT NULL,
    valid_from   TEXT NOT NULL,
    valid_to     TEXT,
    recorded_at  TEXT NOT NULL,
    retracted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_clause_versions_clause ON clause_versions(clause_id);

-- 谱系边：替代/勘误/拆分/合并/撤回关系永久保留。
-- 撤回没有后继版本，to_version_id 为 NULL 且必须给出 effective_at；
-- 其余关系的生效时刻缺省取后继版本的 valid_from。
CREATE TABLE IF NOT EXISTS clause_edges (
    edge_id         TEXT PRIMARY KEY,
    from_version_id TEXT NOT NULL REFERENCES clause_versions(clause_version_id),
    to_version_id   TEXT REFERENCES clause_versions(clause_version_id),
    relation        TEXT NOT NULL CHECK (relation IN ('replaces', 'corrects', 'splits_into', 'merges_into', 'withdraws')),
    effective_at    TEXT,
    recorded_at     TEXT NOT NULL,
    retracted_at    TEXT,
    CHECK (
        (relation = 'withdraws' AND to_version_id IS NULL AND effective_at IS NOT NULL)
        OR (relation <> 'withdraws' AND to_version_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_clause_edges_from ON clause_edges(from_version_id);
CREATE INDEX IF NOT EXISTS idx_clause_edges_to ON clause_edges(to_version_id);

CREATE TABLE IF NOT EXISTS obligations (
    obligation_id     TEXT PRIMARY KEY,
    clause_version_id TEXT NOT NULL REFERENCES clause_versions(clause_version_id),
    statement         TEXT NOT NULL,
    recorded_at       TEXT NOT NULL,
    retracted_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_obligations_version ON obligations(clause_version_id);

CREATE TABLE IF NOT EXISTS product_configs (
    config_id    TEXT PRIMARY KEY,
    part_number  TEXT NOT NULL,
    product_line TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    recorded_at  TEXT NOT NULL,
    retracted_at TEXT
);

CREATE TABLE IF NOT EXISTS supplier_declarations (
    declaration_id TEXT PRIMARY KEY,
    supplier       TEXT NOT NULL,
    part_number    TEXT NOT NULL,
    statement      TEXT NOT NULL,
    recorded_at    TEXT NOT NULL,
    retracted_at   TEXT
);

CREATE TABLE IF NOT EXISTS attachments (
    attachment_id TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    media_type    TEXT NOT NULL DEFAULT 'application/octet-stream',
    content       BLOB NOT NULL,
    confidential  INTEGER NOT NULL DEFAULT 0,
    recorded_at   TEXT NOT NULL,
    retracted_at  TEXT
);

-- 保密附件按项目授权；授权可撤销（retracted_at），撤销后可重新授权。
CREATE TABLE IF NOT EXISTS attachment_grants (
    grant_id      TEXT PRIMARY KEY,
    attachment_id TEXT NOT NULL REFERENCES attachments(attachment_id),
    project_id    TEXT NOT NULL REFERENCES projects(project_id),
    recorded_at   TEXT NOT NULL,
    retracted_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_grants_attachment ON attachment_grants(attachment_id, project_id);

CREATE TABLE IF NOT EXISTS test_conclusions (
    conclusion_id TEXT PRIMARY KEY,
    report_no     TEXT NOT NULL,
    config_id     TEXT REFERENCES product_configs(config_id),
    result        TEXT NOT NULL CHECK (result IN ('pass', 'fail', 'conditional')),
    summary       TEXT NOT NULL DEFAULT '',
    attachment_id TEXT REFERENCES attachments(attachment_id),
    recorded_at   TEXT NOT NULL,
    retracted_at  TEXT
);

CREATE TABLE IF NOT EXISTS deviation_approvals (
    approval_id   TEXT PRIMARY KEY,
    config_id     TEXT REFERENCES product_configs(config_id),
    approver      TEXT NOT NULL,
    rationale     TEXT NOT NULL,
    expires_at    TEXT,
    attachment_id TEXT REFERENCES attachments(attachment_id),
    recorded_at   TEXT NOT NULL,
    retracted_at  TEXT
);

-- 义务到四类对象的映射：恰好一个目标列非空，且与 target_kind 一致。
CREATE TABLE IF NOT EXISTS obligation_links (
    link_id        TEXT PRIMARY KEY,
    obligation_id  TEXT NOT NULL REFERENCES obligations(obligation_id),
    target_kind    TEXT NOT NULL CHECK (target_kind IN ('product_config', 'supplier_declaration', 'test_conclusion', 'deviation_approval')),
    config_id      TEXT REFERENCES product_configs(config_id),
    declaration_id TEXT REFERENCES supplier_declarations(declaration_id),
    conclusion_id  TEXT REFERENCES test_conclusions(conclusion_id),
    approval_id    TEXT REFERENCES deviation_approvals(approval_id),
    recorded_at    TEXT NOT NULL,
    retracted_at   TEXT,
    CHECK (
        (CASE WHEN config_id IS NOT NULL THEN 1 ELSE 0 END
       + CASE WHEN declaration_id IS NOT NULL THEN 1 ELSE 0 END
       + CASE WHEN conclusion_id IS NOT NULL THEN 1 ELSE 0 END
       + CASE WHEN approval_id IS NOT NULL THEN 1 ELSE 0 END) = 1
    ),
    CHECK (target_kind <> 'product_config' OR config_id IS NOT NULL),
    CHECK (target_kind <> 'supplier_declaration' OR declaration_id IS NOT NULL),
    CHECK (target_kind <> 'test_conclusion' OR conclusion_id IS NOT NULL),
    CHECK (target_kind <> 'deviation_approval' OR approval_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_obligation_links_obligation ON obligation_links(obligation_id);

CREATE TABLE IF NOT EXISTS release_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    config_id   TEXT NOT NULL REFERENCES product_configs(config_id),
    standard_id TEXT NOT NULL REFERENCES standards(standard_id),
    as_of       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    signed_at   TEXT,
    signer      TEXT,
    digest      TEXT
);

CREATE TABLE IF NOT EXISTS snapshot_items (
    snapshot_id       TEXT NOT NULL REFERENCES release_snapshots(snapshot_id),
    clause_version_id TEXT NOT NULL REFERENCES clause_versions(clause_version_id),
    PRIMARY KEY (snapshot_id, clause_version_id)
);

CREATE TABLE IF NOT EXISTS snapshot_evidence (
    snapshot_id TEXT NOT NULL REFERENCES release_snapshots(snapshot_id),
    link_id     TEXT NOT NULL REFERENCES obligation_links(link_id),
    PRIMARY KEY (snapshot_id, link_id)
);

-- 已签署的放行快照及其条目、证据不可更改或删除；
-- 迟到的更正只能以 snapshot_divergences 的形式另行登记。
CREATE TRIGGER trg_release_snapshots_lock_update
BEFORE UPDATE ON release_snapshots
WHEN OLD.signed_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'signed release snapshot is immutable');
END;

CREATE TRIGGER trg_release_snapshots_lock_delete
BEFORE DELETE ON release_snapshots
WHEN OLD.signed_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'signed release snapshot is immutable');
END;

CREATE TRIGGER trg_snapshot_items_lock_insert
BEFORE INSERT ON snapshot_items
BEGIN
    SELECT RAISE(ABORT, 'signed release snapshot is immutable')
    WHERE (SELECT signed_at FROM release_snapshots WHERE snapshot_id = NEW.snapshot_id) IS NOT NULL;
END;

CREATE TRIGGER trg_snapshot_items_lock_update
BEFORE UPDATE ON snapshot_items
BEGIN
    SELECT RAISE(ABORT, 'signed release snapshot is immutable')
    WHERE (SELECT signed_at FROM release_snapshots WHERE snapshot_id = OLD.snapshot_id) IS NOT NULL;
END;

CREATE TRIGGER trg_snapshot_items_lock_delete
BEFORE DELETE ON snapshot_items
BEGIN
    SELECT RAISE(ABORT, 'signed release snapshot is immutable')
    WHERE (SELECT signed_at FROM release_snapshots WHERE snapshot_id = OLD.snapshot_id) IS NOT NULL;
END;

CREATE TRIGGER trg_snapshot_evidence_lock_insert
BEFORE INSERT ON snapshot_evidence
BEGIN
    SELECT RAISE(ABORT, 'signed release snapshot is immutable')
    WHERE (SELECT signed_at FROM release_snapshots WHERE snapshot_id = NEW.snapshot_id) IS NOT NULL;
END;

CREATE TRIGGER trg_snapshot_evidence_lock_update
BEFORE UPDATE ON snapshot_evidence
BEGIN
    SELECT RAISE(ABORT, 'signed release snapshot is immutable')
    WHERE (SELECT signed_at FROM release_snapshots WHERE snapshot_id = OLD.snapshot_id) IS NOT NULL;
END;

CREATE TRIGGER trg_snapshot_evidence_lock_delete
BEFORE DELETE ON snapshot_evidence
BEGIN
    SELECT RAISE(ABORT, 'signed release snapshot is immutable')
    WHERE (SELECT signed_at FROM release_snapshots WHERE snapshot_id = OLD.snapshot_id) IS NOT NULL;
END;

-- 已签署快照与迟到条款变更之间的偏差登记。
CREATE TABLE IF NOT EXISTS snapshot_divergences (
    divergence_id     TEXT PRIMARY KEY,
    snapshot_id       TEXT NOT NULL REFERENCES release_snapshots(snapshot_id),
    clause_version_id TEXT REFERENCES clause_versions(clause_version_id),
    edge_id           TEXT REFERENCES clause_edges(edge_id),
    kind              TEXT NOT NULL CHECK (kind IN ('retroactive', 'prospective')),
    detail            TEXT NOT NULL DEFAULT '',
    recorded_at       TEXT NOT NULL,
    CHECK (clause_version_id IS NOT NULL OR edge_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_divergences_snapshot ON snapshot_divergences(snapshot_id);

CREATE TABLE IF NOT EXISTS review_decisions (
    decision_id       TEXT PRIMARY KEY,
    reviewer          TEXT NOT NULL,
    clause_version_id TEXT NOT NULL REFERENCES clause_versions(clause_version_id),
    verdict           TEXT NOT NULL CHECK (verdict IN ('accept', 'reject', 'abstain')),
    rationale         TEXT NOT NULL DEFAULT '',
    seen_epoch        INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'stale', 'confirmed', 'withdrawn')),
    recorded_at       TEXT NOT NULL,
    status_changed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_version ON review_decisions(clause_version_id);

CREATE TABLE IF NOT EXISTS objections (
    objection_id TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL CHECK (subject_kind IN ('clause_version', 'obligation', 'decision', 'snapshot', 'obligation_link')),
    subject_id   TEXT NOT NULL,
    raised_by    TEXT NOT NULL,
    detail       TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    resolved_at  TEXT,
    resolution   TEXT
);

CREATE TABLE IF NOT EXISTS review_tasks (
    task_id           TEXT PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('snapshot_divergence', 'stale_decision')),
    ref_id            TEXT NOT NULL,
    origin_version_id TEXT,
    title             TEXT NOT NULL,
    assignee          TEXT,
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    recorded_at       TEXT NOT NULL,
    resolved_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_tasks_status ON review_tasks(status);

-- 条款索引纪元：每次登记版本或谱系边递增，供评议方做乐观并发比对。
CREATE TABLE IF NOT EXISTS index_epoch (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    epoch INTEGER NOT NULL
);
INSERT OR IGNORE INTO index_epoch(id, epoch) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_version(version) VALUES (2);
