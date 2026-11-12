"""通知分发接口。

上半部分保留原有通知规则的维护能力；下半部分是投递结果处置域：
- 投递台账按「未发送 / 等待确认 / 对端拒收 / 已确认送达」四态管理；
- 回执按 delivery_id 关联，支持乱序、重复、迟到与签名校验；
- 本地接收端（mock）用于演示拒收、超时、迟到回执，不接真实通知服务。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services import notify_domain as domain
from app.services.delivery import delivery_service
from app.services.local_receiver import local_receiver
from app.services.notify import NotifyService

router = APIRouter(prefix="/api/notify", tags=["通知分发"])

service = NotifyService()

LIST_FIELDS = ["规则编号", "触发条件", "通知渠道", "接收列表", "消息模板", "合并窗口秒", "重复抑制", "优先级"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


# =====================================================================
# 一、通知规则（原有能力，保持兼容）
# =====================================================================
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤通知分发列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出通知分发清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "notify", "total": total, "items": items}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条通知规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="通知规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条通知规则执行提交校验、启用通知、停用通知；停用会联动阻断未完成投递。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


# =====================================================================
# 二、本地接收端（mock，不接真实通知服务）
# =====================================================================
@router.get("/receiver/status")
def receiver_status() -> dict[str, Any]:
    """读取本地接收端当前模式、在线状态与已登记渠道。"""
    return local_receiver.status()


@router.post("/receiver/mode", response_model=ActionResult)
def set_receiver_mode(payload: EntryPayload) -> ActionResult:
    """切换接收端行为：accept 正常 / reject 拒收 / timeout 超时 / hold 挂起补发。"""
    mode = str(payload.values.get("mode") or "").strip()
    try:
        status = local_receiver.set_mode(mode)
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=f"接收端已切换为：{status['mode_label']}", entry=status)


@router.post("/receiver/online", response_model=ActionResult)
def set_receiver_online(payload: EntryPayload) -> ActionResult:
    """设置接收端在线/离线，离线投递按连接超时口径进入等待确认。"""
    online = bool(payload.values.get("online"))
    status = local_receiver.set_online(online)
    return ActionResult(ok=True, message="接收端已上线" if online else "接收端已离线（投递将超时）", entry=status)


@router.post("/receiver/channels", response_model=ActionResult)
def register_channel(payload: EntryPayload) -> ActionResult:
    """登记一个演示渠道，解除「渠道未登记」阻断。"""
    channel = str(payload.values.get("channel") or "").strip()
    if not channel:
        return ActionResult(ok=False, message="渠道名称不能为空")
    local_receiver.register_channel(channel)
    return ActionResult(ok=True, message=f"渠道 {channel} 已登记", entry=local_receiver.status())


@router.get("/receiver/held")
def held_deliveries() -> dict[str, Any]:
    """查看接收端挂起、等待补发回执的投递。"""
    return {"items": local_receiver.held_deliveries()}


@router.post("/receiver/release-receipt", response_model=ActionResult)
def release_held_receipt(payload: EntryPayload) -> ActionResult:
    """对挂起投递补发一张迟到回执（演示人工确认与晚到回执冲突）。"""
    delivery_no = str(payload.values.get("delivery_no") or "").strip()
    result = str(payload.values.get("result") or "accepted").strip()
    try:
        receipt = local_receiver.release_held_receipt(delivery_no, result)
    except KeyError as exc:
        return ActionResult(ok=False, message=str(exc))
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    # 回执进入统一关联入口，保证与正常回执走同一套裁决/留痕逻辑
    delivery, message = delivery_service.ingest_receipt(
        receipt["delivery_no"],
        receipt["result"],
        receipt["signature"],
        issued_at=receipt["issued_at"],
        note=receipt.get("note", ""),
        source="迟到回执",
        operator="接收端补发",
    )
    return ActionResult(ok=delivery is not None, message=message, entry=delivery)


@router.get("/receiver/traffic")
def receiver_traffic() -> dict[str, Any]:
    """接收端视角的收发日志，方便演示时核对对端到底收到了什么。"""
    return {"items": local_receiver.traffic()}


# =====================================================================
# 三、异常检测事件（跨模块消费结论）
# =====================================================================
@router.get("/events")
def list_events(consumption: str | None = Query(default=None, description="待分发/等待投递确认/投递阻断/已处理")) -> dict[str, Any]:
    """列出异常检测事件及其通知消费结论；等待确认不会回写为已处理。"""
    return {"total": len(delivery_service.list_events(consumption=consumption)),
            "items": delivery_service.list_events(consumption=consumption)}


@router.post("/events", response_model=ActionResult)
def create_event(payload: EntryPayload) -> ActionResult:
    """手动登记一条演示用异常检测事件。"""
    event = delivery_service.create_event(payload.values)
    return ActionResult(ok=True, message="异常事件已登记，可发起通知投递", entry=event)


# =====================================================================
# 四、投递台账：四态、阻断原因、下一步动作
# =====================================================================
@router.get("/deliveries")
def list_deliveries(
    status: str | None = Query(default=None, description="not_sent/pending_ack/rejected/delivered"),
    keyword: str | None = None,
    legacy_only: bool = False,
) -> dict[str, Any]:
    """投递台账列表，附带每个状态允许的下一步动作与阻断原因。"""
    rows = delivery_service.list_deliveries(status=status, keyword=keyword, legacy_only=legacy_only)
    items = []
    for row in rows:
        item = dict(row)
        item["next_actions"] = delivery_service.next_actions(row)
        items.append(item)
    return {"total": len(items), "items": items, "stats": delivery_service.delivery_stats()}


@router.get("/deliveries/{delivery_id}")
def get_delivery(delivery_id: int) -> dict[str, Any]:
    """投递明细：台账、回执清单、阻断原因与下一步动作，供页面抽屉展示。"""
    delivery = delivery_service.get_delivery(delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail=f"投递 {delivery_id} 不存在或已归档")
    return {
        "delivery": delivery,
        "receipts": delivery_service.list_receipts(delivery_id),
        "next_actions": delivery_service.next_actions(delivery),
    }


@router.post("/events/{event_id}/deliveries", response_model=ActionResult)
def create_delivery(event_id: int, payload: EntryPayload) -> ActionResult:
    """为异常事件建立投递台账；同事件同渠道的未完成投递会幂等复用。"""
    delivery, message = delivery_service.create_delivery(event_id, payload.values)
    if delivery is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=delivery)


@router.post("/deliveries/{delivery_id}/dispatch", response_model=ActionResult)
def dispatch_delivery(delivery_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """发起一次投递尝试；连接超时只进入等待确认，不会自动重试。"""
    operator = str((payload.values if payload else {}).get("operator") or "值班员")
    delivery, message = delivery_service.dispatch(delivery_id, operator=operator)
    if delivery is None:
        return ActionResult(ok=False, message=message)
    # 状态仍停在未发送/等待确认且带阻断原因，说明动作没有真正发出，按失败口径返回
    ok = delivery.get("block_reason") is None and delivery["status"] != domain.DELIVERY_NOT_SENT
    return ActionResult(ok=ok, message=message, entry=delivery)


@router.post("/deliveries/{delivery_id}/retry", response_model=ActionResult)
def retry_delivery(delivery_id: int, payload: EntryPayload) -> ActionResult:
    """操作员核对后重发：复用同一投递项，不生成第二次相同投递。"""
    values = payload.values
    operator = str(values.get("operator") or "值班员")
    acknowledge = bool(values.get("acknowledge"))
    delivery, message = delivery_service.retry(
        delivery_id, operator=operator, acknowledge=acknowledge
    )
    if delivery is None:
        return ActionResult(ok=False, message=message)
    ok = delivery.get("block_reason") is None and delivery["status"] not in (
        domain.DELIVERY_NOT_SENT,
        domain.DELIVERY_PENDING,
    )
    return ActionResult(ok=ok, message=message, entry=delivery)


@router.post("/deliveries/{delivery_id}/manual-resolve", response_model=ActionResult)
def manual_resolve(delivery_id: int, payload: EntryPayload) -> ActionResult:
    """人工确认送达/判定拒收；证据级别低于签名回执，冲突时回执优先。"""
    values = payload.values
    outcome = str(values.get("outcome") or "").strip()
    operator = str(values.get("operator") or "值班员")
    note = str(values.get("note") or "")
    delivery, message = delivery_service.manual_resolve(
        delivery_id, outcome, operator=operator, note=note
    )
    if delivery is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=delivery)


@router.post("/deliveries/{delivery_id}/verify-legacy", response_model=ActionResult)
def verify_legacy(delivery_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """核对历史无投递标识记录；核对完成后才允许决定是否重发。"""
    values = payload.values if payload else {}
    operator = str(values.get("operator") or "值班员")
    note = str(values.get("note") or "")
    delivery, message = delivery_service.verify_legacy(delivery_id, operator=operator, note=note)
    if delivery is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=delivery)


@router.post("/resume", response_model=ActionResult)
def resume_deliveries(payload: EntryPayload | None = None) -> ActionResult:
    """恢复续发：只继续未发送且已核对的投递，等待确认项不自动重试。"""
    values = payload.values if payload else {}
    operator = str(values.get("operator") or "值班员")
    report = delivery_service.resume(operator=operator)
    message = (
        f"续发完成：成功 {len(report['resumed'])} 条，"
        f"跳过 {len(report['skipped'])} 条，失败 {len(report['failed'])} 条"
    )
    return ActionResult(ok=True, message=message, entry=report)


# =====================================================================
# 五、回执：关联、验签、未关联清单
# =====================================================================
@router.get("/receipts")
def list_receipts(delivery_id: int | None = None) -> dict[str, Any]:
    """回执台账（可按投递项过滤）与关联不上的回执清单。"""
    return {
        "items": delivery_service.list_receipts(delivery_id),
        "unmatched": delivery_service.list_unmatched_receipts(),
    }


@router.post("/receipts", response_model=ActionResult)
def ingest_receipt(payload: EntryPayload) -> ActionResult:
    """录入一张回执并按 delivery_no 关联；签名无效或关联不上都会明确留痕。"""
    values = payload.values
    delivery_no = str(values.get("delivery_no") or "").strip()
    result = str(values.get("result") or "").strip()
    signature = str(values.get("signature") or "").strip()
    if not delivery_no or result not in (domain.RECEIPT_ACCEPTED, domain.RECEIPT_REJECTED):
        return ActionResult(ok=False, message="回执需包含 delivery_no，且 result 只能是 accepted/rejected")
    delivery, message = delivery_service.ingest_receipt(
        delivery_no,
        result,
        signature,
        issued_at=values.get("issued_at"),
        note=str(values.get("note") or ""),
        source=str(values.get("source") or "手工录入回执"),
        operator=str(values.get("operator") or "值班员"),
    )
    return ActionResult(ok=delivery is not None, message=message, entry=delivery)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条通知规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"通知规则 {entry_id} 不存在或已归档")
    return entry
