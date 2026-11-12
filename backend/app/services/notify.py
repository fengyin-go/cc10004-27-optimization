"""通知分发业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

import time
from typing import Any

from app.store import store

MODULE = "notify"
REQUIRED_FIELDS = ["规则编号", "触发条件", "通知渠道"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用通知": "已启用", "停用通知": "已停用"}
NEGATIVE_ACTIONS = ["停用通知"]


class NotifyService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"通知规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于通知分发可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS

        # 规则停用/启用与投递台账联动：停用会成为未发送投递的阻断原因，
        # 重新启用后阻断解除，操作员可通过「恢复续发」继续未完成部分。
        # 延迟导入，避免服务模块之间的循环依赖。
        from app.services import notify_domain as domain
        from app.services.delivery import delivery_service

        rule_no = entry.get("规则编号")
        for delivery in store.rows("notify_deliveries"):
            if delivery.get("rule_no") != rule_no:
                continue
            if target == "已停用" and delivery.get("status") == domain.DELIVERY_NOT_SENT:
                delivery["block_reason"] = domain.BLOCK_RULE_DISABLED
                delivery["block_reason_label"] = domain.block_label(domain.BLOCK_RULE_DISABLED)
                delivery.setdefault("history", []).append({
                    "at": int(time.time()),
                    "action": "规则停用联动阻断",
                    "detail": f"规则 {rule_no} 已停用，投递暂停，恢复启用后可续发",
                })
                delivery_service._sync_event_consumption(  # noqa: SLF001 - 同域服务联动
                    int(delivery["event_id"]), action="规则停用", detail="投递被规则停用阻断"
                )
            elif target == "已启用" and delivery.get("block_reason") == domain.BLOCK_RULE_DISABLED:
                delivery["block_reason"] = None
                delivery["block_reason_label"] = ""
                delivery.setdefault("history", []).append({
                    "at": int(time.time()),
                    "action": "规则启用解除阻断",
                    "detail": f"规则 {rule_no} 已恢复启用，可继续未完成投递",
                })
                delivery_service._sync_event_consumption(  # noqa: SLF001
                    int(delivery["event_id"]), action="规则启用", detail="投递阻断解除，等待续发"
                )
        return entry, f"通知规则已{action}"
