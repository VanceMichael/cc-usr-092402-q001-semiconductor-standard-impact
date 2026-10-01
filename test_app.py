"""标准变更影响闭环的端到端测试。

覆盖：生效区间谱系、四类义务映射、替代/拆分/合并/撤回保留原关系、
快照不可变与迟到更正、并发评议的旧版本检测、保密附件授权、
影响追踪、任意历史时点重现。
"""
import pytest
from fastapi.testclient import TestClient

from app import create_app
from db import applied_version, connect, migrate


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "app.db"))
    with TestClient(app) as c:
        yield c


# ---------- 构造辅助 ----------

def add_standard(client, code="AEC-Q101"):
    r = client.post("/standards", json={"code": code, "title": "车规功率器件标准"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def add_clause(client, standard_id, no="7.3.2", title="耐压试验"):
    r = client.post(f"/standards/{standard_id}/clauses",
                    json={"clause_no": no, "title": title})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def add_version(client, clause_id, label, stage, valid_from, recorded_at,
                relations=None, obligation="应满足耐压要求"):
    r = client.post(f"/clauses/{clause_id}/versions", json={
        "version_label": label,
        "stage": stage,
        "content": f"{label} 条款正文",
        "obligation": obligation,
        "valid_from": valid_from,
        "recorded_at": recorded_at,
        "relations": relations or [],
    })
    assert r.status_code == 201, r.text
    return r.json()


def add_product(client, part_number="PN-001"):
    r = client.post("/products", json={
        "part_number": part_number,
        "config": {"package": "TO-247", "voltage_class": "1200V"},
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def add_mapping(client, version_id, target_type, target_id, **kw):
    payload = {"clause_version_id": version_id, "target_type": target_type,
               "target_id": target_id, "disposition": "compliant"}
    payload.update(kw)
    return client.post("/mappings", json=payload)


def open_tasks(client, **params):
    return client.get("/review-tasks", params={"status": "open", **params}).json()["tasks"]


# ---------- 基础 ----------

def test_health_and_migration_idempotent(tmp_path):
    db_path = str(tmp_path / "app.db")
    assert migrate(db_path) == 2
    assert migrate(db_path) == 2  # 重复执行无副作用
    with connect(db_path) as db:
        assert applied_version(db) == 2
    app = create_app(db_path)
    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}


# ---------- 1. 条款谱系按生效区间维护 ----------

def test_clause_lineage_across_draft_errata_official(client):
    sid = add_standard(client)
    cid = add_clause(client, sid)
    add_version(client, cid, "2024-D1", "draft", "2024-06-01", "2023-11-01")
    v1 = add_version(client, cid, "2024-F", "official", "2024-01-01", "2023-12-01")
    v2 = add_version(client, cid, "2024-E1", "errata", "2024-06-01", "2024-05-20",
                     relations=[{"from_version_id": v1["version"]["id"],
                                 "relation": "replaces"}])
    v3 = add_version(client, cid, "2025-F", "official", "2025-01-01", "2024-12-15",
                     relations=[{"from_version_id": v2["version"]["id"],
                                 "relation": "replaces"}])

    # 生效区间决定各时点适用版本
    assert client.get(f"/clauses/{cid}/applicable",
                      params={"at": "2024-03-01"}).json()["current"]["version_label"] == "2024-F"
    assert client.get(f"/clauses/{cid}/applicable",
                      params={"at": "2024-07-01"}).json()["current"]["version_label"] == "2024-E1"
    assert client.get(f"/clauses/{cid}/applicable",
                      params={"at": "2025-02-01"}).json()["current"]["version_label"] == "2025-F"

    # 获知时间轴：勘误登记之前回看，当时适用的仍是正式版
    view = client.get(f"/clauses/{cid}/applicable",
                      params={"at": "2024-07-01", "knowledge_at": "2024-05-01"}).json()
    assert view["current"]["version_label"] == "2024-F"

    # 谱系完整可查
    lineage = client.get(f"/clauses/{cid}/lineage").json()
    assert [v["version_label"] for v in lineage["versions"]] == [
        "2024-F", "2024-D1", "2024-E1", "2025-F"]
    assert [r["relation"] for r in lineage["relations"]] == ["replaces", "replaces"]


# ---------- 2. 义务映射到四类目标 ----------

def test_obligation_mapping_to_four_target_kinds(client):
    sid = add_standard(client)
    cid = add_clause(client, sid)
    v1 = add_version(client, cid, "2024-F", "official", "2024-01-01", "2023-12-01")
    vid = v1["version"]["id"]

    pid = add_product(client)
    decl = client.post("/supplier-declarations", json={
        "part_number": "PN-001", "supplier": "ACME", "content": "材料符合声明",
        "recorded_at": "2024-02-01"}).json()
    report = client.post("/test-reports", json={
        "report_no": "TR-1001", "part_number": "PN-001", "conclusion": "pass",
        "recorded_at": "2024-02-05"}).json()
    deviation = client.post("/deviation-approvals", json={
        "deviation_no": "DV-07", "part_number": "PN-001", "scope": "湿热循环次数减半",
        "approved_by": "quality-lead", "recorded_at": "2024-02-10"}).json()

    for target_type, target_id in [
        ("product_config", pid),
        ("supplier_declaration", decl["id"]),
        ("test_conclusion", report["id"]),
        ("deviation_approval", deviation["id"]),
    ]:
        r = add_mapping(client, vid, target_type, target_id,
                        decided_at="2024-03-01", decided_by="alice")
        assert r.status_code == 201, r.text
        assert r.json()["stale"] is False

    mappings = client.get("/mappings", params={"part_number": "PN-001"}).json()["mappings"]
    assert len(mappings) == 4
    assert {m["target"]["target_label"] for m in mappings} == {
        "产品配置", "供应商声明", "试验结论", "偏离批准"}


# ---------- 3. 替代/拆分/合并/撤回保留原关系 ----------

def test_replace_preserves_mapping_and_carry_forward(client):
    sid = add_standard(client)
    cid = add_clause(client, sid)
    v1 = add_version(client, cid, "2024-F", "official", "2024-01-01", "2023-12-01")
    pid = add_product(client)
    m1 = add_mapping(client, v1["version"]["id"], "product_config", pid,
                     decided_at="2024-02-01").json()["mapping"]

    # 替代：原映射保留在原版本上，并生成复核任务
    v2 = add_version(client, cid, "2025-F", "official", "2025-01-01", "2024-12-01",
                     relations=[{"from_version_id": v1["version"]["id"],
                                 "relation": "replaces"}])
    assert len(v2["tasks"]) == 1
    assert v2["tasks"][0]["kind"] == "clause_change"
    assert v2["tasks"][0]["mapping_id"] == m1["id"]
    old = client.get("/mappings", params={"open_only": False}).json()["mappings"][0]
    assert old["clause_version_id"] == v1["version"]["id"]
    assert old["closed_at"] is None

    # 结转：新映射记录来源，旧映射关闭，复核任务了结
    carried = client.post(f"/mappings/{m1['id']}/carry", json={
        "clause_version_id": v2["version"]["id"], "decided_by": "alice",
        "decided_at": "2025-01-10"})
    assert carried.status_code == 201, carried.text
    body = carried.json()
    assert body["carried_from_id"] == m1["id"]
    assert body["resolved_tasks"] == 1
    assert open_tasks(client) == []
    mappings = client.get("/mappings").json()["mappings"]
    by_id = {m["id"]: m for m in mappings}
    assert by_id[m1["id"]]["closed_at"] is not None
    assert by_id[body["mapping"]["id"]]["carried_from_id"] == m1["id"]


def test_split_merge_withdraw_preserve_relations(client):
    sid = add_standard(client)
    # 拆分：B v1 -> B1 + B2
    cb = add_clause(client, sid, "8.1", "老条款")
    vb = add_version(client, cb, "B-1", "official", "2024-01-01", "2023-12-01")
    pb = add_product(client, "PN-100")
    mb = add_mapping(client, vb["version"]["id"], "product_config", pb,
                     decided_at="2024-02-01").json()["mapping"]
    cb1 = add_clause(client, sid, "8.1.1", "拆分条款一")
    cb2 = add_clause(client, sid, "8.1.2", "拆分条款二")
    add_version(client, cb1, "B1-1", "official", "2025-01-01", "2024-12-01",
                relations=[{"from_version_id": vb["version"]["id"],
                            "relation": "splits_into"}])
    add_version(client, cb2, "B2-1", "official", "2025-01-01", "2024-12-01",
                relations=[{"from_version_id": vb["version"]["id"],
                            "relation": "splits_into"}])
    lineage = client.get(f"/clauses/{cb}/lineage").json()
    assert sorted(r["relation"] for r in lineage["relations"]) == ["splits_into"] * 2
    # 原映射仍在旧版本上，拆分两侧各生成一条复核任务
    tasks = open_tasks(client, kind="clause_change")
    assert {t["mapping_id"] for t in tasks} == {mb["id"]}
    assert len(tasks) == 2

    # 合并：C1 + C2 -> C
    cc1 = add_clause(client, sid, "9.1", "合并来源一")
    cc2 = add_clause(client, sid, "9.2", "合并来源二")
    vc1 = add_version(client, cc1, "C1-1", "official", "2024-01-01", "2023-12-01")
    vc2 = add_version(client, cc2, "C2-1", "official", "2024-01-01", "2023-12-01")
    cc = add_clause(client, sid, "9.3", "合并后条款")
    add_version(client, cc, "C-1", "official", "2025-06-01", "2025-05-01", relations=[
        {"from_version_id": vc1["version"]["id"], "relation": "merges_into"},
        {"from_version_id": vc2["version"]["id"], "relation": "merges_into"},
    ])
    for cid_old in (cc1, cc2):
        view = client.get(f"/clauses/{cid_old}/applicable",
                          params={"at": "2025-07-01"}).json()
        assert view["current"] is None  # 合并生效后旧版本不再适用

    # 撤回：关系登记后条款停止适用，既有映射生成撤回复核任务
    cd = add_clause(client, sid, "10.1", "将撤回条款")
    vd = add_version(client, cd, "D-1", "official", "2024-01-01", "2023-12-01")
    pd = add_product(client, "PN-200")
    md = add_mapping(client, vd["version"]["id"], "product_config", pd,
                     decided_at="2024-02-01").json()["mapping"]
    r = client.post("/relations", json={
        "from_version_id": vd["version"]["id"], "relation": "withdraws",
        "effective_at": "2026-01-01", "note": "标准组织公告废止"})
    assert r.status_code == 201, r.text
    assert r.json()["tasks"][0]["kind"] == "withdrawal"
    assert r.json()["tasks"][0]["mapping_id"] == md["id"]
    # 撤回是追溯生效的：以登记之前的认知看，条款当时仍适用；
    # 以登记之后的认知看，2026-02-01 起条款已不再适用
    unaware = client.get(f"/clauses/{cd}/applicable", params={
        "at": "2026-02-01", "knowledge_at": "2026-02-01"}).json()
    assert unaware["current"]["version_label"] == "D-1"
    aware = client.get(f"/clauses/{cd}/applicable", params={
        "at": "2026-02-01", "knowledge_at": "2027-01-01"}).json()
    assert aware["current"] is None
    # 撤回后新决定被拒绝
    rejected = add_mapping(client, vd["version"]["id"], "product_config", pd,
                           decided_at="2026-02-02")
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["error"] == "clause_withdrawn"


# ---------- 4. 迟到的更正不改写已签署快照 ----------

def test_late_correction_does_not_rewrite_signed_snapshot(client):
    sid = add_standard(client)
    cid = add_clause(client, sid)
    v1 = add_version(client, cid, "2025-F", "official", "2025-01-01", "2024-12-01")
    pid = add_product(client)
    m1 = add_mapping(client, v1["version"]["id"], "product_config", pid,
                     decided_at="2025-02-01").json()["mapping"]
    project = client.post("/projects", json={"code": "P1", "name": "一代平台"}).json()

    snap = client.post("/snapshots", json={
        "part_number": "PN-001", "project_id": project["id"], "standard_id": sid,
        "signed_by": "release-owner", "signed_at": "2025-03-01"}).json()
    snap_id = snap["snapshot"]["id"]
    assert snap["items"][0]["version_label"] == "2025-F"
    assert client.get(f"/snapshots/{snap_id}/verify").json()["ok"] is True

    # 迟到的勘误：6 月才登记，生效却追溯到 1 月 15 日（早于决定与签署）
    v2 = add_version(client, cid, "2025-E1", "errata", "2025-01-15", "2025-06-01",
                     relations=[{"from_version_id": v1["version"]["id"],
                                 "relation": "replaces",
                                 "effective_at": "2025-01-15"}])
    kinds = {(t["kind"], t.get("mapping_id"), t.get("snapshot_id")) for t in v2["tasks"]}
    assert ("late_correction", m1["id"], None) in kinds
    assert ("late_correction", None, snap_id) in kinds

    # 快照内容与哈希保持不变
    after = client.get(f"/snapshots/{snap_id}").json()
    assert after["items"][0]["version_label"] == "2025-F"
    assert client.get(f"/snapshots/{snap_id}/verify").json()["ok"] is True

    # 差异端点暴露迟到更正：以当前认知重放签署时点，版本已变为勘误
    diff = client.get(f"/snapshots/{snap_id}/diff").json()
    assert len(diff["late_corrections"]) == 1
    delta = diff["late_corrections"][0]
    assert delta["frozen"]["version_label"] == "2025-F"
    assert delta["computed"]["version_label"] == "2025-E1"


# ---------- 5. 多人评议时检测基于旧版本的决定 ----------

def test_concurrent_review_detects_stale_decision(client):
    sid = add_standard(client)
    cid = add_clause(client, sid)
    v1 = add_version(client, cid, "2025-F", "official", "2025-01-01", "2024-12-01")
    pid = add_product(client)

    # Alice 打开评议页面时最新版本是 2025-F；Bob 随后登记了新版本
    v2 = add_version(client, cid, "2025-E1", "errata", "2025-04-01", "2025-03-01",
                     relations=[{"from_version_id": v1["version"]["id"],
                                 "relation": "replaces"}])

    # Alice 仍基于旧版本提交决定 -> 409，响应携带当前最新版本
    stale = add_mapping(client, v1["version"]["id"], "product_config", pid,
                        basis_version_id=v1["version"]["id"],
                        decided_at="2025-03-15", decided_by="alice")
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["error"] == "stale_basis"
    assert detail["head_version_id"] == v2["version"]["id"]

    # 显式留痕放行 -> 生成 stale_decision 复核任务
    allowed = add_mapping(client, v1["version"]["id"], "product_config", pid,
                          basis_version_id=v1["version"]["id"],
                          decided_at="2025-03-15", decided_by="alice",
                          allow_stale=True)
    assert allowed.status_code == 201
    assert allowed.json()["stale"] is True
    assert allowed.json()["tasks"][0]["kind"] == "stale_decision"
    stale_list = client.get("/mappings", params={"stale": True}).json()["mappings"]
    assert [m["id"] for m in stale_list] == [allowed.json()["mapping"]["id"]]

    # Bob 基于新版本的决定正常通过
    fresh = add_mapping(client, v2["version"]["id"], "product_config", pid,
                        decided_at="2025-03-16", decided_by="bob")
    assert fresh.status_code == 201
    assert fresh.json()["stale"] is False


# ---------- 6. 保密附件只向获授权项目开放 ----------

def test_confidential_attachment_scoped_to_authorized_projects(client):
    for name in ("alice", "bob", "carol"):
        assert client.post("/users", json={"name": name}).status_code == 201
    p1 = client.post("/projects", json={"code": "P1", "name": "平台一"}).json()
    p2 = client.post("/projects", json={"code": "P2", "name": "平台二"}).json()
    client.post(f"/projects/{p1['id']}/members", json={"user": "alice"})
    client.post(f"/projects/{p2['id']}/members", json={"user": "carol"})

    secret = client.post("/attachments", json={
        "project_id": p1["id"], "name": "失效分析报告.pdf",
        "content": "保密内容", "confidential": True},
        headers={"X-User-Name": "alice"})
    assert secret.status_code == 201, secret.text
    aid = secret.json()["attachment"]["id"]

    assert client.get(f"/attachments/{aid}").status_code == 401            # 未认证
    assert client.get(f"/attachments/{aid}",
                      headers={"X-User-Name": "bob"}).status_code == 403   # 非成员
    assert client.get(f"/attachments/{aid}",
                      headers={"X-User-Name": "carol"}).status_code == 403 # 其他项目
    ok = client.get(f"/attachments/{aid}", headers={"X-User-Name": "alice"})
    assert ok.status_code == 200
    assert ok.json()["attachment"]["content"] == "保密内容"

    # 非项目成员不能上传；成员可授权给其他项目
    forbidden = client.post("/attachments", json={
        "project_id": p1["id"], "name": "x", "content": "y"},
        headers={"X-User-Name": "bob"})
    assert forbidden.status_code == 403
    grant = client.post(f"/attachments/{aid}/grants", json={"project_id": p2["id"]},
                        headers={"X-User-Name": "alice"})
    assert grant.status_code == 201
    assert client.get(f"/attachments/{aid}",
                      headers={"X-User-Name": "carol"}).status_code == 200

    # 非保密附件任何登记用户可读
    public = client.post("/attachments", json={
        "project_id": p1["id"], "name": "公开 datasheet", "content": "公开内容",
        "confidential": False}, headers={"X-User-Name": "alice"}).json()
    assert client.get(f"/attachments/{public['attachment']['id']}",
                      headers={"X-User-Name": "bob"}).status_code == 200


# ---------- 7. 从一次标准变化追到全部待复核事项 ----------

def test_impact_trace_from_change_to_pending_items(client):
    sid = add_standard(client)
    cid = add_clause(client, sid)
    v1 = add_version(client, cid, "2025-F", "official", "2025-01-01", "2024-12-01")
    project = client.post("/projects", json={"code": "P1", "name": "平台一"}).json()

    p1 = add_product(client, "PN-001")
    p2 = add_product(client, "PN-002")
    report = client.post("/test-reports", json={
        "report_no": "TR-1", "part_number": "PN-001", "conclusion": "pass"}).json()
    add_mapping(client, v1["version"]["id"], "product_config", p1,
                decided_at="2025-02-01")
    add_mapping(client, v1["version"]["id"], "test_conclusion", report["id"],
                decided_at="2025-02-02")
    client.post("/snapshots", json={
        "part_number": "PN-001", "project_id": project["id"], "standard_id": sid,
        "signed_at": "2025-02-10"})
    client.post("/customer-commitments", json={
        "commitment_no": "CC-1", "customer": "车厂A", "part_number": "PN-001",
        "content": "承诺满足 AEC-Q101 全部条款", "promised_at": "2025-01-20"})
    # 无关料号的映射不应被卷入
    cid2 = add_clause(client, sid, "8.8", "无关条款")
    v_other = add_version(client, cid2, "X-1", "official", "2025-01-01", "2024-12-01")
    add_mapping(client, v_other["version"]["id"], "product_config", p2,
                decided_at="2025-02-01")

    v2 = add_version(client, cid, "2025-E1", "errata", "2025-03-01", "2025-02-20",
                     relations=[{"from_version_id": v1["version"]["id"],
                                 "relation": "replaces"}])
    impact = client.get(f"/changes/{v2['version']['id']}/impact").json()

    assert impact["open_task_count"] == 3          # 两条映射 + 一份快照
    assert impact["affected_part_numbers"] == ["PN-001"]
    assert {m["target_type"] for m in impact["affected_mappings"]} == {
        "product_config", "test_conclusion"}
    assert len(impact["affected_snapshots"]) == 1
    assert [c["commitment_no"] for c in impact["customer_commitments"]] == ["CC-1"]
    assert {t["status"] for t in impact["tasks"]} == {"open"}


# ---------- 8. 任意历史时点重现 ----------

def test_as_of_reconstructs_clauses_evidence_objections(client):
    sid = add_standard(client)
    cid = add_clause(client, sid)
    v1 = add_version(client, cid, "2025-F", "official", "2025-01-01", "2024-12-01")
    pid = add_product(client)
    m1 = add_mapping(client, v1["version"]["id"], "product_config", pid,
                     decided_at="2025-02-01").json()["mapping"]
    objection = client.post("/objections", json={
        "mapping_id": m1["id"], "content": "试验样本量不足",
        "raised_by": "carol", "raised_at": "2025-02-15"}).json()["objection"]
    v2 = add_version(client, cid, "2025-E1", "errata", "2025-04-01", "2025-03-01",
                     relations=[{"from_version_id": v1["version"]["id"],
                                 "relation": "replaces"}])
    client.post(f"/objections/{objection['id']}/resolve",
                json={"resolved_by": "alice", "resolved_at": "2025-03-10"})
    client.post(f"/mappings/{m1['id']}/close",
                json={"reason": "按勘误重评", "closed_at": "2025-04-15"})
    add_mapping(client, v2["version"]["id"], "product_config", pid,
                decided_at="2025-04-20")

    # 2 月 20 日：v1 适用，证据 m1 在，异议未决
    feb = client.get("/as-of", params={"at": "2025-02-20",
                                       "standard_id": sid}).json()
    assert feb["clauses"][0]["current"]["version_label"] == "2025-F"
    assert [e["id"] for e in feb["evidence"]] == [m1["id"]]
    assert [o["id"] for o in feb["open_objections"]] == [objection["id"]]

    # 3 月 15 日：异议已了结；新版本已登记但尚未生效，适用仍是 v1；
    # 版本登记触发的复核任务处于未决
    mar = client.get("/as-of", params={"at": "2025-03-15",
                                       "standard_id": sid}).json()
    assert mar["clauses"][0]["current"]["version_label"] == "2025-F"
    assert mar["open_objections"] == []
    assert len(mar["open_review_tasks"]) == 1
    assert mar["open_review_tasks"][0]["kind"] == "clause_change"

    # 4 月 10 日：勘误适用；m1 尚未关闭；新映射尚未决定
    apr = client.get("/as-of", params={"at": "2025-04-10",
                                       "standard_id": sid}).json()
    assert apr["clauses"][0]["current"]["version_label"] == "2025-E1"
    assert [e["id"] for e in apr["evidence"]] == [m1["id"]]

    # 4 月 20 日之后：m1 已关闭，新映射生效
    may = client.get("/as-of", params={"at": "2025-05-01",
                                       "standard_id": sid}).json()
    assert [e["id"] for e in may["evidence"]] != [m1["id"]]
    assert all(e["version_label"] == "2025-E1" for e in may["evidence"])


def test_as_of_knowledge_axis_shows_late_correction(client):
    sid = add_standard(client)
    cid = add_clause(client, sid)
    v1 = add_version(client, cid, "2025-F", "official", "2025-01-01", "2024-12-01")
    # 6 月才登记的勘误，生效追溯到 1 月
    add_version(client, cid, "2025-E1", "errata", "2025-01-15", "2025-06-01",
                relations=[{"from_version_id": v1["version"]["id"],
                            "relation": "replaces", "effective_at": "2025-01-15"}])

    # 以 3 月 1 日当时所获知重现：适用 2025-F
    then = client.get("/as-of", params={
        "at": "2025-03-01", "knowledge_at": "2025-03-01",
        "standard_id": sid}).json()
    assert then["clauses"][0]["current"]["version_label"] == "2025-F"

    # 以现在所获知重看同一时点：勘误已追溯生效
    now_view = client.get("/as-of", params={
        "at": "2025-03-01", "knowledge_at": "2025-07-01",
        "standard_id": sid}).json()
    assert now_view["clauses"][0]["current"]["version_label"] == "2025-E1"
