"""条款谱系、义务映射、快照保护、评议并发与历史重现的端到端测试。"""

import base64
import sqlite3
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import app as app_module
import db

T0 = "2025-01-01T00:00:00Z"  # 主数据默认录入时刻


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "app.db"))
    with TestClient(app_module.app) as test_client:
        yield test_client


def _post(client, url, body):
    resp = client.post(url, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _get(client, url, **kwargs):
    resp = client.get(url, **kwargs)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _clause_url(clause_id, suffix=""):
    return f"/clauses/{quote(clause_id, safe='')}{suffix}"


def _make_standard(client, standard_id="AEC-Q101"):
    _post(client, "/standards", {"standard_id": standard_id,
                                 "title": "车规分立半导体器件应力测试", "recorded_at": T0})
    return standard_id


def _make_clause(client, clause_no, title, standard_id="AEC-Q101"):
    _post(client, f"/standards/{standard_id}/clauses",
          {"clause_no": clause_no, "title": title, "recorded_at": T0})
    return f"{standard_id}#{clause_no}"


def _make_version(client, clause_id, **kwargs):
    kwargs.setdefault("recorded_at", T0)
    return _post(client, _clause_url(clause_id, "/versions"), kwargs)["clause_version_id"]


def _applicable(client, clause_id, at):
    data = _get(client, _clause_url(clause_id, "/applicable"), params={"at": at})
    return [v["clause_version_id"] for v in data["versions"]]


def _make_obligation_with_targets(client, clause_version_id,
                                  recorded_at="2026-01-02T00:00:00Z"):
    """登记一条义务并映射到料号配置、供应商声明、试验结论、偏离批准。"""
    obligation_id = _post(client, "/obligations", {
        "clause_version_id": clause_version_id, "statement": "-55℃~150℃ 温度循环 1000 次",
        "recorded_at": recorded_at})["obligation_id"]
    config_id = _post(client, "/product-configs", {
        "part_number": "SQJ422EP", "product_line": "功率 MOSFET",
        "recorded_at": recorded_at})["config_id"]
    declaration_id = _post(client, "/supplier-declarations", {
        "supplier": "某晶圆厂", "part_number": "SQJ422EP", "statement": "外延片耐压符合声明",
        "recorded_at": recorded_at})["declaration_id"]
    conclusion_id = _post(client, "/test-conclusions", {
        "report_no": "TR-2026-001", "config_id": config_id, "result": "pass",
        "summary": "温度循环试验通过", "recorded_at": recorded_at})["conclusion_id"]
    approval_id = _post(client, "/deviation-approvals", {
        "config_id": config_id, "approver": "质量负责人",
        "rationale": "循环次数偏离已获客户批准", "recorded_at": recorded_at})["approval_id"]
    for kind, target in [("product_config", config_id),
                         ("supplier_declaration", declaration_id),
                         ("test_conclusion", conclusion_id),
                         ("deviation_approval", approval_id)]:
        _post(client, f"/obligations/{obligation_id}/links",
              {"target_kind": kind, "target_id": target, "recorded_at": recorded_at})
    return obligation_id, config_id, declaration_id, conclusion_id, approval_id


def test_health(client):
    assert _get(client, "/health") == {"status": "ok"}


def test_clause_genealogy_across_draft_errata_and_official(client):
    """同一条款在草案、正式、勘误之间多次改写，按生效区间维护谱系。"""
    _make_standard(client)
    clause_id = _make_clause(client, "4.1.2", "温度循环")
    v_draft = _make_version(client, clause_id, stage="draft",
                            content="草案：-40℃~125℃，500 次循环",
                            valid_from="2026-01-01T00:00:00Z",
                            recorded_at="2025-12-01T00:00:00Z")
    v_official = _make_version(
        client, clause_id, stage="official", content="正式：-55℃~150℃，1000 次循环",
        valid_from="2026-03-01T00:00:00Z", recorded_at="2026-02-20T00:00:00Z",
        edges=[{"from_version_id": v_draft, "relation": "replaces"}])
    v_errata = _make_version(
        client, clause_id, stage="errata", content="勘误：循环次数更正为 700 次",
        valid_from="2026-06-01T00:00:00Z", recorded_at="2026-05-30T00:00:00Z",
        edges=[{"from_version_id": v_official, "relation": "corrects"}])

    assert _applicable(client, clause_id, "2026-02-01T00:00:00Z") == [v_draft]
    assert _applicable(client, clause_id, "2026-04-01T00:00:00Z") == [v_official]
    assert _applicable(client, clause_id, "2026-07-01T00:00:00Z") == [v_errata]

    lineage = _get(client, _clause_url(clause_id, "/lineage"))
    assert [v["clause_version_id"] for v in lineage["versions"]] == [
        v_draft, v_official, v_errata]
    assert {(e["from_version_id"], e["to_version_id"], e["relation"])
            for e in lineage["edges"]} == {
        (v_draft, v_official, "replaces"), (v_official, v_errata, "corrects")}


def test_split_merge_and_withdraw_preserve_genealogy(client):
    """拆分、合并与撤回都以谱系边保留原关系，适用性随生效时刻切换。"""
    _make_standard(client)
    clause_a = _make_clause(client, "5.1", "高温反偏")
    v_a = _make_version(client, clause_a, stage="official", content="HTRB 总要求",
                        valid_from="2026-01-01T00:00:00Z")
    clause_b = _make_clause(client, "5.1.1", "高温反偏（N 沟道）")
    clause_c = _make_clause(client, "5.1.2", "高温反偏（P 沟道）")
    v_b = _make_version(client, clause_b, stage="official", content="N 沟道 HTRB",
                        valid_from="2026-05-01T00:00:00Z",
                        edges=[{"from_version_id": v_a, "relation": "splits_into"}])
    v_c = _make_version(client, clause_c, stage="official", content="P 沟道 HTRB",
                        valid_from="2026-05-01T00:00:00Z",
                        edges=[{"from_version_id": v_a, "relation": "splits_into"}])
    clause_m = _make_clause(client, "5.2", "高温栅偏")
    v_m = _make_version(client, clause_m, stage="official", content="合并后的 HTGB 要求",
                        valid_from="2026-08-01T00:00:00Z",
                        edges=[{"from_version_id": v_b, "relation": "merges_into"},
                               {"from_version_id": v_c, "relation": "merges_into"}])
    _post(client, f"/clause-versions/{v_m}/withdraw",
          {"effective_at": "2026-09-15T00:00:00Z", "recorded_at": "2026-09-01T00:00:00Z"})

    assert _applicable(client, clause_a, "2026-04-01T00:00:00Z") == [v_a]
    assert _applicable(client, clause_a, "2026-06-01T00:00:00Z") == []
    assert _applicable(client, clause_b, "2026-06-01T00:00:00Z") == [v_b]
    assert _applicable(client, clause_b, "2026-09-01T00:00:00Z") == []
    assert _applicable(client, clause_m, "2026-09-01T00:00:00Z") == [v_m]
    assert _applicable(client, clause_m, "2026-10-01T00:00:00Z") == []

    lineage_a = _get(client, _clause_url(clause_a, "/lineage"))
    assert sorted(e["relation"] for e in lineage_a["edges"]) == [
        "splits_into", "splits_into"]
    assert set(lineage_a["related_clause_ids"]) == {clause_b, clause_c}

    lineage_m = _get(client, _clause_url(clause_m, "/lineage"))
    assert sorted(e["relation"] for e in lineage_m["edges"]) == [
        "merges_into", "merges_into", "withdraws"]


def test_obligation_mapping_and_change_impact(client):
    """义务映射到四类对象，负责人可从一次标准变化追到全部受影响项。"""
    _make_standard(client)
    clause_id = _make_clause(client, "4.1.2", "温度循环")
    v1 = _make_version(client, clause_id, stage="official", content="1000 次循环",
                       valid_from="2026-01-01T00:00:00Z")
    obligation_id, config_id, declaration_id, conclusion_id, approval_id = (
        _make_obligation_with_targets(client, v1))
    v2 = _make_version(client, clause_id, stage="official", content="修订为 700 次循环",
                       valid_from="2026-07-01T00:00:00Z",
                       recorded_at="2026-06-30T00:00:00Z",
                       edges=[{"from_version_id": v1, "relation": "replaces"}])

    impact = _get(client, f"/clause-versions/{v2}/impact")
    assert v1 in impact["related_clause_version_ids"]
    assert obligation_id in [o["obligation_id"] for o in impact["obligations"]]
    assert [c["config_id"] for c in impact["affected"]["product_configs"]] == [config_id]
    assert [d["declaration_id"] for d in impact["affected"]["supplier_declarations"]] == [
        declaration_id]
    assert [t["conclusion_id"] for t in impact["affected"]["test_conclusions"]] == [
        conclusion_id]
    assert [a["approval_id"] for a in impact["affected"]["deviation_approvals"]] == [
        approval_id]


def test_signed_snapshot_is_immutable_and_late_correction_raises_divergence(client):
    """迟到的更正不得悄悄改变已签署的放行快照，只能登记偏差与复核任务。"""
    _make_standard(client)
    clause_id = _make_clause(client, "4.1.2", "温度循环")
    v1 = _make_version(client, clause_id, stage="official", content="1000 次循环",
                       valid_from="2026-01-01T00:00:00Z")
    _, config_id, *_ = _make_obligation_with_targets(client, v1)

    snapshot_id = _post(client, "/snapshots", {
        "config_id": config_id, "standard_id": "AEC-Q101",
        "as_of": "2026-02-01T00:00:00Z", "created_at": "2026-02-01T00:00:00Z"})["snapshot_id"]
    _post(client, f"/snapshots/{snapshot_id}/sign",
          {"signer": "质量负责人", "signed_at": "2026-02-02T00:00:00Z"})
    before = _get(client, f"/snapshots/{snapshot_id}")
    assert before["items"] == [v1]
    assert before["divergences"] == []

    # 迟到的勘误：生效日回溯到签署之前，但系统录入晚于签署
    v2 = _make_version(client, clause_id, stage="errata", content="勘误：循环条件更正",
                       valid_from="2026-01-15T00:00:00Z",
                       recorded_at="2026-03-01T00:00:00Z",
                       edges=[{"from_version_id": v1, "relation": "corrects"}])
    after = _get(client, f"/snapshots/{snapshot_id}")
    assert after["items"] == [v1]  # 快照内容不被悄悄改变
    assert after["digest"] == before["digest"]
    assert [(d["clause_version_id"], d["kind"]) for d in after["divergences"]] == [
        (v2, "retroactive")]

    # 触发器兜底：任何对已签署快照的改写都会被数据库拒绝
    conn = db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE release_snapshots SET as_of = ? WHERE snapshot_id = ?",
                         ("2026-02-15T00:00:00+00:00", snapshot_id))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO snapshot_items(snapshot_id, clause_version_id) VALUES (?, ?)",
                (snapshot_id, v2))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM release_snapshots WHERE snapshot_id = ?", (snapshot_id,))
    finally:
        conn.close()

    # 前瞻性的后续修订同样登记偏差
    v3 = _make_version(client, clause_id, stage="official", content="下一次修订",
                       valid_from="2026-04-01T00:00:00Z",
                       recorded_at="2026-03-05T00:00:00Z",
                       edges=[{"from_version_id": v2, "relation": "replaces"}])
    final = _get(client, f"/snapshots/{snapshot_id}")
    assert (v3, "prospective") in [
        (d["clause_version_id"], d["kind"]) for d in final["divergences"]]

    tasks = _get(client, "/review-tasks", params={"status": "open"})
    assert {t["kind"] for t in tasks} == {"snapshot_divergence"}
    impact = _get(client, f"/clause-versions/{v2}/impact")
    assert any(t["kind"] == "snapshot_divergence" for t in impact["open_tasks"])


def test_concurrent_review_flags_decisions_based_on_old_versions(client):
    """多人同时评议时，基于旧版本的决定被检测并生成复核任务。"""
    _make_standard(client)
    clause_id = _make_clause(client, "4.1.2", "温度循环")
    v1 = _make_version(client, clause_id, stage="official", content="1000 次循环",
                       valid_from="2026-01-01T00:00:00Z")
    d1 = _post(client, "/decisions", {
        "reviewer": "工程师甲", "clause_version_id": v1, "verdict": "accept",
        "rationale": "满足要求", "recorded_at": "2026-01-02T00:00:00Z"})
    assert d1["status"] == "open"

    # 评议期间条款被改写
    v2 = _make_version(client, clause_id, stage="errata", content="勘误：700 次循环",
                       valid_from="2026-02-01T00:00:00Z",
                       recorded_at="2026-01-10T00:00:00Z",
                       edges=[{"from_version_id": v1, "relation": "corrects"}])

    # 既有未决决定被主动标记
    stale = _get(client, "/decisions", params={"status": "stale"})
    assert d1["decision_id"] in [d["decision_id"] for d in stale]

    # 仍基于旧版本提交的新决定被即时检测
    d2 = _post(client, "/decisions", {
        "reviewer": "工程师乙", "clause_version_id": v1, "verdict": "accept",
        "recorded_at": "2026-01-11T00:00:00Z"})
    assert d2["status"] == "stale"
    d3 = _post(client, "/decisions", {
        "reviewer": "工程师乙", "clause_version_id": v2, "verdict": "accept",
        "recorded_at": "2026-01-12T00:00:00Z"})
    assert d3["status"] == "open"

    tasks = _get(client, "/review-tasks", params={"status": "open"})
    stale_tasks = [t for t in tasks if t["kind"] == "stale_decision"]
    assert {t["ref_id"] for t in stale_tasks} == {d1["decision_id"], d2["decision_id"]}


def test_confidential_attachment_requires_project_grant(client):
    """保密附件只向获授权项目开放。"""
    _post(client, "/projects", {"project_id": "PRJ-OBC", "name": "车载充电机"})
    _post(client, "/projects", {"project_id": "PRJ-BMS", "name": "电池管理"})
    secret = base64.b64encode(b"confidential-junction-temperature").decode()
    att = _post(client, "/attachments", {
        "name": "结温曲线.pdf", "content_b64": secret,
        "media_type": "application/pdf", "confidential": True})["attachment_id"]

    assert client.get(f"/attachments/{att}/content").status_code == 403
    assert client.get(f"/attachments/{att}/content",
                      headers={"X-Project-Id": "PRJ-BMS"}).status_code == 403

    _post(client, f"/attachments/{att}/grants", {"project_id": "PRJ-OBC"})
    resp = client.get(f"/attachments/{att}/content", headers={"X-Project-Id": "PRJ-OBC"})
    assert resp.status_code == 200
    assert resp.content == b"confidential-junction-temperature"

    _post(client, f"/attachments/{att}/grants/PRJ-OBC/revoke", {})
    assert client.get(f"/attachments/{att}/content",
                      headers={"X-Project-Id": "PRJ-OBC"}).status_code == 403

    public = _post(client, "/attachments", {
        "name": "公开勘误表.txt",
        "content_b64": base64.b64encode(b"public").decode()})["attachment_id"]
    assert client.get(f"/attachments/{public}/content").status_code == 200


def test_asof_reconstructs_clauses_evidence_and_objections(client):
    """按任意历史时点重现当时适用的条款、证据和未决异议。"""
    _make_standard(client)
    clause_id = _make_clause(client, "4.1.2", "温度循环")
    v1 = _make_version(client, clause_id, stage="official", content="1000 次循环",
                       valid_from="2026-01-01T00:00:00Z",
                       recorded_at="2026-01-01T00:00:00Z")
    obligation_id = _post(client, "/obligations", {
        "clause_version_id": v1, "statement": "温度循环 1000 次",
        "recorded_at": "2026-01-01T00:00:00Z"})["obligation_id"]
    config_id = _post(client, "/product-configs", {
        "part_number": "SQJ422EP", "product_line": "功率 MOSFET",
        "recorded_at": "2026-01-01T00:00:00Z"})["config_id"]
    link_id = _post(client, f"/obligations/{obligation_id}/links", {
        "target_kind": "product_config", "target_id": config_id,
        "recorded_at": "2026-01-05T00:00:00Z"})["link_id"]
    objection_id = _post(client, "/objections", {
        "subject_kind": "obligation", "subject_id": obligation_id,
        "raised_by": "客户质量", "detail": "循环次数与客户协议不一致",
        "recorded_at": "2026-01-10T00:00:00Z"})["objection_id"]

    def asof(at):
        return _get(client, "/asof", params={"standard_id": "AEC-Q101", "at": at})

    mid = asof("2026-01-15T00:00:00Z")
    assert mid["clauses"][0]["applicable_versions"] == [v1]
    assert [e["link_id"] for e in mid["evidence"]] == [link_id]
    assert [o["objection_id"] for o in mid["open_objections"]] == [objection_id]

    # 之后撤回映射、关闭异议
    _post(client, f"/obligation-links/{link_id}/retract",
          {"retracted_at": "2026-02-01T00:00:00Z"})
    _post(client, f"/objections/{objection_id}/resolve",
          {"resolution": "客户确认接受偏离", "resolved_at": "2026-02-10T00:00:00Z"})

    still_mid = asof("2026-01-15T00:00:00Z")
    assert [e["link_id"] for e in still_mid["evidence"]] == [link_id]  # 历史时点不受影响
    assert [o["objection_id"] for o in still_mid["open_objections"]] == [objection_id]

    after_retract = asof("2026-02-05T00:00:00Z")
    assert after_retract["evidence"] == []
    assert [o["objection_id"] for o in after_retract["open_objections"]] == [objection_id]

    assert asof("2026-02-15T00:00:00Z")["open_objections"] == []

    # 迟到的更正不改变「当时适用」的重现
    v2 = _make_version(client, clause_id, stage="errata", content="勘误：700 次循环",
                       valid_from="2026-01-01T00:00:00Z",
                       recorded_at="2026-03-01T00:00:00Z",
                       edges=[{"from_version_id": v1, "relation": "corrects"}])
    assert asof("2026-01-15T00:00:00Z")["clauses"][0]["applicable_versions"] == [v1]
    assert asof("2026-03-02T00:00:00Z")["clauses"][0]["applicable_versions"] == [v2]
