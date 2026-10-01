"""条款谱系、义务映射与影响闭环的领域逻辑。

双时态语义：
- at（生效时间）：条款版本在现实世界中的生效区间 [valid_from, valid_to)，
  继承关系自 effective_at 起终止旧版本的适用性；
- knowledge（获知时间）：只考虑 recorded_at <= knowledge 的记录。
两条时间轴分离后，迟到的更正（recorded_at 晚于 valid_from）只会改变
“现在回头看”的结论，不会改写当时签署的快照与决定。
"""
import hashlib
import json
from datetime import datetime, timezone

TARGET_TABLES = {
    "product_config": "products",
    "supplier_declaration": "supplier_declarations",
    "test_conclusion": "test_reports",
    "deviation_approval": "deviation_approvals",
}

TARGET_LABELS = {
    "product_config": "产品配置",
    "supplier_declaration": "供应商声明",
    "test_conclusion": "试验结论",
    "deviation_approval": "偏离批准",
}

RELATION_LABELS = {
    "replaces": "替代",
    "splits_into": "拆分",
    "merges_into": "合并",
    "withdraws": "撤回",
}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    """把 ISO 8601 输入归一化为固定的 UTC 字符串，保证字典序可比较。"""
    s = str(value).strip()
    if len(s) == 10:
        s += "T00:00:00"
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_row(db, table, row_id):
    return db.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()


# ---------- 条款谱系 ----------

def applicable_versions(db, clause_id, at, knowledge):
    """at 时点适用、且 knowledge 时点已获知的条款版本，按适用优先级降序。

    草案（draft）只是提案，不进入适用集合。
    """
    return db.execute(
        """
        SELECT cv.* FROM clause_versions cv
        WHERE cv.clause_id = ?
          AND cv.stage != 'draft'
          AND cv.recorded_at <= ?
          AND cv.valid_from <= ?
          AND (cv.valid_to IS NULL OR cv.valid_to > ?)
          AND NOT EXISTS (
            SELECT 1 FROM clause_relations r
            WHERE r.from_version_id = cv.id
              AND r.recorded_at <= ?
              AND r.effective_at <= ?
          )
        ORDER BY cv.valid_from DESC, cv.recorded_at DESC, cv.id DESC
        """,
        (clause_id, knowledge, at, at, knowledge, at),
    ).fetchall()


def head_version(db, clause_id, knowledge):
    """knowledge 时点已获知的最新非草案版本（按生效起点、登记时间排序）。"""
    return db.execute(
        """
        SELECT * FROM clause_versions
        WHERE clause_id = ? AND recorded_at <= ? AND stage != 'draft'
        ORDER BY valid_from DESC, recorded_at DESC, id DESC
        LIMIT 1
        """,
        (clause_id, knowledge),
    ).fetchone()


def is_withdrawn(db, version_id, knowledge):
    return db.execute(
        """
        SELECT 1 FROM clause_relations
        WHERE from_version_id = ? AND relation = 'withdraws' AND recorded_at <= ?
        LIMIT 1
        """,
        (version_id, knowledge),
    ).fetchone() is not None


def mapping_stale(db, mapping, knowledge):
    """判断映射决定是否基于旧版本。返回 (stale, reason, head)。

    草案上的前瞻映射不做拦截，仅在草案之后发布了非草案版本时提示。
    """
    basis = get_row(db, "clause_versions", mapping["basis_version_id"])
    if basis is None:
        return True, "依据版本不存在", None
    head = head_version(db, basis["clause_id"], knowledge)
    if basis["stage"] == "draft":
        if head is not None and head["recorded_at"] > basis["recorded_at"]:
            return True, f"草案之后已发布新版本 {head['version_label']}", head
        return False, None, head
    if head is None:
        return False, None, None
    if is_withdrawn(db, head["id"], knowledge):
        return True, f"条款已撤回（最新版本 {head['version_label']} 已撤回）", head
    if head["id"] != basis["id"]:
        return True, f"存在更新版本 {head['version_label']}（#{head['id']}）", head
    return False, None, head


# ---------- 义务映射目标 ----------

def target_summary(db, target_type, target_id):
    table = TARGET_TABLES.get(target_type)
    if table is None:
        return None
    row = get_row(db, table, target_id)
    if row is None:
        return None
    summary = dict(row)
    summary["target_type"] = target_type
    summary["target_label"] = TARGET_LABELS[target_type]
    return summary


# ---------- 复核任务 ----------

def insert_task(db, kind, reason, opened_at, clause_version_id=None,
                mapping_id=None, snapshot_id=None, relation_id=None):
    """同一来源的未决任务不重复生成。"""
    dup = db.execute(
        """
        SELECT id FROM review_tasks
        WHERE status = 'open' AND kind = ?
          AND clause_version_id IS ? AND mapping_id IS ?
          AND snapshot_id IS ? AND relation_id IS ?
        """,
        (kind, clause_version_id, mapping_id, snapshot_id, relation_id),
    ).fetchone()
    if dup:
        return None
    cur = db.execute(
        """
        INSERT INTO review_tasks
          (kind, clause_version_id, relation_id, mapping_id, snapshot_id, reason, opened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (kind, clause_version_id, relation_id, mapping_id, snapshot_id, reason, opened_at),
    )
    return dict(get_row(db, "review_tasks", cur.lastrowid))


def fanout_tasks(db, prev_version_ids, effective_from, now, default_kind, label,
                 change_version_id=None, relation_id=None):
    """一次标准变化的扇出：为受影响的开放映射与已签署快照生成复核任务。

    变化生效点早于决定/签署时间时，属于迟到的更正（late_correction），
    快照内容保持不变，仅以任务标记差异。
    """
    created = []
    if not prev_version_ids:
        return created
    marks = ",".join("?" for _ in prev_version_ids)
    mappings = db.execute(
        f"SELECT * FROM obligation_mappings WHERE closed_at IS NULL "
        f"AND clause_version_id IN ({marks}) ORDER BY id",
        prev_version_ids,
    ).fetchall()
    for m in mappings:
        if effective_from <= m["decided_at"]:
            kind = "late_correction"
            reason = (f"迟到的更正：「{label}」生效追溯至 {effective_from}，"
                      f"早于映射 #{m['id']} 的决定时间 {m['decided_at']}")
        else:
            kind = default_kind
            reason = f"条款变化「{label}」自 {effective_from} 起生效，映射 #{m['id']} 需复核"
        task = insert_task(db, kind, reason, now, clause_version_id=change_version_id,
                           mapping_id=m["id"], relation_id=relation_id)
        if task:
            created.append(task)
    snapshots = db.execute(
        f"""
        SELECT DISTINCT s.* FROM release_snapshots s
        JOIN release_snapshot_items i ON i.snapshot_id = s.id
        WHERE i.clause_version_id IN ({marks}) ORDER BY s.id
        """,
        prev_version_ids,
    ).fetchall()
    for s in snapshots:
        if effective_from <= s["signed_at"]:
            kind = "late_correction"
            reason = (f"迟到的更正：「{label}」生效追溯至 {effective_from}，"
                      f"早于快照 #{s['id']} 签署时间 {s['signed_at']}；"
                      f"已签署内容保持不变，需复核是否重新放行")
        else:
            kind = default_kind
            reason = f"条款变化「{label}」自 {effective_from} 起生效，放行快照 #{s['id']} 需复核"
        task = insert_task(db, kind, reason, now, clause_version_id=change_version_id,
                           snapshot_id=s["id"], relation_id=relation_id)
        if task:
            created.append(task)
    return created


def resolve_tasks_for_mapping(db, mapping_id, resolution, closed_by, closed_at):
    cur = db.execute(
        """
        UPDATE review_tasks
        SET status = 'resolved', resolution = ?, closed_by = ?, closed_at = ?
        WHERE mapping_id = ? AND status = 'open'
        """,
        (resolution, closed_by, closed_at, mapping_id),
    )
    return cur.rowcount


# ---------- 放行快照 ----------

def build_snapshot_items(db, standard_id, part_number, at, knowledge):
    """at 时点适用条款 + 该料号当前开放的义务映射，作为快照内容。"""
    items = []
    clauses = db.execute(
        "SELECT * FROM clauses WHERE standard_id = ? ORDER BY clause_no", (standard_id,)
    ).fetchall()
    for clause in clauses:
        apps = applicable_versions(db, clause["id"], at, knowledge)
        if not apps:
            continue
        version = apps[0]
        mappings = db.execute(
            "SELECT * FROM obligation_mappings "
            "WHERE clause_version_id = ? AND closed_at IS NULL ORDER BY id",
            (version["id"],),
        ).fetchall()
        matched = [
            m for m in mappings
            if (target_summary(db, m["target_type"], m["target_id"]) or {}).get("part_number")
            == part_number
        ]
        if matched:
            for m in matched:
                items.append({
                    "clause_id": clause["id"],
                    "clause_version_id": version["id"],
                    "mapping_id": m["id"],
                    "disposition": m["disposition"],
                })
        else:
            items.append({
                "clause_id": clause["id"],
                "clause_version_id": version["id"],
                "mapping_id": None,
                "disposition": "pending",
            })
    return items


def snapshot_digest(header, items):
    canon = json.dumps({"header": header, "items": items},
                       ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def snapshot_items_signature(db, items):
    """按条款聚合的版本+映射签名，用于快照差异对比。"""
    signature = {}
    for item in items:
        version = get_row(db, "clause_versions", item["clause_version_id"])
        entry = signature.setdefault(item["clause_id"], {
            "clause_version_id": item["clause_version_id"],
            "version_label": version["version_label"] if version else None,
            "mappings": [],
        })
        if item["mapping_id"] is not None:
            entry["mappings"].append({
                "mapping_id": item["mapping_id"],
                "disposition": item["disposition"],
            })
    for entry in signature.values():
        entry["mappings"].sort(key=lambda m: m["mapping_id"])
    return signature


def snapshot_delta(db, frozen_items, computed_items):
    frozen = snapshot_items_signature(db, frozen_items)
    computed = snapshot_items_signature(db, computed_items)
    delta = []
    for clause_id in sorted(set(frozen) | set(computed)):
        before = frozen.get(clause_id)
        after = computed.get(clause_id)
        if before != after:
            clause = get_row(db, "clauses", clause_id)
            delta.append({
                "clause_id": clause_id,
                "clause_no": clause["clause_no"] if clause else None,
                "frozen": before,
                "computed": after,
            })
    return delta


# ---------- 影响追踪与历史重现 ----------

def impact(db, version_id):
    """从一次标准变化（条款版本）追到全部受影响对象与待复核事项。"""
    version = get_row(db, "clause_versions", version_id)
    if version is None:
        return None
    clause = get_row(db, "clauses", version["clause_id"])
    relations = db.execute(
        "SELECT * FROM clause_relations WHERE from_version_id = ? OR to_version_id = ? "
        "ORDER BY id",
        (version_id, version_id),
    ).fetchall()
    relation_ids = [r["id"] for r in relations]
    if relation_ids:
        marks = ",".join("?" for _ in relation_ids)
        tasks = db.execute(
            f"SELECT * FROM review_tasks WHERE clause_version_id = ? "
            f"OR relation_id IN ({marks}) ORDER BY id",
            (version_id, *relation_ids),
        ).fetchall()
    else:
        tasks = db.execute(
            "SELECT * FROM review_tasks WHERE clause_version_id = ? ORDER BY id",
            (version_id,),
        ).fetchall()

    mapping_ids = sorted({t["mapping_id"] for t in tasks if t["mapping_id"] is not None})
    snapshot_ids = sorted({t["snapshot_id"] for t in tasks if t["snapshot_id"] is not None})

    affected_mappings = []
    part_numbers = set()
    for mid in mapping_ids:
        mapping = get_row(db, "obligation_mappings", mid)
        target = target_summary(db, mapping["target_type"], mapping["target_id"])
        if target and target.get("part_number"):
            part_numbers.add(target["part_number"])
        affected_mappings.append({**dict(mapping), "target": target})

    affected_snapshots = []
    for sid in snapshot_ids:
        snapshot = get_row(db, "release_snapshots", sid)
        part_numbers.add(snapshot["part_number"])
        affected_snapshots.append(dict(snapshot))

    commitments = []
    if part_numbers:
        marks = ",".join("?" for _ in part_numbers)
        commitments = [dict(r) for r in db.execute(
            f"SELECT * FROM customer_commitments WHERE part_number IN ({marks}) ORDER BY id",
            sorted(part_numbers),
        ).fetchall()]

    open_objections = []
    for column, ids in (("mapping_id", mapping_ids), ("snapshot_id", snapshot_ids)):
        if ids:
            marks = ",".join("?" for _ in ids)
            open_objections += db.execute(
                f"SELECT * FROM objections WHERE {column} IN ({marks}) AND status = 'open' "
                f"ORDER BY id",
                ids,
            ).fetchall()

    return {
        "version": dict(version),
        "clause": dict(clause) if clause else None,
        "relations": [dict(r) for r in relations],
        "tasks": [dict(t) for t in tasks],
        "open_task_count": sum(1 for t in tasks if t["status"] == "open"),
        "affected_mappings": affected_mappings,
        "affected_snapshots": affected_snapshots,
        "affected_part_numbers": sorted(part_numbers),
        "customer_commitments": commitments,
        "open_objections": [dict(o) for o in open_objections],
    }


def as_of(db, at, knowledge, standard_id=None):
    """重现 at 时点（按 knowledge 时点所获知）的条款、证据、未决异议与待复核事项。"""
    clauses = db.execute(
        "SELECT * FROM clauses ORDER BY standard_id, clause_no"
    ).fetchall()
    clause_views = []
    for clause in clauses:
        if standard_id is not None and clause["standard_id"] != standard_id:
            continue
        apps = applicable_versions(db, clause["id"], at, knowledge)
        clause_views.append({
            "clause_id": clause["id"],
            "clause_no": clause["clause_no"],
            "title": clause["title"],
            "standard_id": clause["standard_id"],
            "current": dict(apps[0]) if apps else None,
            "applicable": [dict(a) for a in apps],
        })

    evidence = []
    mappings = db.execute(
        "SELECT * FROM obligation_mappings "
        "WHERE decided_at <= ? AND (closed_at IS NULL OR closed_at > ?) ORDER BY id",
        (at, at),
    ).fetchall()
    for m in mappings:
        version = get_row(db, "clause_versions", m["clause_version_id"])
        clause = get_row(db, "clauses", version["clause_id"])
        if standard_id is not None and clause["standard_id"] != standard_id:
            continue
        evidence.append({
            **dict(m),
            "clause_no": clause["clause_no"],
            "version_label": version["version_label"],
            "target": target_summary(db, m["target_type"], m["target_id"]),
        })

    open_objections = [dict(o) for o in db.execute(
        "SELECT * FROM objections WHERE raised_at <= ? "
        "AND (resolved_at IS NULL OR resolved_at > ?) ORDER BY id",
        (at, at),
    ).fetchall()]

    open_tasks = [dict(t) for t in db.execute(
        "SELECT * FROM review_tasks WHERE opened_at <= ? "
        "AND (closed_at IS NULL OR closed_at > ?) ORDER BY id",
        (at, at),
    ).fetchall()]

    snapshots = db.execute(
        "SELECT * FROM release_snapshots WHERE signed_at <= ? ORDER BY id", (at,)
    ).fetchall()
    if standard_id is not None:
        snapshots = [s for s in snapshots if s["standard_id"] == standard_id]

    return {
        "at": at,
        "knowledge_at": knowledge,
        "clauses": clause_views,
        "evidence": evidence,
        "open_objections": open_objections,
        "open_review_tasks": open_tasks,
        "snapshots": [dict(s) for s in snapshots],
    }
