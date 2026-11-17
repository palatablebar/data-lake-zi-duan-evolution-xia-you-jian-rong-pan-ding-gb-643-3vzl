"""恢复单导出：回滚单 Markdown 与阻断原因 CSV。

页面与导出共用 service 的同一份 order / point 数据，恢复点与阻断原因必须一致：
任何一边都不自行重算，避免页面显示放行、导出却写阻断这类分叉。
"""

from __future__ import annotations

import csv
import io

from app import config, reasons


def _md_table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def order_markdown(order: dict, point: dict | None) -> str:
    frozen = bool((point or {}).get("frozen"))
    blockers = order.get("blockers") or []
    boundaries = order.get("boundaries") or []
    parts = [
        f"# 恢复单 {order['order_id']}",
        "",
        f"- 迁移：{order.get('migration_id')}",
        f"- 目标恢复点：{order.get('point_id') or '—'}（发布序号 {order.get('release_seq')}）",
        f"- 结构指纹：{order.get('fingerprint') or '—'}",
        f"- 冻结态：{'已冻结' if frozen else '未冻结'}",
        f"- 状态：{order.get('status')}，尝试次数 {order.get('attempts', 1)}",
        f"- 请求时间：{order.get('requested_at')}",
        "",
        "## 1 恢复边界",
        "",
    ]
    if boundaries:
        parts.append(_md_table(
            ["消费方", "恢复发布序号", "阻断原因"],
            [[item.get("consumer"),
              item.get("point_release_seq") if item.get("found") else "—",
              reasons.label(item["reason"]) if item.get("reason") else ""]
             for item in boundaries],
        ))
    else:
        parts.append("（尚未解析边界）")
    parts.extend(["", "## 2 阻断原因", ""])
    if blockers:
        parts.append(_md_table(
            ["原因码", "说明"],
            [[item.get("code"), item.get("detail")] for item in blockers],
        ))
    else:
        parts.append("无阻断。")
    parts.extend(["", "## 3 事件流", ""])
    events = order.get("events") or []
    if events:
        parts.append(_md_table(
            ["序号", "事件", "原因码", "时间"],
            [[event.get("seq"), event.get("event"),
              event.get("reason") or "", event.get("at")] for event in events],
        ))
    else:
        parts.append("（无事件）")
    parts.extend([
        "",
        "## 4 回滚规则",
        "",
        "1. 恢复依据未冻结不得放行；冻结后结构指纹固定为唯一恢复依据。",
        "2. 共享报表只回滚到发布级恢复点，全部引用须同属一次发布。",
        "3. 每个消费方回到最后一个含自己且完整的发布，找不到即阻断、不共用他人回滚点。",
        "4. 同一迁移重试沿用原恢复点与原回滚单，不新增恢复点。",
        f"5. 生效区间沿用 {config.WINDOW_SEMANTICS} 语义。",
        "",
    ])
    return "\n".join(parts)


def blockers_csv(order: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["order_id", "point_id", "release_seq", "code", "label", "detail"])
    for item in order.get("blockers") or []:
        code = item.get("code") or ""
        writer.writerow([
            order.get("order_id"), order.get("point_id") or "",
            order.get("release_seq"), code, reasons.label(code), item.get("detail") or "",
        ])
    return buffer.getvalue()


def events_csv(order: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["seq", "order_id", "event", "reason", "at"])
    for event in order.get("events") or []:
        writer.writerow([event.get("seq"), event.get("order_id"), event.get("event"),
                         event.get("reason") or "", event.get("at")])
    return buffer.getvalue()
