"""M8 恢复边界：恢复点冻结、回滚阻断、幂等重试、中断恢复、共享报表一致性。

约束对应：
- 恢复依据未冻结不能放行回滚：test_freeze_gate_blocks_unfrozen
- 每个消费方回到最后完整版：test_boundary_*
- 共享报表只引用同次发布：test_shared_report_mismatch_blocks
- 页面与恢复单导出对恢复点与阻断原因一致：test_page_and_export_stay_consistent
- 中断可恢复且不丢记录、同迁移重试不新增恢复点：test_interrupt_resume_* / test_retry_*
"""

from __future__ import annotations

import threading

import pytest

from app import errors, store
from app.modules.m8_recovery import boundary, freeze, service


# ---------- 纯规则：恢复边界 ----------

def _synth_run(seq: int, resolved=(), broken=()) -> dict:
    return {
        "version_seq": seq,
        "resolution": {
            "resolved": [{"consumer": name} for name in resolved],
            "broken": [{"consumer": name} for name in broken],
        },
    }


def test_consumer_state_complete_incomplete_absent():
    run = _synth_run(1, resolved=["a", "b"], broken=["c"])
    assert boundary.consumer_state("a", run) == boundary.COMPLETE
    assert boundary.consumer_state("c", run) == boundary.INCOMPLETE
    assert boundary.consumer_state("late-bird", run) == boundary.ABSENT


def test_boundary_picks_last_complete_release():
    runs = [
        _synth_run(1, resolved=["a"], broken=["c"]),
        _synth_run(2, resolved=["a", "c"]),
        _synth_run(3, resolved=["a", "c"]),
    ]
    # c 在 seq1 不完整，最近完整版是 seq3，不能回到会报错的 seq1
    result = boundary.resolve_boundary("c", failed_seq=3, runs=runs)
    assert result == {"consumer": "c", "found": True,
                      "point_release_seq": 3, "reason": None}


def test_late_consumer_not_sent_to_release_without_itself():
    # 晚开始的下游只在 seq2 出现；它的恢复边界只能是 seq2，不能被甩到 seq1
    runs = [_synth_run(1, resolved=["a"]), _synth_run(2, resolved=["a", "late"])]
    found = boundary.resolve_boundary("late", failed_seq=2, runs=runs)
    assert found["point_release_seq"] == 2
    missing = boundary.resolve_boundary("late", failed_seq=1, runs=runs)
    assert missing["found"] is False
    assert missing["reason"] == "RECOVERY_NO_COMPLETE_POINT"


def test_boundary_never_exceeds_failed_release():
    runs = [_synth_run(1, resolved=["a"]), _synth_run(2, resolved=["a"])]
    result = boundary.resolve_boundary("a", failed_seq=1, runs=runs)
    assert result["point_release_seq"] == 1


def test_idempotency_key_stable_and_distinct():
    key = boundary.idempotency_key("mig-1", 1)
    assert key == boundary.idempotency_key("mig-1", 1)
    assert key != boundary.idempotency_key("mig-2", 1)
    assert key != boundary.idempotency_key("mig-1", 2)


def test_fingerprint_stable_until_structure_changes(run):
    again = dict(run)
    again["created_at"] = "1999-01-01T00:00:00+00:00"  # 易变量不影响指纹
    assert boundary.structure_fingerprint(run) == boundary.structure_fingerprint(again)
    changed = dict(run)
    changed["fields_new"] = [dict(field, type_raw="varchar(999)")
                             for field in run["fields_new"]]
    assert boundary.structure_fingerprint(run) != boundary.structure_fingerprint(changed)


def test_freeze_gate_unit():
    verdict = freeze.evaluate(None)
    assert verdict["allowed"] is False
    assert verdict["blockers"][0]["code"] == "RECOVERY_POINT_NOT_FOUND"
    blocked = freeze.evaluate({"point_id": "rp-0001", "frozen": False,
                               "fingerprint": "x", "release_seq": 1})
    assert blocked["allowed"] is False
    assert blocked["blockers"][0]["code"] == "RECOVERY_BASIS_UNFROZEN"
    allowed = freeze.evaluate({"point_id": "rp-0001", "frozen": True,
                               "fingerprint": "x", "release_seq": 1})
    assert allowed["allowed"] is True
    with pytest.raises(errors.RecoveryBlocked):
        freeze.ensure_rollback_allowed({"frozen": False})


# ---------- 启动登记与冻结 ----------

def test_startup_registers_unfrozen_point_per_release(client):
    points = store.list_recovery_points()
    assert len(points) == 1
    point = points[0]
    assert point["release_seq"] == 1
    assert point["frozen"] is False
    assert point["point_id"] == boundary.point_id_for(1)
    # 重复登记不新增恢复点
    service.ensure_points_for_history()
    assert len(store.list_recovery_points()) == 1


def test_freeze_persists_and_is_idempotent(client):
    point = store.list_recovery_points()[0]
    frozen = service.freeze_point(point["point_id"])
    assert frozen["frozen"] is True and frozen["frozen_at"]
    again = service.freeze_point(point["point_id"])
    assert again["frozen_at"] == frozen["frozen_at"]
    assert store.latest_frozen_point_seq() == 1


def test_freeze_unknown_point_404(client):
    with pytest.raises(errors.NotFound):
        service.freeze_point("rp-9999")


# ---------- 未冻结不放行 ----------

def test_freeze_gate_blocks_unfrozen_rollback(client):
    response = client.post("/api/recovery/rollbacks", json={
        "migration_id": "mig-block", "target_release_seq": 1,
        "consumers": ["dw.mv_order_daily"],
    })
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "RECOVERY_BLOCKED"
    assert body["detail"]["reason"] == "RECOVERY_BASIS_UNFROZEN"
    # 被阻断的单仍然留痕，状态 blocked，未进入执行
    order = store.rollback_order_by_key(boundary.idempotency_key("mig-block", 1))
    assert order["status"] == service.BLOCKED
    events = store.list_rollback_events(order["order_id"])
    assert events[0]["event"] == "order_opened"
    assert not any(e["event"] == "switch_views" for e in events)


def test_unknown_release_point_is_blocked(client):
    response = client.post("/api/recovery/rollbacks", json={
        "migration_id": "mig-ghost", "target_release_seq": 99,
        "consumers": ["dw.mv_order_daily"],
    })
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "RECOVERY_POINT_NOT_FOUND"


def test_bad_rollback_payload_rejected(client):
    assert client.post("/api/recovery/rollbacks",
                       json={"migration_id": "", "target_release_seq": 1,
                             "consumers": ["a"]}).status_code == 400
    assert client.post("/api/recovery/rollbacks",
                       json={"migration_id": "m", "target_release_seq": "x",
                             "consumers": ["a"]}).status_code == 400
    assert client.post("/api/recovery/rollbacks",
                       json={"migration_id": "m", "target_release_seq": 1,
                             "consumers": []}).status_code == 400


# ---------- 冻结后放行 + 幂等重试 ----------

def _freeze_first(client) -> str:
    point = store.list_recovery_points()[0]
    client.post(f"/api/recovery/points/{point['point_id']}/freeze")
    return point["point_id"]


def test_frozen_rollback_succeeds_and_records_journal(client):
    point_id = _freeze_first(client)
    response = client.post("/api/recovery/rollbacks", json={
        "migration_id": "mig-ok", "target_release_seq": 1,
        "consumers": ["dw.mv_order_daily", "dw.task_settle"],
    })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done" and body["frozen"] is True
    assert body["point_id"] == point_id
    order = store.rollback_order_by_id(body["order_id"])
    events = [e["event"] for e in store.list_rollback_events(order["order_id"])]
    for step in ("freeze_check", "boundary_resolve", "switch_views",
                 "restore_consumers", "verify_fingerprint", "order_finished"):
        assert step in events


def test_retry_same_migration_keeps_order_and_adds_no_point(client):
    _freeze_first(client)
    payload = {"migration_id": "mig-retry", "target_release_seq": 1,
               "consumers": ["dw.mv_order_daily"]}
    first = client.post("/api/recovery/rollbacks", json=payload).json()
    points_before = len(store.list_recovery_points())
    second = client.post("/api/recovery/rollbacks", json=payload).json()
    assert second["order_id"] == first["order_id"]
    assert second["attempts"] == first["attempts"]  # 已完成单重放不再加尝试次数
    assert len(store.list_recovery_points()) == points_before
    order = store.rollback_order_by_id(first["order_id"])
    assert order["idempotency_key"] == boundary.idempotency_key("mig-retry", 1)


def test_blocked_then_frozen_retry_reuses_same_order(client):
    payload = {"migration_id": "mig-late-freeze", "target_release_seq": 1,
               "consumers": ["dw.mv_order_daily"]}
    blocked = client.post("/api/recovery/rollbacks", json=payload)
    assert blocked.status_code == 409
    blocked_oid = blocked.json()["detail"]["order"]["order_id"]
    _freeze_first(client)
    ok = client.post("/api/recovery/rollbacks", json=payload).json()
    assert ok["order_id"] == blocked_oid and ok["status"] == "done"
    assert ok["attempts"] == 2


def test_concurrent_same_migration_collapses_to_one_order(client):
    _freeze_first(client)
    results: list[dict] = []
    errors_seen: list[Exception] = []

    def worker():
        try:
            order = service.request_rollback("mig-race", 1, ["dw.mv_order_daily"])
            results.append(order)
        except Exception as exc:  # pragma: no cover - 并发不应抛错
            errors_seen.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors_seen
    order_ids = {order["order_id"] for order in results}
    assert len(order_ids) == 1
    assert len(store.list_rollback_orders()) == 1


def test_concurrent_unfrozen_rollbacks_never_pass(client):
    outcomes: list[str] = []

    def worker(migration_id: str):
        order = service.request_rollback(migration_id, 1, ["dw.mv_order_daily"])
        outcomes.append(order["status"])

    threads = [threading.Thread(target=worker, args=(f"mig-cold-{i}",))
               for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes and all(status == service.BLOCKED for status in outcomes)
    # 不同迁移各自成单，且每张阻断单都留下了 blocked 事件，记录不丢
    orders = store.list_rollback_orders()
    assert len(orders) == 6
    for order in orders:
        events = [e["event"] for e in store.list_rollback_events(order["order_id"])]
        assert "order_opened" in events
        assert any(e["reason"] == "RECOVERY_BASIS_UNFROZEN" for e in
                   store.list_rollback_events(order["order_id"]))


# ---------- 共享报表结构一致性 ----------

def test_shared_report_mismatch_blocks(client):
    _freeze_first(client)
    response = client.post("/api/recovery/rollbacks", json={
        "migration_id": "mig-report", "target_release_seq": 1,
        "consumers": ["dw.task_settle"],
        "shared_reports": ["ads.rpt_pay_channel"],
    })
    assert response.status_code == 409
    codes = [b["code"] for b in response.json()["detail"]["blockers"]]
    assert "RECOVERY_SHARED_MISMATCH" in codes


def test_shared_report_consistent_when_all_refs_resolve(client, run):
    # mv_order_daily 在样例发布里全部引用可达，同次发布结构一致
    verdict = boundary.shared_report_publish_consistency("dw.mv_order_daily", run)
    assert verdict["consistent"] is True


# ---------- 中断恢复 ----------

def test_interrupt_resume_skips_done_steps_and_keeps_records(client):
    _freeze_first(client)
    order = service.request_rollback(
        "mig-interrupt", 1, ["dw.mv_order_daily", "dw.task_settle"],
        fail_after_step="switch_views",
    )
    assert order["status"] == service.INTERRUPTED
    oid = order["order_id"]
    mid_events = store.list_rollback_events(oid)
    mid_names = [e["event"] for e in mid_events]
    assert "switch_views" in mid_names and "order_finished" not in mid_names

    resumed = service.request_rollback(
        "mig-interrupt", 1, ["dw.mv_order_daily", "dw.task_settle"], resume=True)
    assert resumed["status"] == service.DONE
    assert resumed["order_id"] == oid and resumed["attempts"] == 2
    names = [e["event"] for e in store.list_rollback_events(oid)]
    for step in ("freeze_check", "boundary_resolve", "switch_views",
                 "restore_consumers", "verify_fingerprint"):
        assert names.count(step) == 1  # 已完成步骤绝不重放
    assert len(names) > len(mid_names)  # 记录只增不丢
    assert len(store.list_recovery_points()) == 1  # 恢复不新增恢复点


def test_mark_interrupted_requires_live_order(client):
    _freeze_first(client)
    order = service.request_rollback("mig-mark", 1, ["dw.mv_order_daily"])
    assert order["status"] == service.DONE
    with pytest.raises(errors.Conflict):
        service.mark_interrupted(order["order_id"])


# ---------- 页面与导出一致性 ----------

def test_page_and_export_stay_consistent(client):
    # 阻断态：页面与 blockers.csv 必须出现同一个原因码
    blocked = client.post("/api/recovery/rollbacks", json={
        "migration_id": "mig-ui", "target_release_seq": 1,
        "consumers": ["dw.mv_order_daily"],
    })
    assert blocked.status_code == 409
    oid = blocked.json()["detail"]["order"]["order_id"]
    code = blocked.json()["detail"]["reason"]
    page = client.get("/recovery").text
    blockers_csv = client.get(f"/api/export/recovery/{oid}/blockers.csv").text
    assert code in page and code in blockers_csv
    assert oid in page

    # 放行后：恢复单 MD 与页面引用同一个恢复点
    _freeze_first(client)
    client.post("/api/recovery/rollbacks", json={
        "migration_id": "mig-ui", "target_release_seq": 1,
        "consumers": ["dw.mv_order_daily"],
    })
    order = store.rollback_order_by_id(oid)
    point = store.recovery_point_by_id(order["point_id"])
    md = client.get(f"/api/export/recovery/{oid}.md").text
    assert order["point_id"] in md
    assert point["fingerprint"] in md
    assert "状态：done" in md
    events_csv = client.get(f"/api/export/recovery/{oid}/events.csv").text
    event_count = len(store.list_rollback_events(oid))
    assert len(events_csv.splitlines()) == event_count + 1  # 表头 + 事件


def test_recovery_page_renders_empty_and_seeded(client):
    page = client.get("/recovery")
    assert page.status_code == 200
    assert "恢复与回滚" in page.text
    assert "rp-0001" in page.text


def test_boundary_api_is_read_only(client):
    before = len(store.list_rollback_orders())
    response = client.get("/api/recovery/boundary",
                          params={"consumer": "dw.mv_order_daily", "failed_seq": 1})
    assert response.status_code == 200
    assert response.json()["found"] is True
    assert len(store.list_rollback_orders()) == before
