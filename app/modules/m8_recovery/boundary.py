"""恢复点：发布级恢复边界的纯规则。

边界原则：
1. 恢复点挂在发布（批次）上，不挂在消费方上。共享报表只认发布级恢复点，
   因此一次回滚里它引用的对象必然来自同一次发布，不会混用两个结构。
2. 结构指纹在冻结前可漂移；冻结后指纹固定，作为回滚的唯一恢复依据。
3. 每个消费方回到「最后一个含自己且完整」的发布：发布序号不大于故障发布，
   且它在该发布快照里的全部引用都可达。
"""

from __future__ import annotations

import hashlib
import json

from app import manifest as manifest_mod

# 消费方在一次发布里的完整状态
COMPLETE = "complete"
INCOMPLETE = "incomplete"
ABSENT = "absent"


def structure_fingerprint(run: dict) -> str:
    """对一次发布的结构身份取指纹：字段身份、表名、列名、类型、必填。

    只取结构字段，不碰生成时间这类易变量，保证同结构两次发布指纹相同、
    结构一变指纹就变。字段身份按字符串保形，不走 float。
    """
    material = []
    for side in ("fields_old", "fields_new"):
        fields = run.get(side) or []
        material.append([
            {
                "field_id": str(field.get("field_id")),
                "table": field.get("table"),
                "name": field.get("name"),
                "type": field.get("type_raw") or field.get("type"),
                "required": bool(field.get("required")),
            }
            for field in sorted(fields, key=lambda item: str(item.get("field_id")))
        ])
    payload = json.dumps(material, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return manifest_mod.sha256_bytes(payload.encode("utf-8"))


def point_id_for(release_seq: int) -> str:
    return f"rp-{int(release_seq):04d}"


def consumer_state(consumer_name: str, run: dict) -> str:
    """消费方在某次发布里的状态：完整 / 不完整 / 不存在。

    完整 = 该消费方在本发布的引用解析里没有断链。
    """
    resolution = run.get("resolution") or {}
    broken_names = {item.get("consumer") for item in (resolution.get("broken") or [])}
    known_names = {item.get("consumer") for item in (resolution.get("resolved") or [])}
    known_names |= broken_names
    if consumer_name not in known_names:
        return ABSENT
    return INCOMPLETE if consumer_name in broken_names else COMPLETE


def consumer_complete_points(consumer_name: str, runs: list[dict]) -> list[int]:
    """该消费方在哪些发布序号上是完整的，序号升序。"""
    out = []
    for run in sorted(runs, key=lambda item: item["version_seq"]):
        if consumer_state(consumer_name, run) == COMPLETE:
            out.append(int(run["version_seq"]))
    return out


def resolve_boundary(consumer_name: str, failed_seq: int,
                     runs: list[dict]) -> dict:
    """定恢复边界：<= 故障发布的、最近一个该消费方完整的发布。

    找不到时给出明确阻断原因，而不是退回共用别人的回滚点。
    """
    candidates = [
        seq for seq in consumer_complete_points(consumer_name, runs)
        if seq <= int(failed_seq)
    ]
    if not candidates:
        return {
            "consumer": consumer_name,
            "found": False,
            "point_release_seq": None,
            "reason": "RECOVERY_NO_COMPLETE_POINT",
        }
    return {
        "consumer": consumer_name,
        "found": True,
        "point_release_seq": max(candidates),
        "reason": None,
    }


def shared_report_publish_consistency(report_name: str, point_run: dict) -> dict:
    """共享报表只引用同次发布：它在目标恢复点上的全部引用必须同属该发布结构。

    判定方式：报表在该发布快照里不得有断链，且每个可达引用解析到的 field_id
    都属于本次发布的新结构字段集合。任一不满足即结构混用，阻断回滚。
    """
    resolution = point_run.get("resolution") or {}
    new_ids = {str(field.get("field_id")) for field in (point_run.get("fields_new") or [])}
    broken = [item for item in (resolution.get("broken") or [])
              if item.get("consumer") == report_name]
    foreign: list[dict] = []
    for item in (resolution.get("resolved") or []):
        if item.get("consumer") != report_name:
            continue
        if item.get("field_id") is not None and str(item["field_id"]) not in new_ids:
            foreign.append(item)
    if broken or foreign:
        return {
            "report": report_name,
            "consistent": False,
            "reason": "RECOVERY_SHARED_MISMATCH",
            "broken": broken,
            "foreign_refs": foreign,
        }
    return {"report": report_name, "consistent": True, "reason": None}


def idempotency_key(migration_id: str, target_seq: int) -> str:
    """同一迁移的重试（含中断后恢复）共用一个键，因此不新增恢复点、不新增回滚单。"""
    raw = f"rollback|{migration_id}|seq={int(target_seq)}"
    return "rb-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
