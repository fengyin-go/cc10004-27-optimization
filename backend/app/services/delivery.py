"""通知投递服务：四态状态机、回执关联、冲突裁决与幂等重试都收在这里。

与旧版只做规则启停的区别：
- 每次通知对应一条投递台账（delivery），结果按四态记录，不再只有成功/失败；
- 连接超时是「结果不确定」，落到等待确认，绝不自动重试；
- 回执按 delivery_id 关联，乱序、重复只更新同一条投递；带签名回执是可验证证据，
  与人工确认冲突时以证据为准，但所有结论变更都留痕；
- 重试复用同一投递项并递增 attempt，不会产生第二次相同投递；
- 投递结论实时同步到异常检测事件的消费状态，等待确认不会把原事件标成已处理。
"""
from __future__ import annotations

import time
from typing import Any

from app.services import notify_domain as domain
from app.services.local_receiver import (
    DEMO_SIGNING_SECRET,
    ReceiverTimeout,
    local_receiver,
    verify_receipt,
)
from app.store import (
    TABLE_ANOMALY_EVENTS,
    TABLE_DELIVERIES,
    TABLE_RECEIPTS,
    TABLE_UNMATCHED_RECEIPTS,
    store,
)


def _now() -> int:
    return int(time.time())


class DeliveryService:
    # ------------------------------------------------------------------ 事件
    def list_events(self, *, consumption: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(TABLE_ANOMALY_EVENTS)
        if consumption:
            rows = [row for row in rows if row.get("consumption") == consumption]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        return store.find(TABLE_ANOMALY_EVENTS, event_id)

    def create_event(self, values: dict[str, Any]) -> dict[str, Any]:
        """登记一条异常检测事件（演示里由操作员手动触发，真实链路来自检测模块）。"""
        event = {
            "event_no": str(values.get("event_no") or "").strip() or f"EVT-{store.next_id(TABLE_ANOMALY_EVENTS):04d}",
            "source_rule": str(values.get("source_rule") or "").strip(),
            "metric": str(values.get("metric") or "演示指标").strip(),
            "level": str(values.get("level") or "P2").strip(),
            "summary": str(values.get("summary") or "异常检测演示事件").strip(),
            "detected_at": _now(),
            "consumption": domain.CONSUMPTION_PENDING,
            "history": [{"at": _now(), "action": "事件登记", "detail": "等待通知分发"}],
        }
        store.insert(TABLE_ANOMALY_EVENTS, event)
        return event

    def _sync_event_consumption(self, event_id: int, *, action: str, detail: str) -> None:
        """投递结论跨模块同步：事件消费状态只能由投递台账的确定程度推导。"""
        event = store.find(TABLE_ANOMALY_EVENTS, event_id)
        if event is None:
            return
        deliveries = store.filter_by(TABLE_DELIVERIES, "event_id", event_id)
        if not deliveries:
            event["consumption"] = domain.CONSUMPTION_PENDING
        elif any(item["status"] == domain.DELIVERY_PENDING for item in deliveries):
            # 关键规则：只要还有等待确认的在途投递，原检测事件就不能算已处理
            event["consumption"] = domain.CONSUMPTION_WAITING
        elif any(item["status"] == domain.DELIVERY_NOT_SENT for item in deliveries):
            event["consumption"] = domain.CONSUMPTION_STALLED
        else:
            # 剩余投递全部处于拒收/送达等终态，事件闭环
            event["consumption"] = domain.CONSUMPTION_DONE
        event.setdefault("history", []).append({"at": _now(), "action": action, "detail": detail})

    # --------------------------------------------------------------- 投递台账
    def list_deliveries(
        self,
        *,
        status: str | None = None,
        keyword: str | None = None,
        legacy_only: bool = False,
    ) -> list[dict[str, Any]]:
        rows = store.rows(TABLE_DELIVERIES)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if legacy_only:
            rows = [row for row in rows if row.get("legacy")]
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("delivery_no", ""))
                or keyword in str(row.get("事件摘要", ""))
                or keyword in str(row.get("channel", ""))
            ]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    def delivery_stats(self) -> dict[str, int]:
        rows = store.rows(TABLE_DELIVERIES)
        stats = {label: 0 for label in domain.DELIVERY_STATUS_LABELS.values()}
        stats["历史待核对"] = 0
        for row in rows:
            stats[domain.status_label(str(row.get("status")))] += 1
            if row.get("legacy") and row.get("status") != domain.DELIVERY_DELIVERED:
                stats["历史待核对"] += 1
        return stats

    def get_delivery(self, delivery_id: int) -> dict[str, Any] | None:
        return store.find(TABLE_DELIVERIES, delivery_id)

    def _find_by_delivery_no(self, delivery_no: str) -> dict[str, Any] | None:
        return store.find_by(TABLE_DELIVERIES, "delivery_no", delivery_no)

    def create_delivery(self, event_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """为异常事件建立一条投递台账；同一事件+渠道只允许一条未终态投递（幂等）。"""
        event = self.get_event(event_id)
        if event is None:
            return None, f"异常事件 {event_id} 不存在，无法发起通知"
        channel = str(values.get("channel") or "").strip()
        if not channel:
            return None, "通知渠道不能为空"
        rule_id = values.get("rule_id")
        rule = store.find("notify", int(rule_id)) if rule_id else None
        # 同事件同渠道已有在途/未终态投递时，直接返回原投递而不是新建第二条
        for existing in store.filter_by(TABLE_DELIVERIES, "event_id", event_id):
            if existing.get("channel") == channel and existing.get("status") not in domain.TERMINAL_STATUSES:
                return existing, "该事件在此渠道已有未完成投递，已复用原投递项"
        delivery = {
            "delivery_no": f"DLV-{store.next_id(TABLE_DELIVERIES):05d}",
            "event_id": event_id,
            "event_no": event["event_no"],
            "rule_id": rule["id"] if rule else None,
            "rule_no": rule.get("规则编号") if rule else None,
            "channel": channel,
            "recipient": str(values.get("recipient") or "值班组").strip(),
            "事件摘要": event["summary"],
            "status": domain.DELIVERY_NOT_SENT,
            "status_label": domain.status_label(domain.DELIVERY_NOT_SENT),
            "attempt": 0,
            "legacy": False,
            "block_reason": None,
            "block_reason_label": "",
            "evidence_level": None,
            "created_at": _now(),
            "updated_at": _now(),
            "history": [{"at": _now(), "action": "建立投递台账", "detail": f"关联事件 {event['event_no']}"}],
        }
        store.insert(TABLE_DELIVERIES, delivery)
        self._sync_event_consumption(event_id, action="建立投递", detail=f"投递 {delivery['delivery_no']} 待发送")
        return delivery, "投递台账已建立"

    def list_receipts(self, delivery_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(TABLE_RECEIPTS)
        if delivery_id is not None:
            rows = [row for row in rows if row.get("delivery_id") == delivery_id]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def list_unmatched_receipts(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(TABLE_UNMATCHED_RECEIPTS)]

    # ------------------------------------------------------------- 发起/重试
    def dispatch(self, delivery_id: int, *, operator: str = "值班员") -> tuple[dict[str, Any] | None, str]:
        delivery = self.get_delivery(delivery_id)
        if delivery is None:
            return None, f"投递 {delivery_id} 不存在或已归档"

        # 历史记录：没有投递标识、证据链不完整，必须先人工核对
        if delivery.get("legacy") and not delivery.get("legacy_verified"):
            return self._blocked(delivery, domain.BLOCK_LEGACY_UNVERIFIED,
                                 "历史记录缺少投递标识，请先核对原始事件再决定重发")
        # 终态不允许再发
        if delivery["status"] in domain.TERMINAL_STATUSES:
            return self._blocked(delivery, domain.BLOCK_TERMINAL,
                                 f"投递已是「{domain.status_label(delivery['status'])}」终态，无需再次发送")
        # 等待确认：结果不确定，禁止无脑重试
        if delivery["status"] == domain.DELIVERY_PENDING:
            return self._blocked(delivery, domain.BLOCK_WAIT_ACK,
                                 "连接超时、结果尚不确定：请等待回执或人工核对后再处理，禁止直接重试")

        rule = store.find("notify", int(delivery["rule_id"])) if delivery.get("rule_id") else None
        if rule is not None and rule.get("status") == "已停用":
            return self._blocked(delivery, domain.BLOCK_RULE_DISABLED,
                                 f"通知规则 {rule.get('规则编号')} 已停用，恢复启用后可续发")
        receiver_status = local_receiver.status()
        if not receiver_status["online"]:
            return self._blocked(delivery, domain.BLOCK_RECEIVER_OFFLINE,
                                 "本地接收端离线，恢复在线后可继续未完成投递")
        if delivery["channel"] not in receiver_status["channels"]:
            return self._blocked(delivery, domain.BLOCK_CHANNEL_UNKNOWN,
                                 f"渠道 {delivery['channel']} 未在本地接收端登记")

        delivery["attempt"] = int(delivery.get("attempt", 0)) + 1
        delivery["updated_at"] = _now()
        try:
            receipt = local_receiver.send(
                delivery["delivery_no"],
                delivery["channel"],
                {"事件摘要": delivery.get("事件摘要"), "event_no": delivery.get("event_no")},
            )
        except ReceiverTimeout as exc:
            # 关键口径：超时 != 未送达。进入等待确认，保留尝试次数，不做自动重试
            delivery["status"] = domain.DELIVERY_PENDING
            delivery["status_label"] = domain.status_label(domain.DELIVERY_PENDING)
            delivery["block_reason"] = domain.BLOCK_WAIT_ACK
            delivery["block_reason_label"] = domain.block_label(domain.BLOCK_WAIT_ACK)
            delivery["history"].append({
                "at": _now(),
                "action": f"第 {delivery['attempt']} 次投递：连接超时",
                "detail": f"{exc}；结果不确定，转入等待确认，不自动重试",
                "operator": operator,
            })
            self._sync_event_consumption(delivery["event_id"], action="投递超时",
                                         detail=f"{delivery['delivery_no']} 等待确认")
            return delivery, "连接超时，结果不确定：已转入等待确认，不会自动重试"

        # 收到即时回执（接收端正常/拒收模式），走统一的回执关联入口
        self._accept_receipt(receipt, source="即时回执", operator=operator)
        delivery = self.get_delivery(delivery_id)
        assert delivery is not None
        return delivery, f"第 {delivery['attempt']} 次投递完成，结论：{delivery['status_label']}"

    def retry(self, delivery_id: int, *, operator: str = "值班员", acknowledge: bool = False) -> tuple[dict[str, Any] | None, str]:
        """操作员核对后的重发：只复用原投递项，不生成第二条相同投递。"""
        delivery = self.get_delivery(delivery_id)
        if delivery is None:
            return None, f"投递 {delivery_id} 不存在或已归档"
        if delivery.get("legacy") and not delivery.get("legacy_verified"):
            return self._blocked(delivery, domain.BLOCK_LEGACY_UNVERIFIED,
                                 "历史记录尚未核对完成，不能重发")
        if delivery["status"] == domain.DELIVERY_DELIVERED:
            return self._blocked(delivery, domain.BLOCK_TERMINAL, "已确认送达，不能重复发送")
        if delivery["status"] == domain.DELIVERY_PENDING and not acknowledge:
            return self._blocked(
                delivery, domain.BLOCK_WAIT_ACK,
                "该投递仍在等待确认：请先核对对端是否实际收到，确认需要重发时再勾选确认",
            )
        if delivery["status"] == domain.DELIVERY_REJECTED:
            # 拒收是确定否定结论：操作员核对后重发，先复位再走同一条投递链路
            delivery["history"].append({
                "at": _now(),
                "action": "拒收后人工核对重发",
                "detail": "复用原投递项重发，不生成第二条相同投递",
                "operator": operator,
            })
            delivery["status"] = domain.DELIVERY_NOT_SENT
            delivery["status_label"] = domain.status_label(domain.DELIVERY_NOT_SENT)
            delivery["block_reason"] = None
            delivery["block_reason_label"] = ""
        if delivery["status"] == domain.DELIVERY_PENDING and acknowledge:
            # 操作员明确核对后要求重发：记录人工裁决，仍复用同一 delivery_no
            delivery["history"].append({
                "at": _now(),
                "action": "人工核对后允许重发",
                "detail": "等待确认期间经操作员核对，确认对端未收到，允许复用原投递项重发",
                "operator": operator,
            })
            # 复位到未发送并清除等待确认阻断，dispatch 才能重新走发送链路；
            # 台账仍是同一条，attempt 递增，不产生第二次相同投递。
            delivery["status"] = domain.DELIVERY_NOT_SENT
            delivery["status_label"] = domain.status_label(domain.DELIVERY_NOT_SENT)
            delivery["block_reason"] = None
            delivery["block_reason_label"] = ""
        return self.dispatch(delivery_id, operator=operator)

    def resume(self, *, operator: str = "值班员") -> dict[str, Any]:
        """恢复续发：接收端/规则恢复后，只继续「未发送且非历史待核对」的未完成部分。

        等待确认的投递不在自动续发范围内——超时不能被当作未送达重试；
        它们要等回执，或由操作员显式核对后重发。
        """
        resumed, skipped, failed = [], [], []
        for delivery in self.list_deliveries():
            if delivery["status"] != domain.DELIVERY_NOT_SENT:
                continue
            if delivery.get("legacy") and not delivery.get("legacy_verified"):
                skipped.append({"delivery_no": delivery["delivery_no"], "reason": "历史记录待人工核对"})
                continue
            item, message = self.dispatch(int(delivery["id"]), operator=operator)
            if item is None:
                failed.append({"delivery_no": delivery["delivery_no"], "reason": message})
            elif item["status"] == domain.DELIVERY_NOT_SENT:
                skipped.append({"delivery_no": delivery["delivery_no"], "reason": item.get("block_reason_label") or message})
            else:
                resumed.append({"delivery_no": delivery["delivery_no"], "status": item["status_label"], "message": message})
        return {"resumed": resumed, "skipped": skipped, "failed": failed}

    # ---------------------------------------------------------------- 回执
    def ingest_receipt(
        self,
        delivery_no: str,
        result: str,
        signature: str,
        *,
        issued_at: int | None = None,
        note: str = "",
        source: str = "外部回执",
        operator: str = "系统",
    ) -> tuple[dict[str, Any] | None, str]:
        """回执关联入口：签名校验 → 按 delivery_no 定位唯一投递项 → 裁决并留痕。"""
        receipt = {
            "delivery_no": delivery_no,
            "result": result,
            "signature": signature,
            "issued_at": issued_at or _now(),
            "received_at": _now(),
            "note": note,
            "source": source,
            "operator": operator,
            "valid_signature": verify_receipt(delivery_no, result, signature, secret=DEMO_SIGNING_SECRET),
        }
        delivery = self._find_by_delivery_no(delivery_no)
        if delivery is None:
            # 关联不上的回执（含历史无标识场景可能收到的回执）单独落账，不静默丢弃
            receipt["reason"] = "找不到对应投递项，可能来自历史无投递标识记录"
            store.insert(TABLE_UNMATCHED_RECEIPTS, receipt)
            return None, "回执关联不到任何投递项，已记入未关联回执清单，请人工核对"
        return self._accept_receipt(receipt, source=source, operator=operator)

    def _accept_receipt(self, receipt: dict[str, Any], *, source: str, operator: str) -> tuple[dict[str, Any], str]:
        delivery = self._find_by_delivery_no(str(receipt["delivery_id"] if "delivery_id" in receipt else receipt["delivery_no"]))
        assert delivery is not None
        result = str(receipt["result"])
        valid = bool(receipt.get("valid_signature", True))
        target_status = domain.DELIVERY_DELIVERED if result == domain.RECEIPT_ACCEPTED else domain.DELIVERY_REJECTED

        stored_receipt = dict(receipt)
        stored_receipt["delivery_id"] = int(delivery["id"])
        stored_receipt.setdefault("delivery_no", delivery["delivery_no"])
        stored_receipt["source"] = source
        # 重复回执：同一投递项 + 同一结论 + 同一签名只落一条
        duplicate = any(
            existing.get("signature") == stored_receipt.get("signature")
            and existing.get("result") == result
            for existing in self.list_receipts(int(delivery["id"]))
        )
        if not duplicate:
            store.insert(TABLE_RECEIPTS, stored_receipt)

        if not valid:
            delivery["history"].append({
                "at": _now(),
                "action": "回执签名校验失败",
                "detail": f"来自{source}的 {result} 回执未通过 HMAC 校验，不作为送达证据，保持 {delivery['status_label']}",
                "operator": operator,
            })
            return delivery, "回执签名无效，已留痕但不改变投递结论"

        # 可验证证据与人工确认冲突：以证据为准，人工结论降级为留痕
        conflict = delivery.get("evidence_level") == domain.EVIDENCE_MANUAL and delivery["status"] != target_status
        if delivery["status"] == target_status:
            message = "重复/乱序回执：结论一致，仅补充回执记录，投递项不重复变更"
        elif delivery["status"] in domain.TERMINAL_STATUSES and not conflict:
            # 已有人工终态且与证据一致的情况上面已处理；到这里是不同终态但非人工证据——
            # 正常不会发生，保守起见仍以带签名证据为准并留痕
            delivery["history"].append({
                "at": _now(),
                "action": "终态被可验证证据修正",
                "detail": f"{delivery['status_label']} → {domain.status_label(target_status)}",
                "operator": operator,
            })
            message = "收到更强证据，投递结论已修正"
        else:
            if conflict:
                delivery["history"].append({
                    "at": _now(),
                    "action": "人工确认与迟到回执冲突",
                    "detail": (
                        f"人工结论为「{delivery['status_label']}」，带签名回执证明"
                        f"「{domain.status_label(target_status)}」，以可验证送达证据为准"
                    ),
                    "operator": operator,
                })
                message = "人工确认与回执冲突，已按可验证证据更正，并保留冲突留痕"
            else:
                message = f"回执已关联，投递结论更新为「{domain.status_label(target_status)}」"

        delivery["status"] = target_status
        delivery["status_label"] = domain.status_label(target_status)
        delivery["evidence_level"] = domain.EVIDENCE_RECEIPT
        delivery["block_reason"] = None
        delivery["block_reason_label"] = ""
        delivery["updated_at"] = _now()
        delivery["history"].append({
            "at": _now(),
            "action": f"关联{source}",
            "detail": f"{domain.RECEIPT_RESULT_LABELS.get(result, result)}（签名有效，证据级别：可验证回执）",
            "operator": operator,
        })
        self._sync_event_consumption(delivery["event_id"], action="回执关联",
                                     detail=f"{delivery['delivery_no']} → {delivery['status_label']}")
        return delivery, message

    # ------------------------------------------------------------- 人工裁决
    def manual_resolve(
        self,
        delivery_id: int,
        outcome: str,
        *,
        operator: str = "值班员",
        note: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """人工确认送达/判定拒收：只在等待确认（或历史待核对）时允许，证据级别低于签名回执。"""
        delivery = self.get_delivery(delivery_id)
        if delivery is None:
            return None, f"投递 {delivery_id} 不存在或已归档"
        target = {
            domain.ACTION_MANUAL_CONFIRM: domain.DELIVERY_DELIVERED,
            domain.ACTION_MANUAL_REJECT: domain.DELIVERY_REJECTED,
        }.get(outcome)
        if target is None:
            return None, f"不支持的人工裁决动作：{outcome}"
        if delivery["status"] in domain.TERMINAL_STATUSES:
            if delivery.get("evidence_level") == domain.EVIDENCE_RECEIPT:
                return None, "已有可验证回执证据，人工结论不能覆盖证据；如证据有误请走稽核流程"
            return None, f"投递已是「{delivery['status_label']}」，无需重复裁决"
        if delivery["status"] != domain.DELIVERY_PENDING and not delivery.get("legacy"):
            return None, f"当前状态「{delivery['status_label']}」不允许人工裁决，请先发起投递"
        delivery["status"] = target
        delivery["status_label"] = domain.status_label(target)
        delivery["evidence_level"] = domain.EVIDENCE_MANUAL
        delivery["block_reason"] = None
        delivery["block_reason_label"] = ""
        delivery["updated_at"] = _now()
        delivery["history"].append({
            "at": _now(),
            "action": f"人工裁决：{outcome}",
            "detail": note or "操作员依据对端反馈人工确认，证据级别低于签名回执",
            "operator": operator,
        })
        self._sync_event_consumption(delivery["event_id"], action="人工裁决",
                                     detail=f"{delivery['delivery_no']} → {delivery['status_label']}（人工）")
        return delivery, f"已人工标记为「{delivery['status_label']}」；若稍后收到签名回执，将以回执证据为准"

    def verify_legacy(self, delivery_id: int, *, operator: str = "值班员", note: str = "") -> tuple[dict[str, Any] | None, str]:
        """核对历史无投递标识记录：核对后才允许重发，核对动作本身留痕。"""
        delivery = self.get_delivery(delivery_id)
        if delivery is None:
            return None, f"投递 {delivery_id} 不存在或已归档"
        if not delivery.get("legacy"):
            return None, "该投递不是历史记录，无需历史核对"
        delivery["legacy_verified"] = True
        delivery["block_reason"] = None
        delivery["block_reason_label"] = ""
        delivery["updated_at"] = _now()
        delivery["history"].append({
            "at": _now(),
            "action": "历史记录人工核对",
            "detail": note or "操作员已核对原始检测事件与对端记录，允许后续重发",
            "operator": operator,
        })
        if delivery["status"] == domain.DELIVERY_NOT_SENT:
            self._sync_event_consumption(delivery["event_id"], action="历史核对",
                                         detail=f"{delivery['delivery_no']} 核对完成，等待发送")
        return delivery, "历史记录核对完成，现在可以决定是否重发"

    def next_actions(self, delivery: dict[str, Any]) -> list[str]:
        """供前端渲染「下一步可做的动作」，与后端校验口径保持一致。"""
        if delivery.get("legacy") and not delivery.get("legacy_verified"):
            return [domain.ACTION_RESOLVE_LEGACY]
        return list(domain.ALLOWED_ACTIONS.get(str(delivery.get("status")), []))

    def _blocked(self, delivery: dict[str, Any], code: str, detail: str) -> tuple[dict[str, Any], str]:
        delivery["block_reason"] = code
        delivery["block_reason_label"] = domain.block_label(code)
        delivery["updated_at"] = _now()
        delivery.setdefault("history", []).append({"at": _now(), "action": "动作被阻断", "detail": detail})
        if delivery["status"] == domain.DELIVERY_NOT_SENT:
            self._sync_event_consumption(delivery["event_id"], action="投递阻断", detail=detail)
        return delivery, detail


delivery_service = DeliveryService()
