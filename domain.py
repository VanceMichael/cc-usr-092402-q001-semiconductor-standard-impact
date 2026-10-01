"""标准条款生效索引的领域逻辑。

数据模型要点：
- 所有事实表携带 recorded_at（系统得知时刻）与 retracted_at（撤回时刻），
  采用追加式记录，除 retracted_at 与状态列外不做 UPDATE，以支持任意历史时点重现；
- 条款版本以生效区间 [valid_from, valid_to) 描述有效时间；
- 版本间的替代/勘误/拆分/合并/撤回关系保存在 clause_edges，边的生效时刻
  （effective_at，缺省取后继版本 valid_from）决定前驱版本何时停止适用；
- 已签署的放行快照由数据库触发器保护，迟到的更正只登记为 snapshot_divergences
  并生成复核任务，绝不改写快照本身。
"""

import hashlib
import json

from db import new_id, now_ts


class NotFound(Exception):
    """引用的对象不存在。"""


class Conflict(Exception):
    """与既有记录冲突（如重复签署、重复撤回）。"""


class Forbidden(Exception):
    """无权访问受保密约束的资源。"""


SUCCESSOR_RELATIONS = ("replaces", "corrects", "splits_into", "merges_into")

TARGET_COLUMNS = {
    "product_config": ("config_id", "product_configs"),
    "supplier_declaration": ("declaration_id", "supplier_declarations"),
    "test_conclusion": ("conclusion_id", "test_conclusions"),
    "deviation_approval": ("approval_id", "deviation_approvals"),
}


# ---------------------------------------------------------------- 基础助手

def _one(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _all(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _require(row, what):
    if row is None:
        raise NotFound(f"{what}不存在")
    return row


def _active(alias, at):
    """追加式记录在某一系统时刻仍然有效的过滤条件与参数。"""
    return (
        f"{alias}.recorded_at <= ? AND ({alias}.retracted_at IS NULL OR ? < {alias}.retracted_at)",
        [at, at],
    )


def _placeholders(items):
    return ",".join("?" for _ in items)


def _log(conn, kind, payload, at):
    conn.execute(
        "INSERT INTO events(event_id, kind, payload, recorded_at) VALUES (?,?,?,?)",
        (new_id(), kind, json.dumps(payload, ensure_ascii=False, sort_keys=True), at),
    )


def current_epoch(conn) -> int:
    return conn.execute("SELECT epoch FROM index_epoch WHERE id = 1").fetchone()[0]


def _bump_epoch(conn):
    conn.execute("UPDATE index_epoch SET epoch = epoch + 1 WHERE id = 1")


# ---------------------------------------------------------------- 主数据登记

def create_project(conn, project_id, name, recorded_at=None):
    conn.execute(
        "INSERT INTO projects(project_id, name, recorded_at) VALUES (?,?,?)",
        (project_id, name, recorded_at or now_ts()),
    )
    return project_id


def create_standard(conn, standard_id, title, recorded_at=None):
    conn.execute(
        "INSERT INTO standards(standard_id, title, recorded_at) VALUES (?,?,?)",
        (standard_id, title, recorded_at or now_ts()),
    )
    return standard_id


def create_clause(conn, standard_id, clause_no, title, recorded_at=None):
    _require(_one(conn, "SELECT standard_id FROM standards WHERE standard_id = ?", (standard_id,)), "标准")
    clause_id = f"{standard_id}#{clause_no}"
    conn.execute(
        "INSERT INTO clauses(clause_id, standard_id, clause_no, title, recorded_at) VALUES (?,?,?,?,?)",
        (clause_id, standard_id, clause_no, title, recorded_at or now_ts()),
    )
    return clause_id


# ---------------------------------------------------------------- 条款谱系

def register_clause_version(conn, clause_id, stage, content, valid_from,
                            valid_to=None, edges=(), recorded_at=None):
    """登记条款版本及其相对前驱的谱系边（替代/勘误/拆分/合并）。"""
    _require(_one(conn, "SELECT clause_id FROM clauses WHERE clause_id = ?", (clause_id,)), "条款")
    at = recorded_at or now_ts()
    version_id = new_id()
    conn.execute(
        "INSERT INTO clause_versions"
        "(clause_version_id, clause_id, stage, content, valid_from, valid_to, recorded_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (version_id, clause_id, stage, content, valid_from, valid_to, at),
    )
    edge_ids = []
    affected_clauses = {clause_id}
    for edge in edges:
        relation = edge["relation"]
        if relation not in SUCCESSOR_RELATIONS:
            raise Conflict(f"版本登记仅支持后继关系 {SUCCESSOR_RELATIONS}，撤回请使用专用接口")
        from_version = _require(
            _one(conn, "SELECT * FROM clause_versions WHERE clause_version_id = ?",
                 (edge["from_version_id"],)),
            "前驱条款版本",
        )
        effective_at = edge.get("effective_at") or valid_from
        edge_id = new_id()
        conn.execute(
            "INSERT INTO clause_edges"
            "(edge_id, from_version_id, to_version_id, relation, effective_at, recorded_at)"
            " VALUES (?,?,?,?,?,?)",
            (edge_id, edge["from_version_id"], version_id, relation, effective_at, at),
        )
        edge_ids.append(edge_id)
        affected_clauses.add(from_version["clause_id"])
    _after_clause_change(conn, affected_clauses, at,
                         version_id=version_id, change_valid_from=valid_from)
    _log(conn, "clause_version_registered",
         {"clause_version_id": version_id, "clause_id": clause_id, "stage": stage,
          "valid_from": valid_from, "edge_ids": edge_ids}, at)
    return version_id, edge_ids


def register_withdrawal(conn, from_version_id, effective_at, recorded_at=None):
    """登记条款版本撤回：以一条 withdraws 谱系边表达，原版本与其关系保留。"""
    from_version = _require(
        _one(conn, "SELECT * FROM clause_versions WHERE clause_version_id = ?", (from_version_id,)),
        "条款版本",
    )
    at = recorded_at or now_ts()
    edge_id = new_id()
    conn.execute(
        "INSERT INTO clause_edges"
        "(edge_id, from_version_id, to_version_id, relation, effective_at, recorded_at)"
        " VALUES (?,?,NULL,'withdraws',?,?)",
        (edge_id, from_version_id, effective_at, at),
    )
    _after_clause_change(conn, {from_version["clause_id"]}, at,
                         edge_id=edge_id, change_valid_from=effective_at)
    _log(conn, "clause_version_withdrawn",
         {"edge_id": edge_id, "from_version_id": from_version_id, "effective_at": effective_at}, at)
    return edge_id


def _after_clause_change(conn, clause_ids, change_at,
                         version_id=None, edge_id=None, change_valid_from=None):
    """条款变化后的联动：推进纪元、登记快照偏差、标记基于旧版本的决定。"""
    _bump_epoch(conn)
    _flag_divergences(conn, clause_ids, change_at, version_id, edge_id, change_valid_from)
    _flag_stale_decisions(conn, clause_ids, change_at, version_id)


def _flag_divergences(conn, clause_ids, change_at, version_id, edge_id, change_valid_from):
    """已签署快照若包含受影响条款，且签署早于本次录入，则登记偏差与复核任务。"""
    ph = _placeholders(clause_ids)
    rows = _all(conn, f"""
        SELECT DISTINCT s.snapshot_id, s.as_of
        FROM release_snapshots s
        JOIN snapshot_items i ON i.snapshot_id = s.snapshot_id
        JOIN clause_versions v ON v.clause_version_id = i.clause_version_id
        WHERE s.signed_at IS NOT NULL AND s.signed_at < ? AND v.clause_id IN ({ph})
    """, [change_at, *clause_ids])
    for row in rows:
        retroactive = change_valid_from is not None and change_valid_from <= row["as_of"]
        origin = version_id
        if origin is None and edge_id is not None:
            edge = _one(conn, "SELECT from_version_id FROM clause_edges WHERE edge_id = ?", (edge_id,))
            origin = edge["from_version_id"]
        _record_divergence(
            conn, row["snapshot_id"], version_id, edge_id,
            "retroactive" if retroactive else "prospective",
            "签署后登记的条款变更，需复核放行结论", change_at, origin,
        )


def _record_divergence(conn, snapshot_id, version_id, edge_id, kind, detail, at, origin_version_id):
    divergence_id = new_id()
    conn.execute(
        "INSERT INTO snapshot_divergences"
        "(divergence_id, snapshot_id, clause_version_id, edge_id, kind, detail, recorded_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (divergence_id, snapshot_id, version_id, edge_id, kind, detail, at),
    )
    label = "追溯性" if kind == "retroactive" else "前瞻性"
    _open_task(conn, "snapshot_divergence", divergence_id,
               f"放行快照 {snapshot_id} 受到{label}条款变更影响，需复核",
               origin_version_id, at)
    _log(conn, "divergence_raised",
         {"divergence_id": divergence_id, "snapshot_id": snapshot_id, "kind": kind,
          "clause_version_id": version_id, "edge_id": edge_id}, at)
    return divergence_id


def _flag_stale_decisions(conn, clause_ids, change_at, origin_version_id):
    """条款变化后，把仍基于旧版本的未决评议决定标记为 stale。"""
    ph = _placeholders(clause_ids)
    rows = _all(conn, f"""
        SELECT d.decision_id FROM review_decisions d
        JOIN clause_versions v ON v.clause_version_id = d.clause_version_id
        WHERE d.status = 'open' AND v.clause_id IN ({ph}) AND v.recorded_at < ?
    """, [*clause_ids, change_at])
    for row in rows:
        conn.execute(
            "UPDATE review_decisions SET status = 'stale', status_changed_at = ? WHERE decision_id = ?",
            (change_at, row["decision_id"]),
        )
        _open_task(conn, "stale_decision", row["decision_id"],
                   f"评议决定 {row['decision_id']} 基于旧版本条款，需复核",
                   origin_version_id, change_at)
        _log(conn, "decision_staled",
             {"decision_id": row["decision_id"], "origin_version_id": origin_version_id}, change_at)


def _open_task(conn, kind, ref_id, title, origin_version_id, at):
    task_id = new_id()
    conn.execute(
        "INSERT INTO review_tasks(task_id, kind, ref_id, title, origin_version_id, status, recorded_at)"
        " VALUES (?,?,?,?,?,'open',?)",
        (task_id, kind, ref_id, title, origin_version_id, at),
    )
    return task_id


def applicable_versions(conn, clause_id, at, recorded_by=None):
    """条款在有效时点 at 适用的版本。

    版本自 valid_from 起适用，直到 valid_to 或任一谱系边的生效时刻。
    recorded_by 给定后，只考虑该系统时刻之前已录入且未撤回的版本与边，
    用于历史时点重现。
    """
    sql = """
        SELECT v.* FROM clause_versions v
        WHERE v.clause_id = ? AND v.valid_from <= ? AND (v.valid_to IS NULL OR ? < v.valid_to)
    """
    params = [clause_id, at, at]
    if recorded_by is not None:
        sql += " AND v.recorded_at <= ? AND (v.retracted_at IS NULL OR ? < v.retracted_at)"
        params += [recorded_by, recorded_by]
    else:
        sql += " AND v.retracted_at IS NULL"
    sql += """
          AND NOT EXISTS (
              SELECT 1 FROM clause_edges e
              LEFT JOIN clause_versions s ON s.clause_version_id = e.to_version_id
              WHERE e.from_version_id = v.clause_version_id
    """
    if recorded_by is not None:
        sql += " AND e.recorded_at <= ? AND (e.retracted_at IS NULL OR ? < e.retracted_at)"
        params += [recorded_by, recorded_by]
    else:
        sql += " AND e.retracted_at IS NULL"
    sql += " AND COALESCE(e.effective_at, s.valid_from) <= ?)"
    params.append(at)
    sql += " ORDER BY v.valid_from, v.recorded_at"
    return _all(conn, sql, params)


def lineage(conn, clause_id):
    """条款谱系：本条款的全部版本、与之相连的谱系边、以及拆分/合并关联的条款。"""
    _require(_one(conn, "SELECT clause_id FROM clauses WHERE clause_id = ?", (clause_id,)), "条款")
    versions = _all(conn,
                    "SELECT * FROM clause_versions WHERE clause_id = ? ORDER BY valid_from, recorded_at",
                    (clause_id,))
    edges = []
    related = []
    if versions:
        ids = [v["clause_version_id"] for v in versions]
        ph = _placeholders(ids)
        edges = _all(conn, f"""
            SELECT * FROM clause_edges
            WHERE from_version_id IN ({ph}) OR to_version_id IN ({ph})
            ORDER BY recorded_at
        """, ids + ids)
        endpoints = {e["from_version_id"] for e in edges}
        endpoints |= {e["to_version_id"] for e in edges if e["to_version_id"]}
        others = sorted(endpoints - set(ids))
        if others:
            oph = _placeholders(others)
            related = [r["clause_id"] for r in _all(conn, f"""
                SELECT DISTINCT clause_id FROM clause_versions WHERE clause_version_id IN ({oph})
            """, others)]
    return {"clause_id": clause_id, "versions": versions, "edges": edges,
            "related_clause_ids": sorted(set(related))}


def standard_changes(conn, standard_id):
    """标准的变化流水：版本登记与谱系边，按录入时间排序。"""
    _require(_one(conn, "SELECT standard_id FROM standards WHERE standard_id = ?", (standard_id,)), "标准")
    versions = _all(conn, """
        SELECT v.* FROM clause_versions v
        JOIN clauses c ON c.clause_id = v.clause_id
        WHERE c.standard_id = ? ORDER BY v.recorded_at
    """, (standard_id,))
    edges = _all(conn, """
        SELECT e.* FROM clause_edges e
        JOIN clause_versions v ON v.clause_version_id = e.from_version_id
        JOIN clauses c ON c.clause_id = v.clause_id
        WHERE c.standard_id = ? ORDER BY e.recorded_at
    """, (standard_id,))
    return {"standard_id": standard_id, "versions": versions, "edges": edges}


# ---------------------------------------------------------------- 义务与映射

def create_obligation(conn, clause_version_id, statement, recorded_at=None):
    _require(_one(conn, "SELECT clause_version_id FROM clause_versions WHERE clause_version_id = ?",
                  (clause_version_id,)), "条款版本")
    obligation_id = new_id()
    conn.execute(
        "INSERT INTO obligations(obligation_id, clause_version_id, statement, recorded_at)"
        " VALUES (?,?,?,?)",
        (obligation_id, clause_version_id, statement, recorded_at or now_ts()),
    )
    return obligation_id


def create_product_config(conn, part_number, product_line, description="", recorded_at=None):
    config_id = new_id()
    conn.execute(
        "INSERT INTO product_configs(config_id, part_number, product_line, description, recorded_at)"
        " VALUES (?,?,?,?,?)",
        (config_id, part_number, product_line, description, recorded_at or now_ts()),
    )
    return config_id


def create_supplier_declaration(conn, supplier, part_number, statement, recorded_at=None):
    declaration_id = new_id()
    conn.execute(
        "INSERT INTO supplier_declarations(declaration_id, supplier, part_number, statement, recorded_at)"
        " VALUES (?,?,?,?,?)",
        (declaration_id, supplier, part_number, statement, recorded_at or now_ts()),
    )
    return declaration_id


def create_test_conclusion(conn, report_no, config_id, result, summary="",
                           attachment_id=None, recorded_at=None):
    if config_id is not None:
        _require(_one(conn, "SELECT config_id FROM product_configs WHERE config_id = ?", (config_id,)),
                 "产品配置")
    if attachment_id is not None:
        _require(_one(conn, "SELECT attachment_id FROM attachments WHERE attachment_id = ?",
                      (attachment_id,)), "附件")
    conclusion_id = new_id()
    conn.execute(
        "INSERT INTO test_conclusions"
        "(conclusion_id, report_no, config_id, result, summary, attachment_id, recorded_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (conclusion_id, report_no, config_id, result, summary, attachment_id,
         recorded_at or now_ts()),
    )
    return conclusion_id


def create_deviation_approval(conn, config_id, approver, rationale,
                              expires_at=None, attachment_id=None, recorded_at=None):
    if config_id is not None:
        _require(_one(conn, "SELECT config_id FROM product_configs WHERE config_id = ?", (config_id,)),
                 "产品配置")
    if attachment_id is not None:
        _require(_one(conn, "SELECT attachment_id FROM attachments WHERE attachment_id = ?",
                      (attachment_id,)), "附件")
    approval_id = new_id()
    conn.execute(
        "INSERT INTO deviation_approvals"
        "(approval_id, config_id, approver, rationale, expires_at, attachment_id, recorded_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (approval_id, config_id, approver, rationale, expires_at, attachment_id,
         recorded_at or now_ts()),
    )
    return approval_id


def link_obligation(conn, obligation_id, target_kind, target_id, recorded_at=None):
    _require(_one(conn, "SELECT obligation_id FROM obligations WHERE obligation_id = ?",
                  (obligation_id,)), "义务")
    spec = TARGET_COLUMNS.get(target_kind)
    if spec is None:
        raise Conflict(f"未知映射目标类型：{target_kind}")
    column, table = spec
    _require(_one(conn, f"SELECT {column} FROM {table} WHERE {column} = ?", (target_id,)), "映射目标")
    link_id = new_id()
    conn.execute(
        f"INSERT INTO obligation_links(link_id, obligation_id, target_kind, {column}, recorded_at)"
        " VALUES (?,?,?,?,?)",
        (link_id, obligation_id, target_kind, target_id, recorded_at or now_ts()),
    )
    return link_id


def retract_link(conn, link_id, retracted_at=None):
    link = _require(_one(conn, "SELECT * FROM obligation_links WHERE link_id = ?", (link_id,)), "义务映射")
    if link["retracted_at"] is not None:
        raise Conflict("义务映射已撤回")
    at = retracted_at or now_ts()
    conn.execute("UPDATE obligation_links SET retracted_at = ? WHERE link_id = ?", (at, link_id))
    _log(conn, "obligation_link_retracted", {"link_id": link_id}, at)
    return at


# ---------------------------------------------------------------- 放行快照

def create_snapshot(conn, config_id, standard_id, as_of=None, created_at=None):
    """按 as_of 时点冻结适用条款版本与当前有效证据，形成待签署的放行快照。"""
    _require(_one(conn, "SELECT config_id FROM product_configs WHERE config_id = ?", (config_id,)),
             "产品配置")
    _require(_one(conn, "SELECT standard_id FROM standards WHERE standard_id = ?", (standard_id,)),
             "标准")
    created = created_at or now_ts()
    basis = as_of or created
    snapshot_id = new_id()
    conn.execute(
        "INSERT INTO release_snapshots(snapshot_id, config_id, standard_id, as_of, created_at)"
        " VALUES (?,?,?,?,?)",
        (snapshot_id, config_id, standard_id, basis, created),
    )
    versions = _applicable_for_standard(conn, standard_id, basis, created)
    for version in versions:
        conn.execute(
            "INSERT INTO snapshot_items(snapshot_id, clause_version_id) VALUES (?,?)",
            (snapshot_id, version["clause_version_id"]),
        )
    for link_id in _active_evidence(conn, [v["clause_version_id"] for v in versions], created):
        conn.execute(
            "INSERT INTO snapshot_evidence(snapshot_id, link_id) VALUES (?,?)",
            (snapshot_id, link_id),
        )
    _log(conn, "snapshot_created",
         {"snapshot_id": snapshot_id, "config_id": config_id, "standard_id": standard_id,
          "as_of": basis, "items": [v["clause_version_id"] for v in versions]}, created)
    return snapshot_id


def _applicable_for_standard(conn, standard_id, at, recorded_by):
    cond, params = _active("c", recorded_by)
    clauses = _all(conn,
                   f"SELECT c.* FROM clauses c WHERE c.standard_id = ? AND {cond}",
                   [standard_id, *params])
    versions = []
    for clause in clauses:
        versions.extend(applicable_versions(conn, clause["clause_id"], at, recorded_by=recorded_by))
    return versions


def _active_evidence(conn, version_ids, at):
    if not version_ids:
        return []
    ph = _placeholders(version_ids)
    cond_o, params_o = _active("o", at)
    cond_l, params_l = _active("l", at)
    rows = _all(conn, f"""
        SELECT l.link_id FROM obligation_links l
        JOIN obligations o ON o.obligation_id = l.obligation_id
        WHERE o.clause_version_id IN ({ph}) AND {cond_o} AND {cond_l}
    """, [*version_ids, *params_o, *params_l])
    return [r["link_id"] for r in rows]


def sign_snapshot(conn, snapshot_id, signer, signed_at=None):
    """签署放行快照：计算摘要并锁定。

    签署前若已存在适用于 as_of 时点但未冻结进快照的迟到版本，
    登记追溯性偏差，而不是把版本悄悄并入快照。
    """
    snap = _require(_one(conn, "SELECT * FROM release_snapshots WHERE snapshot_id = ?",
                         (snapshot_id,)), "放行快照")
    if snap["signed_at"] is not None:
        raise Conflict("放行快照已签署，不可重复签署")
    at = signed_at or now_ts()
    frozen = {r["clause_version_id"] for r in _all(
        conn, "SELECT clause_version_id FROM snapshot_items WHERE snapshot_id = ?", (snapshot_id,))}
    for version in _applicable_for_standard(conn, snap["standard_id"], snap["as_of"], at):
        if version["clause_version_id"] not in frozen:
            _record_divergence(conn, snapshot_id, version["clause_version_id"], None,
                               "retroactive", "签署前已适用但未冻结进快照的迟到版本",
                               at, version["clause_version_id"])
    digest = _snapshot_digest(conn, snapshot_id, snap)
    conn.execute(
        "UPDATE release_snapshots SET signed_at = ?, signer = ?, digest = ? WHERE snapshot_id = ?",
        (at, signer, digest, snapshot_id),
    )
    _log(conn, "snapshot_signed",
         {"snapshot_id": snapshot_id, "signer": signer, "digest": digest}, at)
    return {"snapshot_id": snapshot_id, "signed_at": at, "digest": digest}


def _snapshot_digest(conn, snapshot_id, snap):
    items = sorted(r["clause_version_id"] for r in _all(
        conn, "SELECT clause_version_id FROM snapshot_items WHERE snapshot_id = ?", (snapshot_id,)))
    evidence = sorted(r["link_id"] for r in _all(
        conn, "SELECT link_id FROM snapshot_evidence WHERE snapshot_id = ?", (snapshot_id,)))
    payload = {
        "snapshot_id": snapshot_id,
        "config_id": snap["config_id"],
        "standard_id": snap["standard_id"],
        "as_of": snap["as_of"],
        "items": items,
        "evidence": evidence,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def get_snapshot(conn, snapshot_id):
    snap = _require(_one(conn, "SELECT * FROM release_snapshots WHERE snapshot_id = ?",
                         (snapshot_id,)), "放行快照")
    snap["items"] = [r["clause_version_id"] for r in _all(
        conn, "SELECT clause_version_id FROM snapshot_items WHERE snapshot_id = ? ORDER BY clause_version_id",
        (snapshot_id,))]
    snap["evidence"] = [r["link_id"] for r in _all(
        conn, "SELECT link_id FROM snapshot_evidence WHERE snapshot_id = ? ORDER BY link_id",
        (snapshot_id,))]
    snap["divergences"] = _all(
        conn, "SELECT * FROM snapshot_divergences WHERE snapshot_id = ? ORDER BY recorded_at",
        (snapshot_id,))
    return snap


# ---------------------------------------------------------------- 评议与异议

def record_decision(conn, reviewer, clause_version_id, verdict, rationale="",
                    seen_epoch=None, recorded_at=None):
    """登记评议决定；若所基于的条款版本已有更新的版本或谱系边，则标记为 stale。"""
    based = _require(_one(conn, "SELECT * FROM clause_versions WHERE clause_version_id = ?",
                          (clause_version_id,)), "条款版本")
    at = recorded_at or now_ts()
    epoch = current_epoch(conn)
    newer_versions = _one(conn, """
        SELECT COUNT(*) AS c FROM clause_versions
        WHERE clause_id = ? AND recorded_at > ?
    """, (based["clause_id"], based["recorded_at"]))["c"]
    newer_edges = _one(conn, """
        SELECT COUNT(*) AS c FROM clause_edges e
        JOIN clause_versions fv ON fv.clause_version_id = e.from_version_id
        WHERE fv.clause_id = ? AND e.recorded_at > ?
    """, (based["clause_id"], based["recorded_at"]))["c"]
    status = "stale" if (newer_versions or newer_edges) else "open"
    decision_id = new_id()
    conn.execute(
        "INSERT INTO review_decisions"
        "(decision_id, reviewer, clause_version_id, verdict, rationale, seen_epoch,"
        " status, recorded_at, status_changed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (decision_id, reviewer, clause_version_id, verdict, rationale,
         seen_epoch if seen_epoch is not None else epoch,
         status, at, at if status == "stale" else None),
    )
    if status == "stale":
        _open_task(conn, "stale_decision", decision_id,
                   f"评议决定 {decision_id} 基于旧版本条款，需复核", clause_version_id, at)
    _log(conn, "decision_recorded",
         {"decision_id": decision_id, "clause_version_id": clause_version_id,
          "verdict": verdict, "status": status}, at)
    return decision_id, status


def list_decisions(conn, status=None):
    if status:
        return _all(conn,
                    "SELECT * FROM review_decisions WHERE status = ? ORDER BY recorded_at",
                    (status,))
    return _all(conn, "SELECT * FROM review_decisions ORDER BY recorded_at")


def raise_objection(conn, subject_kind, subject_id, raised_by, detail, recorded_at=None):
    objection_id = new_id()
    conn.execute(
        "INSERT INTO objections(objection_id, subject_kind, subject_id, raised_by, detail, recorded_at)"
        " VALUES (?,?,?,?,?,?)",
        (objection_id, subject_kind, subject_id, raised_by, detail, recorded_at or now_ts()),
    )
    return objection_id


def resolve_objection(conn, objection_id, resolution, resolved_at=None):
    obj = _require(_one(conn, "SELECT * FROM objections WHERE objection_id = ?", (objection_id,)), "异议")
    if obj["resolved_at"] is not None:
        raise Conflict("异议已关闭")
    at = resolved_at or now_ts()
    conn.execute(
        "UPDATE objections SET resolved_at = ?, resolution = ? WHERE objection_id = ?",
        (at, resolution, objection_id),
    )
    _log(conn, "objection_resolved", {"objection_id": objection_id, "resolution": resolution}, at)
    return at


def list_objections(conn, open_only=False):
    if open_only:
        return _all(conn, "SELECT * FROM objections WHERE resolved_at IS NULL ORDER BY recorded_at")
    return _all(conn, "SELECT * FROM objections ORDER BY recorded_at")


# ---------------------------------------------------------------- 保密附件

def create_attachment(conn, name, content, media_type="application/octet-stream",
                      confidential=False, recorded_at=None):
    attachment_id = new_id()
    conn.execute(
        "INSERT INTO attachments(attachment_id, name, media_type, content, confidential, recorded_at)"
        " VALUES (?,?,?,?,?,?)",
        (attachment_id, name, media_type, content, 1 if confidential else 0,
         recorded_at or now_ts()),
    )
    return attachment_id


def grant_attachment(conn, attachment_id, project_id, recorded_at=None):
    _require(_one(conn, "SELECT attachment_id FROM attachments WHERE attachment_id = ?",
                  (attachment_id,)), "附件")
    _require(_one(conn, "SELECT project_id FROM projects WHERE project_id = ?", (project_id,)), "项目")
    grant_id = new_id()
    conn.execute(
        "INSERT INTO attachment_grants(grant_id, attachment_id, project_id, recorded_at)"
        " VALUES (?,?,?,?)",
        (grant_id, attachment_id, project_id, recorded_at or now_ts()),
    )
    return grant_id


def revoke_grant(conn, attachment_id, project_id, revoked_at=None):
    grant = _require(_one(conn, """
        SELECT * FROM attachment_grants
        WHERE attachment_id = ? AND project_id = ? AND retracted_at IS NULL
    """, (attachment_id, project_id)), "附件授权")
    at = revoked_at or now_ts()
    conn.execute("UPDATE attachment_grants SET retracted_at = ? WHERE grant_id = ?",
                 (at, grant["grant_id"]))
    _log(conn, "attachment_grant_revoked",
         {"attachment_id": attachment_id, "project_id": project_id}, at)
    return at


def attachment_content(conn, attachment_id, project_id):
    """读取附件内容；保密附件仅向持有有效授权的项目开放。"""
    att = _require(_one(conn, """
        SELECT * FROM attachments WHERE attachment_id = ? AND retracted_at IS NULL
    """, (attachment_id,)), "附件")
    if att["confidential"]:
        if not project_id:
            raise Forbidden("保密附件需要以项目身份访问")
        grant = _one(conn, """
            SELECT * FROM attachment_grants
            WHERE attachment_id = ? AND project_id = ? AND retracted_at IS NULL
        """, (attachment_id, project_id))
        if grant is None:
            raise Forbidden(f"项目 {project_id} 未获该附件授权")
    return att


# ---------------------------------------------------------------- 影响追踪

def impact(conn, clause_version_id):
    """从一次条款变化出发，追踪受影响的对象与全部待复核事项。"""
    _require(_one(conn, "SELECT clause_version_id FROM clause_versions WHERE clause_version_id = ?",
                  (clause_version_id,)), "条款版本")
    version_ids, edge_ids = _genealogy(conn, clause_version_id)
    versions = sorted(version_ids)
    vph = _placeholders(versions)
    clause_ids = sorted({r["clause_id"] for r in _all(
        conn, f"SELECT clause_id FROM clause_versions WHERE clause_version_id IN ({vph})", versions)})
    obligations = _all(conn, f"""
        SELECT * FROM obligations WHERE clause_version_id IN ({vph}) AND retracted_at IS NULL
        ORDER BY recorded_at
    """, versions)
    links = []
    if obligations:
        oph = _placeholders(obligations)
        links = _all(conn, f"""
            SELECT * FROM obligation_links
            WHERE obligation_id IN ({oph}) AND retracted_at IS NULL ORDER BY recorded_at
        """, [o["obligation_id"] for o in obligations])
    divergence_sql = f"SELECT * FROM snapshot_divergences WHERE clause_version_id IN ({vph})"
    params = list(versions)
    if edge_ids:
        eph = _placeholders(edge_ids)
        divergence_sql += f" OR edge_id IN ({eph})"
        params += sorted(edge_ids)
    divergences = _all(conn, divergence_sql + " ORDER BY recorded_at", params)
    open_tasks = _all(conn, f"""
        SELECT * FROM review_tasks WHERE status = 'open' AND origin_version_id IN ({vph})
        ORDER BY recorded_at
    """, versions)
    return {
        "clause_version_id": clause_version_id,
        "related_clause_version_ids": versions,
        "clause_ids": clause_ids,
        "obligations": obligations,
        "affected": _resolve_targets(conn, links),
        "divergences": divergences,
        "open_tasks": open_tasks,
        "open_objections": _open_objections_for(
            conn, versions, [o["obligation_id"] for o in obligations]),
    }


def _genealogy(conn, start_version_id):
    """沿谱系边双向遍历，收集与起始版本相连的全部版本与边。"""
    seen_versions = {start_version_id}
    seen_edges = {}
    frontier = [start_version_id]
    while frontier:
        ph = _placeholders(frontier)
        rows = _all(conn, f"""
            SELECT * FROM clause_edges
            WHERE retracted_at IS NULL AND (from_version_id IN ({ph}) OR to_version_id IN ({ph}))
        """, frontier + frontier)
        nxt = []
        for edge in rows:
            if edge["edge_id"] in seen_edges:
                continue
            seen_edges[edge["edge_id"]] = edge
            for end in (edge["from_version_id"], edge["to_version_id"]):
                if end and end not in seen_versions:
                    seen_versions.add(end)
                    nxt.append(end)
        frontier = nxt
    return seen_versions, set(seen_edges)


def _resolve_targets(conn, links):
    affected = {
        "product_configs": [],
        "supplier_declarations": [],
        "test_conclusions": [],
        "deviation_approvals": [],
    }
    for kind, (column, table) in TARGET_COLUMNS.items():
        ids = sorted({link[column] for link in links if link["target_kind"] == kind})
        if ids:
            ph = _placeholders(ids)
            affected[{
                "product_config": "product_configs",
                "supplier_declaration": "supplier_declarations",
                "test_conclusion": "test_conclusions",
                "deviation_approval": "deviation_approvals",
            }[kind]] = _all(conn, f"SELECT * FROM {table} WHERE {column} IN ({ph})", ids)
    return affected


def _open_objections_for(conn, version_ids, obligation_ids):
    clauses = []
    params = []
    if version_ids:
        vph = _placeholders(version_ids)
        clauses.append(f"(subject_kind = 'clause_version' AND subject_id IN ({vph}))")
        params += list(version_ids)
        clauses.append(
            f"(subject_kind = 'decision' AND subject_id IN"
            f" (SELECT decision_id FROM review_decisions WHERE clause_version_id IN ({vph})))")
        params += list(version_ids)
    if obligation_ids:
        oph = _placeholders(obligation_ids)
        clauses.append(f"(subject_kind = 'obligation' AND subject_id IN ({oph}))")
        params += list(obligation_ids)
    if not clauses:
        return []
    return _all(conn, f"""
        SELECT * FROM objections
        WHERE resolved_at IS NULL AND ({' OR '.join(clauses)})
        ORDER BY recorded_at
    """, params)


# ---------------------------------------------------------------- 复核任务

def list_review_tasks(conn, status=None, origin_version_id=None):
    sql = "SELECT * FROM review_tasks WHERE 1 = 1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if origin_version_id:
        sql += " AND origin_version_id = ?"
        params.append(origin_version_id)
    return _all(conn, sql + " ORDER BY recorded_at", params)


def resolve_task(conn, task_id, resolved_at=None):
    task = _require(_one(conn, "SELECT * FROM review_tasks WHERE task_id = ?", (task_id,)), "复核任务")
    if task["status"] == "resolved":
        raise Conflict("复核任务已关闭")
    at = resolved_at or now_ts()
    conn.execute(
        "UPDATE review_tasks SET status = 'resolved', resolved_at = ? WHERE task_id = ?",
        (at, task_id),
    )
    return at


# ---------------------------------------------------------------- 历史时点重现

def asof(conn, standard_id, at):
    """重现历史时点 at：当时适用的条款版本、当时有效的证据、当时未决的异议。"""
    _require(_one(conn, "SELECT standard_id FROM standards WHERE standard_id = ?", (standard_id,)),
             "标准")
    cond, params = _active("c", at)
    clauses = _all(conn,
                   f"SELECT c.* FROM clauses c WHERE c.standard_id = ? AND {cond} ORDER BY c.clause_no",
                   [standard_id, *params])
    clause_entries = []
    for clause in clauses:
        versions = applicable_versions(conn, clause["clause_id"], at, recorded_by=at)
        clause_entries.append({
            "clause_id": clause["clause_id"],
            "clause_no": clause["clause_no"],
            "title": clause["title"],
            "applicable_versions": [v["clause_version_id"] for v in versions],
        })
    cond_o, params_o = _active("o", at)
    cond_l, params_l = _active("l", at)
    evidence = _all(conn, f"""
        SELECT l.link_id, l.obligation_id, l.target_kind,
               COALESCE(l.config_id, l.declaration_id, l.conclusion_id, l.approval_id) AS target_id
        FROM obligation_links l
        JOIN obligations o ON o.obligation_id = l.obligation_id
        JOIN clause_versions v ON v.clause_version_id = o.clause_version_id
        JOIN clauses c ON c.clause_id = v.clause_id
        WHERE c.standard_id = ? AND {cond_o} AND {cond_l}
        ORDER BY l.recorded_at
    """, [standard_id, *params_o, *params_l])
    objections = _all(conn, """
        SELECT ob.* FROM objections ob
        WHERE ob.recorded_at <= ? AND (ob.resolved_at IS NULL OR ? < ob.resolved_at)
          AND (
            (ob.subject_kind = 'clause_version' AND ob.subject_id IN (
                SELECT v.clause_version_id FROM clause_versions v
                JOIN clauses c ON c.clause_id = v.clause_id WHERE c.standard_id = ?))
         OR (ob.subject_kind = 'obligation' AND ob.subject_id IN (
                SELECT o.obligation_id FROM obligations o
                JOIN clause_versions v ON v.clause_version_id = o.clause_version_id
                JOIN clauses c ON c.clause_id = v.clause_id WHERE c.standard_id = ?))
         OR (ob.subject_kind = 'decision' AND ob.subject_id IN (
                SELECT d.decision_id FROM review_decisions d
                JOIN clause_versions v ON v.clause_version_id = d.clause_version_id
                JOIN clauses c ON c.clause_id = v.clause_id WHERE c.standard_id = ?))
         OR (ob.subject_kind = 'snapshot' AND ob.subject_id IN (
                SELECT s.snapshot_id FROM release_snapshots s WHERE s.standard_id = ?))
          )
        ORDER BY ob.recorded_at
    """, [at, at, standard_id, standard_id, standard_id, standard_id])
    return {
        "standard_id": standard_id,
        "at": at,
        "clauses": clause_entries,
        "evidence": evidence,
        "open_objections": objections,
    }
