"""M8 入口：恢复点 / 回滚单接口与恢复页。

页面与所有导出都从 service 读取同一份 order、point、blockers，
不在此层重算，保证恢复点与阻断原因在页面和导出件上完全一致。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app import errors, store, web
from app.modules.m8_recovery import report, service

router = APIRouter()

ALLOWED_ROLLBACK_KEYS = {
    "migration_id", "target_release_seq", "consumers", "shared_reports", "resume",
}


def _point_of(order: dict) -> dict | None:
    if not order.get("point_id"):
        return None
    return store.recovery_point_by_id(order["point_id"])


@router.get("/recovery", response_class=HTMLResponse)
def recovery_page(request: Request):
    points = service.list_points()
    orders = service.list_orders()
    detailed = []
    for order in orders:
        item = dict(order)
        item["events"] = store.list_rollback_events(order["order_id"])
        detailed.append(item)
    return web.render(
        request,
        "recovery.html",
        "recovery",
        points=points,
        orders=detailed,
        latest_frozen_seq=store.latest_frozen_point_seq(),
    )


@router.post("/api/recovery/points")
def create_points():
    """把当前批次历史登记为恢复点（幂等，已存在不新增）。"""
    points = service.ensure_points_for_history()
    return {"created": len(points), "points": service.list_points()}


@router.post("/api/recovery/points/{point_id}/freeze")
def freeze_point(point_id: str):
    point = service.freeze_point(point_id)
    return {"point_id": point["point_id"], "frozen": bool(point["frozen"]),
            "fingerprint": point["fingerprint"]}


@router.get("/api/recovery/points")
def points_api():
    return {"latest_frozen_seq": store.latest_frozen_point_seq(),
            "points": service.list_points()}


@router.get("/api/recovery/boundary")
def boundary_api(consumer: str, failed_seq: int):
    """纯查询：某消费方相对故障发布的恢复边界（不改任何状态）。"""
    from app.modules.m8_recovery import boundary

    runs = [store.run_by_id(summary["run_id"]) for summary in store.list_runs(200)]
    runs = [run for run in runs if run]
    result = boundary.resolve_boundary(consumer, failed_seq, runs)
    return result


@router.post("/api/recovery/rollbacks")
def create_rollback(payload: dict | None = None):
    body = payload or {}
    unknown = set(body) - ALLOWED_ROLLBACK_KEYS
    if unknown:
        raise errors.BadInput("出现未知参数", detail={"keys": sorted(unknown)})
    migration_id = str(body.get("migration_id") or "").strip()
    if not migration_id:
        raise errors.BadInput("migration_id 不能为空", detail={"field": "migration_id"})
    try:
        target_seq = int(body.get("target_release_seq"))
    except (TypeError, ValueError):
        raise errors.BadInput("target_release_seq 必须是整数",
                              detail={"field": "target_release_seq"})
    consumers = body.get("consumers") or []
    if not isinstance(consumers, list) or not all(isinstance(x, str) for x in consumers):
        raise errors.BadInput("consumers 必须是字符串数组", detail={"field": "consumers"})
    shared = body.get("shared_reports") or []
    if not isinstance(shared, list) or not all(isinstance(x, str) for x in shared):
        raise errors.BadInput("shared_reports 必须是字符串数组",
                              detail={"field": "shared_reports"})
    order = service.request_rollback(
        migration_id, target_seq, consumers, shared,
        resume=bool(body.get("resume", False)),
    )
    body_out = {
        "order_id": order["order_id"],
        "status": order["status"],
        "point_id": order.get("point_id"),
        "frozen": bool((_point_of(order) or {}).get("frozen")),
        "attempts": order.get("attempts", 1),
        "blockers": order.get("blockers") or [],
        "boundaries": order.get("boundaries") or [],
    }
    if order["status"] == service.BLOCKED and order.get("blockers"):
        # 不放行：单据与事件已留痕，HTTP 层按 409 显式拒绝
        first = order["blockers"][0]
        raise errors.RecoveryBlocked(
            first.get("detail", "回滚被阻断"),
            detail={"reason": first.get("code"), "order": body_out,
                    "blockers": order["blockers"]},
        )
    return body_out


@router.post("/api/recovery/rollbacks/{order_id}/interrupt")
def interrupt_rollback(order_id: str):
    order = service.mark_interrupted(order_id)
    return {"order_id": order_id, "status": order["status"]}


@router.get("/api/recovery/rollbacks")
def rollbacks_api():
    return {"orders": service.list_orders()}


@router.get("/api/recovery/rollbacks/{order_id}")
def rollback_detail(order_id: str):
    return service.get_order(order_id)


@router.get("/api/export/recovery/{order_id}.md")
def export_recovery_md(order_id: str):
    order = service.get_order(order_id)
    point = _point_of(order)
    return Response(
        content=report.order_markdown(order, point),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{order_id}.md"'},
    )


@router.get("/api/export/recovery/{order_id}/blockers.csv")
def export_recovery_blockers(order_id: str):
    order = service.get_order(order_id)
    return Response(
        content=report.blockers_csv(order),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{order_id}-blockers.csv"'},
    )


@router.get("/api/export/recovery/{order_id}/events.csv")
def export_recovery_events(order_id: str):
    order = service.get_order(order_id)
    return Response(
        content=report.events_csv(order),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{order_id}-events.csv"'},
    )
