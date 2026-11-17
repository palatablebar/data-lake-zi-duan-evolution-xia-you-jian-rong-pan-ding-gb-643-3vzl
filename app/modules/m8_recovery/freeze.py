"""冻结闸门：恢复依据未冻结时不能放行回滚。

闸门只做判定，不写库；返回结构化阻断原因，页面与恢复单导出共用同一份结论。
"""

from __future__ import annotations


def evaluate(point: dict | None, *, require_frozen: bool = True) -> dict:
    """汇总一个恢复点当前的可回滚状态与阻断原因。"""
    blockers: list[dict] = []
    if point is None:
        return {
            "allowed": False,
            "frozen": False,
            "blockers": [{
                "code": "RECOVERY_POINT_NOT_FOUND",
                "detail": "恢复点不存在，无法确定恢复边界",
            }],
        }
    frozen = bool(point.get("frozen"))
    if require_frozen and not frozen:
        blockers.append({
            "code": "RECOVERY_BASIS_UNFROZEN",
            "detail": "结构指纹尚未冻结，恢复依据可能漂移，回滚不得放行",
            "point_id": point.get("point_id"),
        })
    return {
        "allowed": not blockers,
        "frozen": frozen,
        "fingerprint": point.get("fingerprint"),
        "release_seq": point.get("release_seq"),
        "blockers": blockers,
    }


def ensure_rollback_allowed(point: dict | None) -> dict:
    """供回滚编排调用：不放行时抛带原因码的领域异常。"""
    from app import errors

    verdict = evaluate(point)
    if verdict["allowed"]:
        return verdict
    first = verdict["blockers"][0]
    raise errors.RecoveryBlocked(
        first["detail"],
        detail={"reason": first["code"], "blockers": verdict["blockers"]},
    )
