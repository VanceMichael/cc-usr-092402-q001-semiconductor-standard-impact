"""半导体条款生效索引的 HTTP 接口。"""

import base64
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import db
import domain


@asynccontextmanager
async def lifespan(_app):
    path = db.db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect()
    try:
        db.migrate(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="半导体条款生效索引", lifespan=lifespan)


@app.exception_handler(domain.NotFound)
def handle_not_found(_request: Request, exc: domain.NotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(domain.Conflict)
def handle_conflict(_request: Request, exc: domain.Conflict):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(domain.Forbidden)
def handle_forbidden(_request: Request, exc: domain.Forbidden):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(sqlite3.IntegrityError)
def handle_integrity(_request: Request, exc: sqlite3.IntegrityError):
    return JSONResponse(status_code=409, content={"detail": f"记录冲突：{exc}"})


def get_conn():
    conn = db.connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ts(value: Optional[str]) -> Optional[str]:
    return db.parse_ts(value) if value else None


# ---------------------------------------------------------------- 请求模型

class ProjectIn(BaseModel):
    project_id: str
    name: str
    recorded_at: Optional[str] = None


class StandardIn(BaseModel):
    standard_id: str
    title: str
    recorded_at: Optional[str] = None


class ClauseIn(BaseModel):
    clause_no: str
    title: str
    recorded_at: Optional[str] = None


class EdgeIn(BaseModel):
    from_version_id: str
    relation: Literal["replaces", "corrects", "splits_into", "merges_into"]
    effective_at: Optional[str] = None


class ClauseVersionIn(BaseModel):
    stage: Literal["draft", "errata", "corrigendum", "official"]
    content: str
    valid_from: str
    valid_to: Optional[str] = None
    recorded_at: Optional[str] = None
    edges: list[EdgeIn] = []


class WithdrawIn(BaseModel):
    effective_at: str
    recorded_at: Optional[str] = None


class ObligationIn(BaseModel):
    clause_version_id: str
    statement: str
    recorded_at: Optional[str] = None


class ProductConfigIn(BaseModel):
    part_number: str
    product_line: str
    description: str = ""
    recorded_at: Optional[str] = None


class SupplierDeclarationIn(BaseModel):
    supplier: str
    part_number: str
    statement: str
    recorded_at: Optional[str] = None


class TestConclusionIn(BaseModel):
    report_no: str
    config_id: Optional[str] = None
    result: Literal["pass", "fail", "conditional"]
    summary: str = ""
    attachment_id: Optional[str] = None
    recorded_at: Optional[str] = None


class DeviationApprovalIn(BaseModel):
    config_id: Optional[str] = None
    approver: str
    rationale: str
    expires_at: Optional[str] = None
    attachment_id: Optional[str] = None
    recorded_at: Optional[str] = None


class LinkIn(BaseModel):
    target_kind: Literal["product_config", "supplier_declaration", "test_conclusion", "deviation_approval"]
    target_id: str
    recorded_at: Optional[str] = None


class RetractIn(BaseModel):
    retracted_at: Optional[str] = None


class SnapshotIn(BaseModel):
    config_id: str
    standard_id: str
    as_of: Optional[str] = None
    created_at: Optional[str] = None


class SignIn(BaseModel):
    signer: str
    signed_at: Optional[str] = None


class DecisionIn(BaseModel):
    reviewer: str
    clause_version_id: str
    verdict: Literal["accept", "reject", "abstain"]
    rationale: str = ""
    seen_epoch: Optional[int] = None
    recorded_at: Optional[str] = None


class ObjectionIn(BaseModel):
    subject_kind: Literal["clause_version", "obligation", "decision", "snapshot", "obligation_link"]
    subject_id: str
    raised_by: str
    detail: str
    recorded_at: Optional[str] = None


class ResolveIn(BaseModel):
    resolution: str = ""
    resolved_at: Optional[str] = None


class AttachmentIn(BaseModel):
    name: str
    content_b64: str
    media_type: str = "application/octet-stream"
    confidential: bool = False
    recorded_at: Optional[str] = None


class GrantIn(BaseModel):
    project_id: str
    recorded_at: Optional[str] = None


# ---------------------------------------------------------------- 基础

@app.get("/health")
def health():
    conn = db.connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/epoch")
def epoch(conn=Depends(get_conn)):
    return {"epoch": domain.current_epoch(conn)}


# ---------------------------------------------------------------- 主数据

@app.post("/projects")
def create_project(body: ProjectIn, conn=Depends(get_conn)):
    domain.create_project(conn, body.project_id, body.name, _ts(body.recorded_at))
    return {"project_id": body.project_id}


@app.post("/standards")
def create_standard(body: StandardIn, conn=Depends(get_conn)):
    domain.create_standard(conn, body.standard_id, body.title, _ts(body.recorded_at))
    return {"standard_id": body.standard_id}


@app.post("/standards/{standard_id}/clauses")
def create_clause(standard_id: str, body: ClauseIn, conn=Depends(get_conn)):
    clause_id = domain.create_clause(conn, standard_id, body.clause_no, body.title,
                                     _ts(body.recorded_at))
    return {"clause_id": clause_id}


@app.get("/standards/{standard_id}/changes")
def standard_changes(standard_id: str, conn=Depends(get_conn)):
    return domain.standard_changes(conn, standard_id)


# ---------------------------------------------------------------- 条款谱系

@app.post("/clauses/{clause_id}/versions")
def create_clause_version(clause_id: str, body: ClauseVersionIn, conn=Depends(get_conn)):
    edges = []
    for edge in body.edges:
        edges.append({
            "from_version_id": edge.from_version_id,
            "relation": edge.relation,
            "effective_at": _ts(edge.effective_at),
        })
    version_id, edge_ids = domain.register_clause_version(
        conn, clause_id, body.stage, body.content,
        db.parse_ts(body.valid_from), _ts(body.valid_to), edges, _ts(body.recorded_at),
    )
    return {"clause_version_id": version_id, "edge_ids": edge_ids,
            "epoch": domain.current_epoch(conn)}


@app.post("/clause-versions/{version_id}/withdraw")
def withdraw_clause_version(version_id: str, body: WithdrawIn, conn=Depends(get_conn)):
    edge_id = domain.register_withdrawal(conn, version_id, db.parse_ts(body.effective_at),
                                         _ts(body.recorded_at))
    return {"edge_id": edge_id, "epoch": domain.current_epoch(conn)}


@app.get("/clauses/{clause_id}/lineage")
def clause_lineage(clause_id: str, conn=Depends(get_conn)):
    return domain.lineage(conn, clause_id)


@app.get("/clauses/{clause_id}/applicable")
def clause_applicable(clause_id: str, at: str, conn=Depends(get_conn)):
    return {"clause_id": clause_id, "at": db.parse_ts(at),
            "versions": domain.applicable_versions(conn, clause_id, db.parse_ts(at))}


@app.get("/clause-versions/{version_id}/impact")
def clause_version_impact(version_id: str, conn=Depends(get_conn)):
    return domain.impact(conn, version_id)


# ---------------------------------------------------------------- 义务与映射

@app.post("/obligations")
def create_obligation(body: ObligationIn, conn=Depends(get_conn)):
    obligation_id = domain.create_obligation(conn, body.clause_version_id, body.statement,
                                             _ts(body.recorded_at))
    return {"obligation_id": obligation_id}


@app.post("/product-configs")
def create_product_config(body: ProductConfigIn, conn=Depends(get_conn)):
    config_id = domain.create_product_config(conn, body.part_number, body.product_line,
                                             body.description, _ts(body.recorded_at))
    return {"config_id": config_id}


@app.post("/supplier-declarations")
def create_supplier_declaration(body: SupplierDeclarationIn, conn=Depends(get_conn)):
    declaration_id = domain.create_supplier_declaration(conn, body.supplier, body.part_number,
                                                        body.statement, _ts(body.recorded_at))
    return {"declaration_id": declaration_id}


@app.post("/test-conclusions")
def create_test_conclusion(body: TestConclusionIn, conn=Depends(get_conn)):
    conclusion_id = domain.create_test_conclusion(conn, body.report_no, body.config_id,
                                                  body.result, body.summary, body.attachment_id,
                                                  _ts(body.recorded_at))
    return {"conclusion_id": conclusion_id}


@app.post("/deviation-approvals")
def create_deviation_approval(body: DeviationApprovalIn, conn=Depends(get_conn)):
    approval_id = domain.create_deviation_approval(conn, body.config_id, body.approver,
                                                   body.rationale, _ts(body.expires_at),
                                                   body.attachment_id, _ts(body.recorded_at))
    return {"approval_id": approval_id}


@app.post("/obligations/{obligation_id}/links")
def create_obligation_link(obligation_id: str, body: LinkIn, conn=Depends(get_conn)):
    link_id = domain.link_obligation(conn, obligation_id, body.target_kind, body.target_id,
                                     _ts(body.recorded_at))
    return {"link_id": link_id}


@app.post("/obligation-links/{link_id}/retract")
def retract_obligation_link(link_id: str, body: RetractIn, conn=Depends(get_conn)):
    retracted_at = domain.retract_link(conn, link_id, _ts(body.retracted_at))
    return {"link_id": link_id, "retracted_at": retracted_at}


# ---------------------------------------------------------------- 放行快照

@app.post("/snapshots")
def create_snapshot(body: SnapshotIn, conn=Depends(get_conn)):
    snapshot_id = domain.create_snapshot(conn, body.config_id, body.standard_id,
                                         _ts(body.as_of), _ts(body.created_at))
    return {"snapshot_id": snapshot_id}


@app.post("/snapshots/{snapshot_id}/sign")
def sign_snapshot(snapshot_id: str, body: SignIn, conn=Depends(get_conn)):
    return domain.sign_snapshot(conn, snapshot_id, body.signer, _ts(body.signed_at))


@app.get("/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str, conn=Depends(get_conn)):
    return domain.get_snapshot(conn, snapshot_id)


# ---------------------------------------------------------------- 评议与异议

@app.post("/decisions")
def create_decision(body: DecisionIn, conn=Depends(get_conn)):
    decision_id, status = domain.record_decision(
        conn, body.reviewer, body.clause_version_id, body.verdict, body.rationale,
        body.seen_epoch, _ts(body.recorded_at))
    return {"decision_id": decision_id, "status": status, "epoch": domain.current_epoch(conn)}


@app.get("/decisions")
def list_decisions(status: Optional[str] = None, conn=Depends(get_conn)):
    return domain.list_decisions(conn, status)


@app.post("/objections")
def create_objection(body: ObjectionIn, conn=Depends(get_conn)):
    objection_id = domain.raise_objection(conn, body.subject_kind, body.subject_id,
                                          body.raised_by, body.detail, _ts(body.recorded_at))
    return {"objection_id": objection_id}


@app.get("/objections")
def list_objections(open_only: bool = False, conn=Depends(get_conn)):
    return domain.list_objections(conn, open_only)


@app.post("/objections/{objection_id}/resolve")
def resolve_objection(objection_id: str, body: ResolveIn, conn=Depends(get_conn)):
    resolved_at = domain.resolve_objection(conn, objection_id, body.resolution,
                                           _ts(body.resolved_at))
    return {"objection_id": objection_id, "resolved_at": resolved_at}


# ---------------------------------------------------------------- 保密附件

@app.post("/attachments")
def create_attachment(body: AttachmentIn, conn=Depends(get_conn)):
    attachment_id = domain.create_attachment(
        conn, body.name, base64.b64decode(body.content_b64), body.media_type,
        body.confidential, _ts(body.recorded_at))
    return {"attachment_id": attachment_id}


@app.post("/attachments/{attachment_id}/grants")
def grant_attachment(attachment_id: str, body: GrantIn, conn=Depends(get_conn)):
    grant_id = domain.grant_attachment(conn, attachment_id, body.project_id,
                                       _ts(body.recorded_at))
    return {"grant_id": grant_id}


@app.post("/attachments/{attachment_id}/grants/{project_id}/revoke")
def revoke_attachment_grant(attachment_id: str, project_id: str, body: RetractIn,
                            conn=Depends(get_conn)):
    domain.revoke_grant(conn, attachment_id, project_id, _ts(body.retracted_at))
    return {"attachment_id": attachment_id, "project_id": project_id}


@app.get("/attachments/{attachment_id}/content")
def get_attachment_content(attachment_id: str, x_project_id: Optional[str] = Header(None),
                           conn=Depends(get_conn)):
    att = domain.attachment_content(conn, attachment_id, x_project_id)
    return Response(content=bytes(att["content"]), media_type=att["media_type"])


# ---------------------------------------------------------------- 复核与重现

@app.get("/review-tasks")
def list_review_tasks(status: Optional[str] = None, origin_version_id: Optional[str] = None,
                      conn=Depends(get_conn)):
    return domain.list_review_tasks(conn, status, origin_version_id)


@app.post("/review-tasks/{task_id}/resolve")
def resolve_review_task(task_id: str, body: ResolveIn, conn=Depends(get_conn)):
    resolved_at = domain.resolve_task(conn, task_id, _ts(body.resolved_at))
    return {"task_id": task_id, "resolved_at": resolved_at}


@app.get("/asof")
def asof(standard_id: str, at: str, conn=Depends(get_conn)):
    return domain.asof(conn, standard_id, db.parse_ts(at))
