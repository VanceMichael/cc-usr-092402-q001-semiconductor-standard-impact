"""半导体标准变更影响闭环 —— 条款生效索引服务。

在基线健康检查之上扩展：
- 条款版本谱系：按生效区间维护，替代/拆分/合并/撤回显式记录并保留原关系；
- 义务映射：条款义务映射到产品配置、供应商声明、试验结论、偏离批准；
- 放行快照：签署后不可改写，迟到的更正以复核任务标记而非篡改；
- 评议并发：基于旧版本的决定在写入时被检测（409），可显式放行并留痕；
- 保密附件：仅获授权项目的成员可读；
- 影响闭环与历史重现：/changes/{id}/impact 追到全部待复核事项，
  /as-of 按任意历史时点重现当时适用的条款、证据与未决异议。
"""
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

import domain
from db import connect, migrate

router = APIRouter()


# ---------- 请求模型 ----------

class UserIn(BaseModel):
    name: str
    role: str = "engineer"


class ProjectIn(BaseModel):
    code: str
    name: str


class MemberIn(BaseModel):
    user: str
    role: str = "member"


class StandardIn(BaseModel):
    code: str
    title: str


class ClauseIn(BaseModel):
    clause_no: str
    title: str = ""


class RelationIn(BaseModel):
    from_version_id: int
    relation: Literal["replaces", "splits_into", "merges_into", "withdraws"]
    to_version_id: Optional[int] = None      # 版本登记内嵌时缺省为新版本
    effective_at: Optional[str] = None       # 缺省取新版本生效起点
    note: str = ""


class VersionIn(BaseModel):
    version_label: str
    stage: Literal["draft", "errata", "official"]
    content: str
    obligation: str = ""
    valid_from: str
    valid_to: Optional[str] = None
    recorded_at: Optional[str] = None        # 缺省为服务器当前时间
    relations: list[RelationIn] = []         # 与历史版本的继承关系


class RelationPostIn(RelationIn):
    to_version_id: Optional[int] = None      # 撤回时为空
    effective_at: Optional[str] = None
    recorded_at: Optional[str] = None        # 缺省为服务器当前时间


class ProductIn(BaseModel):
    part_number: str
    config: dict = {}


class SupplierDeclarationIn(BaseModel):
    part_number: str
    supplier: str
    content: str
    project_id: Optional[int] = None
    recorded_at: Optional[str] = None


class TestReportIn(BaseModel):
    report_no: str
    part_number: str
    conclusion: Literal["pass", "fail", "conditional"]
    project_id: Optional[int] = None
    recorded_at: Optional[str] = None


class DeviationApprovalIn(BaseModel):
    deviation_no: str
    part_number: str
    scope: str
    approved_by: str
    expires_at: Optional[str] = None
    project_id: Optional[int] = None
    recorded_at: Optional[str] = None


class CustomerCommitmentIn(BaseModel):
    commitment_no: str
    customer: str
    part_number: str
    content: str
    project_id: Optional[int] = None
    promised_at: Optional[str] = None


class MappingIn(BaseModel):
    clause_version_id: int
    target_type: Literal["product_config", "supplier_declaration",
                         "test_conclusion", "deviation_approval"]
    target_id: int
    disposition: Literal["compliant", "deviation", "not_applicable", "pending"] = "pending"
    basis_version_id: Optional[int] = None   # 缺省等于 clause_version_id
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    note: str = ""
    allow_stale: bool = False                # 显式接受基于旧版本的决定


class MappingCloseIn(BaseModel):
    reason: str = ""
    closed_by: Optional[str] = None
    closed_at: Optional[str] = None


class CarryIn(BaseModel):
    clause_version_id: int                   # 结转到的新条款版本
    disposition: Optional[Literal["compliant", "deviation", "not_applicable", "pending"]] = None
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    note: str = ""
    close_old: bool = True
    allow_stale: bool = False


class SnapshotIn(BaseModel):
    part_number: str
    project_id: int
    standard_id: int
    signed_by: Optional[str] = None
    signed_at: Optional[str] = None


class TaskCloseIn(BaseModel):
    status: Literal["resolved", "dismissed"] = "resolved"
    resolution: str = ""
    closed_by: Optional[str] = None
    closed_at: Optional[str] = None


class ObjectionIn(BaseModel):
    mapping_id: Optional[int] = None
    snapshot_id: Optional[int] = None
    content: str
    raised_by: Optional[str] = None
    raised_at: Optional[str] = None


class ObjectionResolveIn(BaseModel):
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None


class AttachmentIn(BaseModel):
    project_id: int
    name: str
    content: str
    confidential: bool = True


class GrantIn(BaseModel):
    project_id: int
    granted_by: Optional[str] = None


# ---------- 公共辅助 ----------

def get_db(request: Request):
    db = connect(request.app.state.db_path)
    try:
        yield db
    finally:
        db.close()


def actor(request: Request, explicit: Optional[str] = None) -> str:
    return explicit or request.headers.get("x-user-name") or "system"


def must_get(db, table, row_id, label="记录"):
    row = domain.get_row(db, table, row_id)
    if row is None:
        raise HTTPException(404, f"{label} #{row_id} 不存在")
    return row


def ts_or_400(value: str) -> str:
    try:
        return domain.parse_ts(value)
    except (ValueError, AttributeError):
        raise HTTPException(
            400, f"时间格式无效：{value!r}，请使用 ISO 8601（如 2026-03-01 或 2026-03-01T08:00:00Z）")


def opt_ts(value: Optional[str]) -> Optional[str]:
    return ts_or_400(value) if value else None


def insert_or_409(db, sql, params, message):
    try:
        cur = db.execute(sql, params)
    except sqlite3.IntegrityError:
        raise HTTPException(409, message)
    return cur.lastrowid


def version_out(db, version):
    clause = domain.get_row(db, "clauses", version["clause_id"])
    return {
        **dict(version),
        "clause_no": clause["clause_no"] if clause else None,
        "standard_id": clause["standard_id"] if clause else None,
    }


def mapping_out(db, mapping, knowledge):
    version = domain.get_row(db, "clause_versions", mapping["clause_version_id"])
    clause = domain.get_row(db, "clauses", version["clause_id"])
    stale, reason, head = domain.mapping_stale(db, mapping, knowledge)
    return {
        **dict(mapping),
        "clause_no": clause["clause_no"],
        "version_label": version["version_label"],
        "target": domain.target_summary(db, mapping["target_type"], mapping["target_id"]),
        "stale": stale,
        "stale_reason": reason,
        "head_version_id": head["id"] if head else None,
    }


def check_basis(db, clause_id, basis_id, decided_at, now):
    """评议写入时的旧版本检测：以当前已获知（now）的版本谱系为准。

    撤回与新版登记一旦发生，任何新提交的决定都必须面对；而登记之前已留痕的
    决定由 fanout 的 late_correction / clause_change 任务负责标记。
    返回 (error_detail 或 None, head)，error_detail 用于 409 响应。
    """
    basis = domain.get_row(db, "clause_versions", basis_id)
    if basis is None:
        raise HTTPException(404, f"依据版本 #{basis_id} 不存在")
    if basis["clause_id"] != clause_id:
        raise HTTPException(400, "依据版本与目标条款版本不属于同一条款")
    if basis["recorded_at"] > decided_at:
        raise HTTPException(400, "决定时间早于依据版本的登记时间，请检查 decided_at")
    if basis["stage"] == "draft":
        return None, None  # 草案上的前瞻映射不做旧版本拦截
    head = domain.head_version(db, clause_id, now)
    if head is None:
        return None, None
    if domain.is_withdrawn(db, head["id"], now):
        return {
            "error": "clause_withdrawn",
            "message": f"条款最新版本 {head['version_label']}（#{head['id']}）已撤回，"
                       f"请确认义务是否仍然适用；确需记录请置 allow_stale=true",
            "head_version_id": head["id"],
        }, head
    if head["id"] != basis_id:
        return {
            "error": "stale_basis",
            "message": f"决定基于旧版本 #{basis_id}，当前已存在更新版本 "
                       f"{head['version_label']}（#{head['id']}）；"
                       f"请基于新版本复核，或显式置 allow_stale=true 留痕",
            "head_version_id": head["id"],
            "head_version_label": head["version_label"],
        }, head
    return None, head


def snapshot_items_out(db, snapshot_id):
    rows = db.execute(
        "SELECT * FROM release_snapshot_items WHERE snapshot_id = ? ORDER BY id",
        (snapshot_id,),
    ).fetchall()
    items = []
    for row in rows:
        clause = domain.get_row(db, "clauses", row["clause_id"])
        version = domain.get_row(db, "clause_versions", row["clause_version_id"])
        mapping = (domain.get_row(db, "obligation_mappings", row["mapping_id"])
                   if row["mapping_id"] else None)
        items.append({
            **dict(row),
            "clause_no": clause["clause_no"] if clause else None,
            "version_label": version["version_label"] if version else None,
            "stage": version["stage"] if version else None,
            "decided_by": mapping["decided_by"] if mapping else None,
            "decided_at": mapping["decided_at"] if mapping else None,
        })
    return items


def current_user(request: Request, db):
    name = request.headers.get("x-user-name")
    if not name:
        raise HTTPException(401, "缺少 X-User-Name 请求头")
    user = db.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
    if user is None:
        raise HTTPException(401, f"用户 {name!r} 未登记")
    return user


def is_member(db, user_id, project_id):
    return db.execute(
        "SELECT 1 FROM project_members WHERE user_id = ? AND project_id = ?",
        (user_id, project_id),
    ).fetchone() is not None


def can_access_attachment(db, user, attachment):
    if not attachment["confidential"]:
        return True
    if is_member(db, user["id"], attachment["project_id"]):
        return True
    return db.execute(
        """
        SELECT 1 FROM attachment_grants g
        JOIN project_members m ON m.project_id = g.project_id
        WHERE g.attachment_id = ? AND m.user_id = ?
        """,
        (attachment["id"], user["id"]),
    ).fetchone() is not None


# ---------- 健康检查 ----------

@router.get("/health")
def health(db=Depends(get_db)):
    db.execute("SELECT 1")
    return {"status": "ok"}


# ---------- 用户 / 项目 / 授权 ----------

@router.post("/users", status_code=201)
def create_user(payload: UserIn, db=Depends(get_db)):
    row_id = insert_or_409(
        db, "INSERT INTO users (name, role) VALUES (?, ?)",
        (payload.name, payload.role), f"用户 {payload.name!r} 已存在")
    db.commit()
    return dict(domain.get_row(db, "users", row_id))


@router.post("/projects", status_code=201)
def create_project(payload: ProjectIn, db=Depends(get_db)):
    row_id = insert_or_409(
        db, "INSERT INTO projects (code, name) VALUES (?, ?)",
        (payload.code, payload.name), f"项目 {payload.code!r} 已存在")
    db.commit()
    return dict(domain.get_row(db, "projects", row_id))


@router.post("/projects/{project_id}/members", status_code=201)
def add_member(project_id: int, payload: MemberIn, db=Depends(get_db)):
    must_get(db, "projects", project_id, "项目")
    user = db.execute("SELECT * FROM users WHERE name = ?", (payload.user,)).fetchone()
    if user is None:
        raise HTTPException(404, f"用户 {payload.user!r} 未登记")
    db.execute(
        "INSERT OR REPLACE INTO project_members (project_id, user_id, role) VALUES (?, ?, ?)",
        (project_id, user["id"], payload.role))
    db.commit()
    return {"project_id": project_id, "user": payload.user, "role": payload.role}


# ---------- 标准 / 条款 / 版本谱系 ----------

@router.post("/standards", status_code=201)
def create_standard(payload: StandardIn, db=Depends(get_db)):
    row_id = insert_or_409(
        db, "INSERT INTO standards (code, title) VALUES (?, ?)",
        (payload.code, payload.title), f"标准 {payload.code!r} 已存在")
    db.commit()
    return dict(domain.get_row(db, "standards", row_id))


@router.get("/standards")
def list_standards(db=Depends(get_db)):
    return {"standards": [dict(r) for r in db.execute(
        "SELECT * FROM standards ORDER BY code").fetchall()]}


@router.post("/standards/{standard_id}/clauses", status_code=201)
def create_clause(standard_id: int, payload: ClauseIn, db=Depends(get_db)):
    must_get(db, "standards", standard_id, "标准")
    row_id = insert_or_409(
        db, "INSERT INTO clauses (standard_id, clause_no, title) VALUES (?, ?, ?)",
        (standard_id, payload.clause_no, payload.title),
        f"条款 {payload.clause_no!r} 在该标准下已存在")
    db.commit()
    return dict(domain.get_row(db, "clauses", row_id))


@router.get("/standards/{standard_id}/clauses")
def list_clauses(standard_id: int, db=Depends(get_db)):
    must_get(db, "standards", standard_id, "标准")
    now = domain.now_iso()
    clauses = []
    for clause in db.execute(
            "SELECT * FROM clauses WHERE standard_id = ? ORDER BY clause_no",
            (standard_id,)).fetchall():
        head = domain.head_version(db, clause["id"], now)
        clauses.append({
            **dict(clause),
            "head_version_id": head["id"] if head else None,
            "head_version_label": head["version_label"] if head else None,
        })
    return {"clauses": clauses}


@router.post("/clauses/{clause_id}/versions", status_code=201)
def create_version(clause_id: int, payload: VersionIn, request: Request, db=Depends(get_db)):
    """登记条款新版本，可同时声明与历史版本的替代/拆分/合并关系。

    登记后自动扇出复核任务：影响开放中的义务映射与已签署的放行快照；
    生效区间追溯至决定/签署之前的，标记为迟到的更正。
    """
    clause = must_get(db, "clauses", clause_id, "条款")
    now = domain.now_iso()
    recorded_at = opt_ts(payload.recorded_at) or now
    valid_from = ts_or_400(payload.valid_from)
    valid_to = opt_ts(payload.valid_to)
    if valid_to is not None and valid_to <= valid_from:
        raise HTTPException(400, "valid_to 必须晚于 valid_from")
    recorded_by = actor(request)
    try:
        version_id = db.execute(
            """
            INSERT INTO clause_versions
              (clause_id, version_label, stage, content, obligation,
               valid_from, valid_to, recorded_at, recorded_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (clause_id, payload.version_label, payload.stage, payload.content,
             payload.obligation, valid_from, valid_to, recorded_at, recorded_by),
        ).lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"条款 {clause['clause_no']!r} 下版本 {payload.version_label!r} 已存在")

    relations = []
    for rel in payload.relations:
        if rel.relation == "withdraws":
            db.rollback()
            raise HTTPException(400, "撤回没有目标版本，请使用 POST /relations 单独登记")
        must_get(db, "clause_versions", rel.from_version_id, "历史版本")
        effective_at = opt_ts(rel.effective_at) or valid_from
        rel_id = db.execute(
            """
            INSERT INTO clause_relations
              (from_version_id, to_version_id, relation, effective_at, note,
               recorded_at, recorded_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (rel.from_version_id, version_id, rel.relation, effective_at, rel.note,
             recorded_at, recorded_by),
        ).lastrowid
        relations.append(dict(domain.get_row(db, "clause_relations", rel_id)))

    prev_ids = [rel.from_version_id for rel in payload.relations]
    if not prev_ids:
        prev_ids = [v["id"] for v in domain.applicable_versions(db, clause_id,
                                                                recorded_at, recorded_at)
                    if v["id"] != version_id]
    # 复核任务随变化的获知时间（recorded_at）开立，保证历史重现时时间轴一致
    tasks = domain.fanout_tasks(
        db, prev_ids, valid_from, recorded_at, "clause_change",
        f"{clause['clause_no']} {payload.version_label}", change_version_id=version_id)
    db.commit()
    return {
        "version": version_out(db, domain.get_row(db, "clause_versions", version_id)),
        "relations": relations,
        "tasks": tasks,
    }


@router.get("/clauses/{clause_id}/versions")
def list_versions(clause_id: int, db=Depends(get_db)):
    must_get(db, "clauses", clause_id, "条款")
    versions = db.execute(
        "SELECT * FROM clause_versions WHERE clause_id = ? "
        "ORDER BY valid_from, recorded_at, id", (clause_id,)).fetchall()
    return {"versions": [version_out(db, v) for v in versions]}


@router.get("/clauses/{clause_id}/lineage")
def clause_lineage(clause_id: int, db=Depends(get_db)):
    """条款谱系：全部版本与继承关系（替代/拆分/合并/撤回）。"""
    clause = must_get(db, "clauses", clause_id, "条款")
    versions = db.execute(
        "SELECT * FROM clause_versions WHERE clause_id = ? "
        "ORDER BY valid_from, recorded_at, id", (clause_id,)).fetchall()
    relations = db.execute(
        """
        SELECT r.* FROM clause_relations r
        WHERE r.from_version_id IN (SELECT id FROM clause_versions WHERE clause_id = ?)
           OR r.to_version_id IN (SELECT id FROM clause_versions WHERE clause_id = ?)
        ORDER BY r.id
        """,
        (clause_id, clause_id)).fetchall()
    return {
        "clause": dict(clause),
        "versions": [version_out(db, v) for v in versions],
        "relations": [{**dict(r), "relation_label": domain.RELATION_LABELS[r["relation"]]}
                      for r in relations],
    }


@router.get("/clauses/{clause_id}/applicable")
def clause_applicable(clause_id: int, at: Optional[str] = None,
                      knowledge_at: Optional[str] = None, db=Depends(get_db)):
    """某生效时点适用的条款版本；knowledge_at 控制“以何时所获知为准”。"""
    must_get(db, "clauses", clause_id, "条款")
    at_ts = opt_ts(at) or domain.now_iso()
    knowledge_ts = opt_ts(knowledge_at) or at_ts
    apps = domain.applicable_versions(db, clause_id, at_ts, knowledge_ts)
    return {
        "clause_id": clause_id,
        "at": at_ts,
        "knowledge_at": knowledge_ts,
        "current": version_out(db, apps[0]) if apps else None,
        "applicable": [version_out(db, v) for v in apps],
    }


@router.post("/relations", status_code=201)
def create_relation(payload: RelationPostIn, request: Request, db=Depends(get_db)):
    """单独登记继承关系，用于撤回或事后补登的替代/拆分/合并。"""
    now = domain.now_iso()
    from_version = must_get(db, "clause_versions", payload.from_version_id, "历史版本")
    to_version = None
    if payload.relation == "withdraws":
        if payload.to_version_id is not None:
            raise HTTPException(400, "撤回关系不应携带目标版本")
    else:
        if payload.to_version_id is None:
            raise HTTPException(400, "替代/拆分/合并必须给出目标版本 to_version_id")
        to_version = must_get(db, "clause_versions", payload.to_version_id, "目标版本")
    if payload.effective_at:
        effective_at = ts_or_400(payload.effective_at)
    elif to_version is not None:
        effective_at = to_version["valid_from"]
    else:
        effective_at = now
    recorded_at = opt_ts(payload.recorded_at) or now
    rel_id = db.execute(
        """
        INSERT INTO clause_relations
          (from_version_id, to_version_id, relation, effective_at, note,
           recorded_at, recorded_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (payload.from_version_id, payload.to_version_id, payload.relation,
         effective_at, payload.note, recorded_at, actor(request)),
    ).lastrowid
    default_kind = "withdrawal" if payload.relation == "withdraws" else "clause_change"
    label = f"{domain.RELATION_LABELS[payload.relation]}（自 {effective_at}）"
    change_subject = payload.to_version_id or payload.from_version_id
    tasks = domain.fanout_tasks(
        db, [payload.from_version_id], effective_at, recorded_at, default_kind, label,
        change_version_id=change_subject, relation_id=rel_id)
    db.commit()
    return {
        "relation": dict(domain.get_row(db, "clause_relations", rel_id)),
        "tasks": tasks,
    }


# ---------- 义务映射目标 ----------

@router.post("/products", status_code=201)
def create_product(payload: ProductIn, db=Depends(get_db)):
    row_id = insert_or_409(
        db, "INSERT INTO products (part_number, config) VALUES (?, ?)",
        (payload.part_number, json.dumps(payload.config, ensure_ascii=False)),
        f"料号 {payload.part_number!r} 已存在")
    db.commit()
    row = dict(domain.get_row(db, "products", row_id))
    row["config"] = json.loads(row["config"])
    return row


@router.get("/products")
def list_products(db=Depends(get_db)):
    products = []
    for row in db.execute("SELECT * FROM products ORDER BY part_number").fetchall():
        product = dict(row)
        product["config"] = json.loads(product["config"])
        products.append(product)
    return {"products": products}


def _insert_target(db, table, fields, values, dup_message):
    row_id = insert_or_409(
        db, f"INSERT INTO {table} ({', '.join(fields)}) "
            f"VALUES ({', '.join('?' for _ in fields)})",
        values, dup_message)
    db.commit()
    return dict(domain.get_row(db, table, row_id))


@router.post("/supplier-declarations", status_code=201)
def create_supplier_declaration(payload: SupplierDeclarationIn, db=Depends(get_db)):
    if payload.project_id is not None:
        must_get(db, "projects", payload.project_id, "项目")
    return _insert_target(
        db, "supplier_declarations",
        ["part_number", "supplier", "content", "project_id", "recorded_at"],
        [payload.part_number, payload.supplier, payload.content, payload.project_id,
         opt_ts(payload.recorded_at) or domain.now_iso()],
        "供应商声明登记失败：唯一性冲突")


@router.get("/supplier-declarations")
def list_supplier_declarations(part_number: Optional[str] = None, db=Depends(get_db)):
    if part_number:
        rows = db.execute(
            "SELECT * FROM supplier_declarations WHERE part_number = ? ORDER BY id",
            (part_number,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM supplier_declarations ORDER BY id").fetchall()
    return {"supplier_declarations": [dict(r) for r in rows]}


@router.post("/test-reports", status_code=201)
def create_test_report(payload: TestReportIn, db=Depends(get_db)):
    if payload.project_id is not None:
        must_get(db, "projects", payload.project_id, "项目")
    return _insert_target(
        db, "test_reports",
        ["report_no", "part_number", "conclusion", "project_id", "recorded_at"],
        [payload.report_no, payload.part_number, payload.conclusion, payload.project_id,
         opt_ts(payload.recorded_at) or domain.now_iso()],
        f"试验报告 {payload.report_no!r} 已存在")


@router.get("/test-reports")
def list_test_reports(part_number: Optional[str] = None, db=Depends(get_db)):
    if part_number:
        rows = db.execute(
            "SELECT * FROM test_reports WHERE part_number = ? ORDER BY id",
            (part_number,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM test_reports ORDER BY id").fetchall()
    return {"test_reports": [dict(r) for r in rows]}


@router.post("/deviation-approvals", status_code=201)
def create_deviation_approval(payload: DeviationApprovalIn, db=Depends(get_db)):
    if payload.project_id is not None:
        must_get(db, "projects", payload.project_id, "项目")
    return _insert_target(
        db, "deviation_approvals",
        ["deviation_no", "part_number", "scope", "approved_by", "expires_at",
         "project_id", "recorded_at"],
        [payload.deviation_no, payload.part_number, payload.scope, payload.approved_by,
         opt_ts(payload.expires_at), payload.project_id,
         opt_ts(payload.recorded_at) or domain.now_iso()],
        f"偏离批准 {payload.deviation_no!r} 已存在")


@router.get("/deviation-approvals")
def list_deviation_approvals(part_number: Optional[str] = None, db=Depends(get_db)):
    if part_number:
        rows = db.execute(
            "SELECT * FROM deviation_approvals WHERE part_number = ? ORDER BY id",
            (part_number,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM deviation_approvals ORDER BY id").fetchall()
    return {"deviation_approvals": [dict(r) for r in rows]}


@router.post("/customer-commitments", status_code=201)
def create_customer_commitment(payload: CustomerCommitmentIn, db=Depends(get_db)):
    if payload.project_id is not None:
        must_get(db, "projects", payload.project_id, "项目")
    return _insert_target(
        db, "customer_commitments",
        ["commitment_no", "customer", "part_number", "content", "project_id", "promised_at"],
        [payload.commitment_no, payload.customer, payload.part_number, payload.content,
         payload.project_id, opt_ts(payload.promised_at) or domain.now_iso()],
        f"客户承诺 {payload.commitment_no!r} 已存在")


@router.get("/customer-commitments")
def list_customer_commitments(part_number: Optional[str] = None, db=Depends(get_db)):
    if part_number:
        rows = db.execute(
            "SELECT * FROM customer_commitments WHERE part_number = ? ORDER BY id",
            (part_number,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM customer_commitments ORDER BY id").fetchall()
    return {"customer_commitments": [dict(r) for r in rows]}


# ---------- 义务映射 ----------

@router.post("/mappings", status_code=201)
def create_mapping(payload: MappingIn, request: Request, db=Depends(get_db)):
    """登记义务映射。多人同时评议时，基于旧版本的决定会被拒绝（409），
    除非显式 allow_stale=true —— 此时决定留痕并生成 stale_decision 复核任务。
    """
    now = domain.now_iso()
    version = must_get(db, "clause_versions", payload.clause_version_id, "条款版本")
    basis_id = payload.basis_version_id or version["id"]
    decided_at = opt_ts(payload.decided_at) or now
    decided_by = actor(request, payload.decided_by)
    if domain.target_summary(db, payload.target_type, payload.target_id) is None:
        raise HTTPException(404, f"映射目标 {payload.target_type} #{payload.target_id} 不存在")

    error, head = check_basis(db, version["clause_id"], basis_id, decided_at, now)
    if error and not payload.allow_stale:
        raise HTTPException(409, error)

    mapping_id = db.execute(
        """
        INSERT INTO obligation_mappings
          (clause_version_id, target_type, target_id, disposition,
           basis_version_id, decided_by, decided_at, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (version["id"], payload.target_type, payload.target_id, payload.disposition,
         basis_id, decided_by, decided_at, payload.note),
    ).lastrowid

    tasks = []
    if error:
        reason = (f"映射 #{mapping_id} 基于旧版本 #{basis_id} 决定"
                  if error["error"] == "stale_basis"
                  else f"映射 #{mapping_id} 登记时条款已撤回")
        task = domain.insert_task(
            db, "stale_decision", f"{reason}（{error['message']}）", now,
            clause_version_id=head["id"] if head else None, mapping_id=mapping_id)
        if task:
            tasks.append(task)
    db.commit()
    mapping = domain.get_row(db, "obligation_mappings", mapping_id)
    return {
        "mapping": mapping_out(db, mapping, now),
        "stale": error is not None,
        "head_version": version_out(db, head) if head else None,
        "tasks": tasks,
    }


@router.get("/mappings")
def list_mappings(stale: Optional[bool] = None, open_only: bool = False,
                  part_number: Optional[str] = None, db=Depends(get_db)):
    now = domain.now_iso()
    rows = db.execute("SELECT * FROM obligation_mappings ORDER BY id").fetchall()
    mappings = []
    for row in rows:
        if open_only and row["closed_at"] is not None:
            continue
        out = mapping_out(db, row, now)
        if stale is not None and out["stale"] != stale:
            continue
        if part_number and (out["target"] or {}).get("part_number") != part_number:
            continue
        mappings.append(out)
    return {"mappings": mappings, "knowledge_at": now}


@router.post("/mappings/{mapping_id}/close")
def close_mapping(mapping_id: int, payload: MappingCloseIn, request: Request,
                  db=Depends(get_db)):
    mapping = must_get(db, "obligation_mappings", mapping_id, "映射")
    if mapping["closed_at"] is not None:
        raise HTTPException(409, f"映射 #{mapping_id} 已关闭")
    closed_at = opt_ts(payload.closed_at) or domain.now_iso()
    closed_by = actor(request, payload.closed_by)
    db.execute(
        "UPDATE obligation_mappings SET closed_at = ?, closed_reason = ? WHERE id = ?",
        (closed_at, payload.reason or f"由 {closed_by} 关闭", mapping_id))
    resolved = domain.resolve_tasks_for_mapping(
        db, mapping_id, f"映射已关闭：{payload.reason or '未说明'}", closed_by, closed_at)
    db.commit()
    return {
        "mapping": mapping_out(db, domain.get_row(db, "obligation_mappings", mapping_id),
                               domain.now_iso()),
        "resolved_tasks": resolved,
    }


@router.post("/mappings/{mapping_id}/carry", status_code=201)
def carry_mapping(mapping_id: int, payload: CarryIn, request: Request, db=Depends(get_db)):
    """把映射结转到新的条款版本：原映射保留并关闭，新映射记录来源链，
    该映射上的未决复核任务随结转一并了结。"""
    now = domain.now_iso()
    old = must_get(db, "obligation_mappings", mapping_id, "映射")
    if old["closed_at"] is not None:
        raise HTTPException(409, f"映射 #{mapping_id} 已关闭，不能结转")
    version = must_get(db, "clause_versions", payload.clause_version_id, "条款版本")
    decided_at = opt_ts(payload.decided_at) or now
    decided_by = actor(request, payload.decided_by)

    error, head = check_basis(db, version["clause_id"], version["id"], decided_at, now)
    if error and not payload.allow_stale:
        raise HTTPException(409, error)

    new_id = db.execute(
        """
        INSERT INTO obligation_mappings
          (clause_version_id, target_type, target_id, disposition,
           basis_version_id, decided_by, decided_at, note, carried_from_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (version["id"], old["target_type"], old["target_id"],
         payload.disposition or old["disposition"], version["id"], decided_by, decided_at,
         payload.note, mapping_id),
    ).lastrowid
    resolved = 0
    if payload.close_old:
        db.execute(
            "UPDATE obligation_mappings SET closed_at = ?, closed_reason = ? WHERE id = ?",
            (now, f"结转至映射 #{new_id}", mapping_id))
        resolved = domain.resolve_tasks_for_mapping(
            db, mapping_id, f"已结转至映射 #{new_id}", decided_by, now)
    tasks = []
    if error:
        task = domain.insert_task(
            db, "stale_decision", f"结转映射 #{new_id} 时条款已撤回", now,
            clause_version_id=head["id"] if head else None, mapping_id=new_id)
        if task:
            tasks.append(task)
    db.commit()
    return {
        "mapping": mapping_out(db, domain.get_row(db, "obligation_mappings", new_id), now),
        "carried_from_id": mapping_id,
        "resolved_tasks": resolved,
        "stale": error is not None,
        "tasks": tasks,
    }


# ---------- 放行快照 ----------

@router.post("/snapshots", status_code=201)
def sign_snapshot(payload: SnapshotIn, request: Request, db=Depends(get_db)):
    """签署放行快照：冻结签署时点适用的条款版本与该料号的开放映射，
    内容哈希存证。签署后不提供任何修改入口，迟到的更正只生成复核任务。
    """
    must_get(db, "projects", payload.project_id, "项目")
    must_get(db, "standards", payload.standard_id, "标准")
    now = domain.now_iso()
    signed_at = opt_ts(payload.signed_at) or now
    signed_by = actor(request, payload.signed_by)
    header = {
        "part_number": payload.part_number,
        "project_id": payload.project_id,
        "standard_id": payload.standard_id,
        "signed_by": signed_by,
        "signed_at": signed_at,
    }
    items = domain.build_snapshot_items(
        db, payload.standard_id, payload.part_number, at=signed_at, knowledge=now)
    digest = domain.snapshot_digest(header, items)
    snapshot_id = db.execute(
        """
        INSERT INTO release_snapshots
          (part_number, project_id, standard_id, signed_by, signed_at, digest)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (payload.part_number, payload.project_id, payload.standard_id,
         signed_by, signed_at, digest),
    ).lastrowid
    for item in items:
        db.execute(
            """
            INSERT INTO release_snapshot_items
              (snapshot_id, clause_id, clause_version_id, mapping_id, disposition)
            VALUES (?, ?, ?, ?, ?)
            """,
            (snapshot_id, item["clause_id"], item["clause_version_id"],
             item["mapping_id"], item["disposition"]),
        )
    db.commit()
    return {
        "snapshot": dict(domain.get_row(db, "release_snapshots", snapshot_id)),
        "items": snapshot_items_out(db, snapshot_id),
    }


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: int, db=Depends(get_db)):
    snapshot = must_get(db, "release_snapshots", snapshot_id, "放行快照")
    objections = db.execute(
        "SELECT * FROM objections WHERE snapshot_id = ? AND status = 'open' ORDER BY id",
        (snapshot_id,)).fetchall()
    return {
        "snapshot": dict(snapshot),
        "items": snapshot_items_out(db, snapshot_id),
        "open_objections": [dict(o) for o in objections],
    }


@router.get("/snapshots/{snapshot_id}/verify")
def verify_snapshot(snapshot_id: int, db=Depends(get_db)):
    """重算内容哈希，证明签署内容未被改写。"""
    snapshot = must_get(db, "release_snapshots", snapshot_id, "放行快照")
    header = {
        "part_number": snapshot["part_number"],
        "project_id": snapshot["project_id"],
        "standard_id": snapshot["standard_id"],
        "signed_by": snapshot["signed_by"],
        "signed_at": snapshot["signed_at"],
    }
    items = [dict(r) for r in db.execute(
        "SELECT clause_id, clause_version_id, mapping_id, disposition "
        "FROM release_snapshot_items WHERE snapshot_id = ? ORDER BY id",
        (snapshot_id,)).fetchall()]
    recomputed = domain.snapshot_digest(header, items)
    return {
        "snapshot_id": snapshot_id,
        "ok": recomputed == snapshot["digest"],
        "digest": snapshot["digest"],
        "recomputed": recomputed,
    }


@router.get("/snapshots/{snapshot_id}/diff")
def diff_snapshot(snapshot_id: int, db=Depends(get_db)):
    """快照与当前认知的差异：
    - late_corrections：以当前所获知重放签署时点，暴露迟到的更正；
    - subsequent_changes：以当前时点重算，暴露签署之后的后续变化。
    """
    snapshot = must_get(db, "release_snapshots", snapshot_id, "放行快照")
    now = domain.now_iso()
    frozen = [dict(r) for r in db.execute(
        "SELECT clause_id, clause_version_id, mapping_id, disposition "
        "FROM release_snapshot_items WHERE snapshot_id = ? ORDER BY id",
        (snapshot_id,)).fetchall()]
    at_signing = domain.build_snapshot_items(
        db, snapshot["standard_id"], snapshot["part_number"],
        at=snapshot["signed_at"], knowledge=now)
    current = domain.build_snapshot_items(
        db, snapshot["standard_id"], snapshot["part_number"], at=now, knowledge=now)
    return {
        "snapshot_id": snapshot_id,
        "signed_at": snapshot["signed_at"],
        "late_corrections": domain.snapshot_delta(db, frozen, at_signing),
        "subsequent_changes": domain.snapshot_delta(db, frozen, current),
    }


# ---------- 复核任务 ----------

@router.get("/review-tasks")
def list_review_tasks(status: Optional[str] = None, kind: Optional[str] = None,
                      db=Depends(get_db)):
    sql = "SELECT * FROM review_tasks"
    conditions, params = [], []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if kind:
        conditions.append("kind = ?")
        params.append(kind)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY id"
    return {"tasks": [dict(r) for r in db.execute(sql, params).fetchall()]}


@router.post("/review-tasks/{task_id}/close")
def close_review_task(task_id: int, payload: TaskCloseIn, request: Request,
                      db=Depends(get_db)):
    task = must_get(db, "review_tasks", task_id, "复核任务")
    if task["status"] != "open":
        raise HTTPException(409, f"复核任务 #{task_id} 已了结")
    closed_at = opt_ts(payload.closed_at) or domain.now_iso()
    db.execute(
        "UPDATE review_tasks SET status = ?, resolution = ?, closed_by = ?, closed_at = ? "
        "WHERE id = ?",
        (payload.status, payload.resolution, actor(request, payload.closed_by),
         closed_at, task_id))
    db.commit()
    return {"task": dict(domain.get_row(db, "review_tasks", task_id))}


# ---------- 异议 ----------

@router.post("/objections", status_code=201)
def create_objection(payload: ObjectionIn, request: Request, db=Depends(get_db)):
    if (payload.mapping_id is None) == (payload.snapshot_id is None):
        raise HTTPException(400, "异议必须且只能挂接映射或快照之一")
    if payload.mapping_id is not None:
        must_get(db, "obligation_mappings", payload.mapping_id, "映射")
    if payload.snapshot_id is not None:
        must_get(db, "release_snapshots", payload.snapshot_id, "放行快照")
    row_id = db.execute(
        """
        INSERT INTO objections (mapping_id, snapshot_id, raised_by, raised_at, content)
        VALUES (?, ?, ?, ?, ?)
        """,
        (payload.mapping_id, payload.snapshot_id, actor(request, payload.raised_by),
         opt_ts(payload.raised_at) or domain.now_iso(), payload.content),
    ).lastrowid
    db.commit()
    return {"objection": dict(domain.get_row(db, "objections", row_id))}


@router.post("/objections/{objection_id}/resolve")
def resolve_objection(objection_id: int, payload: ObjectionResolveIn, request: Request,
                      db=Depends(get_db)):
    objection = must_get(db, "objections", objection_id, "异议")
    if objection["status"] != "open":
        raise HTTPException(409, f"异议 #{objection_id} 已了结")
    db.execute(
        "UPDATE objections SET status = 'resolved', resolved_by = ?, resolved_at = ? "
        "WHERE id = ?",
        (actor(request, payload.resolved_by),
         opt_ts(payload.resolved_at) or domain.now_iso(), objection_id))
    db.commit()
    return {"objection": dict(domain.get_row(db, "objections", objection_id))}


@router.get("/objections")
def list_objections(status: Optional[str] = None, db=Depends(get_db)):
    if status:
        rows = db.execute(
            "SELECT * FROM objections WHERE status = ? ORDER BY id", (status,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM objections ORDER BY id").fetchall()
    return {"objections": [dict(r) for r in rows]}


# ---------- 保密附件 ----------

@router.post("/attachments", status_code=201)
def upload_attachment(payload: AttachmentIn, request: Request, db=Depends(get_db)):
    user = current_user(request, db)
    must_get(db, "projects", payload.project_id, "项目")
    if not is_member(db, user["id"], payload.project_id):
        raise HTTPException(403, "仅项目成员可上传附件")
    row_id = db.execute(
        """
        INSERT INTO attachments (project_id, name, content, confidential,
                                 uploaded_by, uploaded_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (payload.project_id, payload.name, payload.content,
         1 if payload.confidential else 0, user["name"], domain.now_iso()),
    ).lastrowid
    db.commit()
    attachment = dict(domain.get_row(db, "attachments", row_id))
    attachment.pop("content")
    return {"attachment": attachment}


@router.get("/attachments/{attachment_id}")
def read_attachment(attachment_id: int, request: Request, db=Depends(get_db)):
    user = current_user(request, db)
    attachment = must_get(db, "attachments", attachment_id, "附件")
    if not can_access_attachment(db, user, attachment):
        raise HTTPException(403, "保密附件仅向获授权项目成员开放")
    result = dict(attachment)
    if is_member(db, user["id"], attachment["project_id"]):
        grants = db.execute(
            "SELECT project_id, granted_by, granted_at FROM attachment_grants "
            "WHERE attachment_id = ? ORDER BY project_id", (attachment_id,)).fetchall()
        result["granted_projects"] = [dict(g) for g in grants]
    return {"attachment": result}


@router.post("/attachments/{attachment_id}/grants", status_code=201)
def grant_attachment(attachment_id: int, payload: GrantIn, request: Request,
                     db=Depends(get_db)):
    user = current_user(request, db)
    attachment = must_get(db, "attachments", attachment_id, "附件")
    must_get(db, "projects", payload.project_id, "项目")
    if not is_member(db, user["id"], attachment["project_id"]):
        raise HTTPException(403, "仅归属项目的成员可授权附件")
    db.execute(
        "INSERT OR REPLACE INTO attachment_grants "
        "(attachment_id, project_id, granted_by, granted_at) VALUES (?, ?, ?, ?)",
        (attachment_id, payload.project_id, user["name"], domain.now_iso()))
    db.commit()
    return {"attachment_id": attachment_id, "project_id": payload.project_id,
            "granted_by": user["name"]}


@router.get("/projects/{project_id}/attachments")
def list_project_attachments(project_id: int, request: Request, db=Depends(get_db)):
    user = current_user(request, db)
    must_get(db, "projects", project_id, "项目")
    rows = db.execute(
        "SELECT * FROM attachments WHERE project_id = ? ORDER BY id",
        (project_id,)).fetchall()
    visible = []
    for row in rows:
        if can_access_attachment(db, user, row):
            item = dict(row)
            item.pop("content")
            visible.append(item)
    return {"attachments": visible}


# ---------- 影响追踪与历史重现 ----------

@router.get("/changes")
def list_changes(since: Optional[str] = None, limit: int = 50, db=Depends(get_db)):
    """标准变化 feed：按登记时间倒序的条款版本。"""
    params = []
    sql = """
        SELECT cv.*, c.clause_no, c.standard_id FROM clause_versions cv
        JOIN clauses c ON c.id = cv.clause_id
    """
    if since:
        sql += " WHERE cv.recorded_at >= ?"
        params.append(ts_or_400(since))
    sql += " ORDER BY cv.recorded_at DESC, cv.id DESC LIMIT ?"
    params.append(limit)
    return {"changes": [dict(r) for r in db.execute(sql, params).fetchall()]}


@router.get("/changes/{version_id}/impact")
def change_impact(version_id: int, db=Depends(get_db)):
    """从一次标准变化追到全部受影响对象与待复核事项。"""
    result = domain.impact(db, version_id)
    if result is None:
        raise HTTPException(404, f"条款版本 #{version_id} 不存在")
    return result


@router.get("/as-of")
def as_of(at: str, knowledge_at: Optional[str] = None,
          standard_id: Optional[int] = None, db=Depends(get_db)):
    """按历史时点重现：当时适用的条款、证据（义务映射）、未决异议与待复核事项。

    at 为生效时点；knowledge_at 为获知时点（缺省等于 at，即“当时所知”），
    将其设为现在可观察迟到的更正如何改变历史结论。
    """
    at_ts = ts_or_400(at)
    knowledge_ts = opt_ts(knowledge_at) or at_ts
    if standard_id is not None:
        must_get(db, "standards", standard_id, "标准")
    return domain.as_of(db, at_ts, knowledge_ts, standard_id)


# ---------- 应用装配 ----------

@asynccontextmanager
async def lifespan(app):
    migrate(app.state.db_path)
    yield


def create_app(db_path=None):
    app = FastAPI(title="半导体标准变更影响闭环", lifespan=lifespan)
    app.state.db_path = db_path or os.environ.get("APP_DB", "data/app.db")
    app.include_router(router)
    return app


app = create_app()
