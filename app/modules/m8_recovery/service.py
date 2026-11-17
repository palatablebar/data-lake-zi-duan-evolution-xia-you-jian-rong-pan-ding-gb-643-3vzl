"""回滚编排：冻结恢复点、定边界、回滚单、事件流、中断恢复。

并发约束：
- 迁移与回滚并发到达时，靠进程内互斥 + 恢复点冻结态串行化，未冻结一律不放行。
- 回滚单以幂等键去重：同一迁移的重试拿到同一张单、同一个恢复点，不新增恢复点。
- 事件只追加不改写；中断后凭原单从最后一条事件继续，已完成的步骤不重放、记录不丢。
"""

from __future__ import annotations

import threading

from app import config, errors, store
from app.modules.m8_recovery import boundary, freeze

# 回滚单生命周期
PENDING = "pending"
RUNNING = "running"
INTERRUPTED = "interrupted"
DONE = "done"
BLOCKED = "blocked"

# 一张回滚单要顺序走完的步骤；中断恢复时跳过已记录步骤
STEPS = ("freeze_check", "boundary_resolve", "switch_views", "restore_consumers",
         "verify_fingerprint")

_GUARD = threading.Lock()


# ---------- 恢复点登记与冻结 ----------

def register_point_from_run(run: dict) -> dict:
    """一次发布对应一个恢复点；重复登记同一发布不新增行。"""
    seq = int(run["version_seq"])
    existing = store.recovery_point_by_seq(seq)
    if existing is not None:
        return existing
    consumers = sorted({
        item.get("consumer")
        for item in (run.get("resolution") or {}).get("resolved", [])
    } | {
        item.get("consumer")
        for item in (run.get("resolution") or {}).get("broken", [])
    })
    point = {
        "point_id": boundary.point_id_for(seq),
        "release_seq": seq,
        "run_id": run["run_id"],
        "schema_id": run["new_schema_id"],
        "fingerprint": boundary.structure_fingerprint(run),
        "frozen": False,
        "frozen_at": None,
        "created_at": config.now_utc().isoformat(),
        "consumers": consumers,
    }
    store.save_recovery_point(point)
    return store.recovery_point_by_seq(seq)


def freeze_point(point_id: str) -> dict:
    point = store.recovery_point_by_id(point_id)
    if point is None:
        raise errors.NotFound("恢复点不存在", detail={"point_id": point_id})
    if not point.get("frozen"):
        store.mark_recovery_frozen(point_id, config.now_utc().isoformat())
        point = store.recovery_point_by_id(point_id)
    return point


def ensure_points_for_history() -> list[dict]:
    """把已有批次补登成恢复点（启动 / 演示用），已存在的不重复。"""
    runs = [run for run in
            (store.run_by_id(summary["run_id"]) for summary in store.list_runs(200))
            if run]
    ordered = sorted(runs, key=lambda run: run["version_seq"])
    return [register_point_from_run(run) for run in ordered]


# ---------- 回滚单 ----------

def _record(order_id: str, event: str, reason: str | None, detail: dict | None = None) -> None:
    store.append_rollback_event(order_id, event, reason,
                                config.now_utc().isoformat(), detail)


def _persist(order: dict) -> dict:
    store.save_rollback_order(order)
    return store.rollback_order_by_id(order["order_id"])


def request_rollback(migration_id: str, target_release_seq: int,
                     consumer_names: list[str], shared_reports: list[str] | None = None,
                     *, resume: bool = False,
                     fail_after_step: str | None = None) -> dict:
    """请求一次回滚；同一 migration_id 重试返回同一张单，不产生新恢复点。

    resume=True 表示中断后的恢复调用，语义与重试相同（共用幂等键）。
    fail_after_step 仅用于故障演练：在该步骤落事件后把单置为中断并停止。
    """
    consumer_names = sorted({name for name in consumer_names if name})
    if not consumer_names:
        raise errors.BadInput("回滚必须指定至少一个消费方", detail={"field": "consumers"})
    shared_reports = sorted({name for name in (shared_reports or []) if name})
    key = boundary.idempotency_key(migration_id, target_release_seq)

    with _GUARD:
        existing = store.rollback_order_by_key(key)
        if existing is not None:
            return _resume_or_return(existing, consumer_names, shared_reports, resume,
                                     fail_after_step)
        return _open_new_order(key, migration_id, target_release_seq,
                               consumer_names, shared_reports, fail_after_step)


def _open_new_order(key: str, migration_id: str, target_seq: int,
                    consumer_names: list[str], shared_reports: list[str],
                    fail_after_step: str | None = None) -> dict:
    point = store.recovery_point_by_seq(target_seq)
    gate = freeze.evaluate(point)
    order_id = f"ro-{target_seq:04d}-{key[3:11]}"
    order = {
        "order_id": order_id,
        "idempotency_key": key,
        "migration_id": migration_id,
        "point_id": point["point_id"] if point else None,
        "release_seq": target_seq,
        "fingerprint": (point or {}).get("fingerprint"),
        "status": BLOCKED,
        "requested_at": config.now_utc().isoformat(),
        "finished_at": None,
        "attempts": 1,
        "consumers": consumer_names,
        "shared_reports": shared_reports,
        "blockers": list(gate["blockers"]),
        "boundaries": [],
    }
    _record(order_id, "order_opened", None,
            {"migration_id": migration_id, "target_release_seq": target_seq})

    if not gate["allowed"]:
        # 恢复依据未冻结：不进入执行，事件留痕，不放行
        for blocker in gate["blockers"]:
            _record(order_id, "blocked", blocker["code"], blocker)
        return _persist(order)

    return _drive(order, point, consumer_names, shared_reports, fail_after_step)


def _resume_or_return(existing: dict, consumer_names: list[str],
                      shared_reports: list[str], resume: bool,
                      fail_after_step: str | None = None) -> dict:
    """幂等返回：已结束的单原样返回；中断单可恢复，沿用原恢复点，attempts +1。"""
    order_id = existing["order_id"]
    if existing["status"] == DONE:
        _record(order_id, "rollback_replayed", "ROLLBACK_ALREADY_FINISHED",
                {"resume": resume})
        return existing

    # 强制重读，避免拿到登记时的未冻结缓存对象
    point = store.recovery_point_by_id(existing["point_id"]) if existing.get("point_id") else None
    gate = freeze.evaluate(point)
    if not gate["allowed"]:
        # 恢复点被解冻等异常：保持阻断并留痕，绝不放行
        existing["blockers"] = list(gate["blockers"])
        existing["status"] = BLOCKED
        for blocker in gate["blockers"]:
            _record(order_id, "blocked", blocker["code"], blocker)
        return _persist(existing)

    existing["attempts"] = int(existing.get("attempts", 1)) + 1
    existing["consumers"] = sorted(set(existing.get("consumers") or []) | set(consumer_names))
    existing["shared_reports"] = sorted(
        set(existing.get("shared_reports") or []) | set(shared_reports))
    reason = "ROLLBACK_INTERRUPTED" if existing["status"] == INTERRUPTED \
        else "ROLLBACK_RETRY_SAME_POINT"
    _record(order_id, "rollback_resumed", reason,
            {"attempt": existing["attempts"], "resume": resume})
    return _drive(existing, point, existing["consumers"], existing["shared_reports"],
                  fail_after_step)


def _drive(order: dict, point: dict, consumer_names: list[str],
           shared_reports: list[str], fail_after_step: str | None = None) -> dict:
    """驱动回滚步骤；每步先看事件流，已完成的不重放（断点续跑）。"""
    order_id = order["order_id"]
    done_events = {event["event"] for event in store.list_rollback_events(order_id)}
    order["status"] = RUNNING
    order["blockers"] = []
    _persist(order)

    runs = [store.run_by_id(summary["run_id"]) for summary in store.list_runs(200)]
    runs = [run for run in runs if run]
    point_run = next((run for run in runs if run["version_seq"] == point["release_seq"]), None)

    try:
        if "freeze_check" not in done_events:
            freeze.ensure_rollback_allowed(point)
            _record(order_id, "freeze_check", "RECOVERY_POINT_FROZEN",
                    {"fingerprint": point["fingerprint"]})

        boundaries = order.get("boundaries") or []
        if "boundary_resolve" not in done_events:
            boundaries = _resolve_all(consumer_names, point, runs)
            order["boundaries"] = boundaries
            blockers = [
                {"code": item["reason"], "detail": f"消费方 {item['consumer']} 无完整恢复点"}
                for item in boundaries if not item["found"]
            ]
            if blockers:
                order["blockers"] = blockers
                order["status"] = BLOCKED
                for blocker in blockers:
                    _record(order_id, "blocked", blocker["code"], blocker)
                return _persist(order)
            _record(order_id, "boundary_resolve", None,
                    {"boundaries": boundaries})
        elif not boundaries:
            # 续跑时单据缺边界（旧数据 / 边缘情况）：只读重算，结论确定且不改事件流
            boundaries = _resolve_all(consumer_names, point, runs)
            order["boundaries"] = boundaries
            _persist(order)

        if "shared_check" not in done_events:
            shared_blockers = _shared_check(shared_reports, point_run)
            if shared_blockers:
                order["blockers"] = shared_blockers
                order["status"] = BLOCKED
                for blocker in shared_blockers:
                    _record(order_id, "blocked", blocker["code"], blocker)
                return _persist(order)
            if shared_reports:
                _record(order_id, "shared_check", None,
                        {"reports": shared_reports})

        for step in ("switch_views", "restore_consumers", "verify_fingerprint"):
            if step in done_events:
                continue
            order["status"] = RUNNING
            _persist(order)
            detail = _simulate_step(step, point, boundaries, consumer_names)
            _record(order_id, step, None, detail)
            if fail_after_step == step:
                order["status"] = INTERRUPTED
                _record(order_id, "rollback_interrupted", "ROLLBACK_INTERRUPTED",
                        {"fail_after_step": step})
                return _persist(order)

        order["status"] = DONE
        order["finished_at"] = config.now_utc().isoformat()
        order["blockers"] = []
        _record(order_id, "order_finished", None, {"fingerprint": point["fingerprint"]})
        return _persist(order)
    except errors.RecoveryBlocked as exc:
        order["status"] = BLOCKED
        order["blockers"] = exc.detail.get("blockers") or [
            {"code": exc.detail.get("reason", "RECOVERY_BASIS_UNFROZEN"),
             "detail": exc.message}]
        for blocker in order["blockers"]:
            _record(order_id, "blocked", blocker.get("code"), blocker)
        return _persist(order)


def mark_interrupted(order_id: str) -> dict:
    """测试 / 故障注入：把执行中的单标记为中断，验证可恢复且记录不丢。"""
    order = store.rollback_order_by_id(order_id)
    if order is None:
        raise errors.NotFound("回滚单不存在", detail={"order_id": order_id})
    if order["status"] == DONE:
        raise errors.Conflict("已结束的回滚单不能标记中断",
                              detail={"order_id": order_id})
    order["status"] = INTERRUPTED
    _record(order_id, "rollback_interrupted", "ROLLBACK_INTERRUPTED", None)
    return _persist(order)


def _resolve_all(consumer_names: list[str], point: dict, runs: list[dict]) -> list[dict]:
    return [
        boundary.resolve_boundary(name, point["release_seq"], runs)
        for name in consumer_names
    ]


def _shared_check(shared_reports: list[str], point_run: dict | None) -> list[dict]:
    if point_run is None:
        return [{"code": "RECOVERY_POINT_NOT_FOUND", "detail": "目标发布批次缺失"}]
    blockers = []
    for report in shared_reports:
        verdict = boundary.shared_report_publish_consistency(report, point_run)
        if not verdict["consistent"]:
            blockers.append({
                "code": verdict["reason"],
                "detail": f"共享报表 {report} 引用跨越发布，结构混用",
                "report": report,
            })
    return blockers


def _simulate_step(step: str, point: dict, boundaries: list[dict],
                   consumer_names: list[str]) -> dict:
    """本服务不直连计算引擎，这里给出可核对的步骤产物，真实切换由执行器消费。"""
    if step == "switch_views":
        return {"to_release_seq": point["release_seq"], "point_id": point["point_id"]}
    if step == "restore_consumers":
        return {
            "consumers": consumer_names,
            "points": [item["point_release_seq"] for item in boundaries],
        }
    if step == "verify_fingerprint":
        return {"expected_fingerprint": point["fingerprint"], "matched": True}
    return {}


# ---------- 读取 ----------

def get_order(order_id: str) -> dict:
    order = store.rollback_order_by_id(order_id)
    if order is None:
        raise errors.NotFound("回滚单不存在", detail={"order_id": order_id})
    order["events"] = store.list_rollback_events(order_id)
    return order


def list_orders() -> list[dict]:
    return store.list_rollback_orders()


def list_points() -> list[dict]:
    return store.list_recovery_points()
