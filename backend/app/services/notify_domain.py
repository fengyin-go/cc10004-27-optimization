"""通知分发投递域：状态口径、阻断原因与可执行动作的统一定义。

规则要点（与操作员手册保持一致）：
- 投递结果区分四态：未发送 / 等待确认 / 对端拒收 / 已确认送达。
- 连接超时属于「结果不确定」，只能落到等待确认，不允许直接当作未送达后自动重试。
- 对端拒收是确定的否定结论，等待确认是不确定结论，两者处置方式不同。
"""
from __future__ import annotations

# ---- 异常检测事件的消费结论（跨模块同步给异常检测模块）----
CONSUMPTION_PENDING = "待分发"        # 还没有产生任何投递尝试
CONSUMPTION_WAITING = "等待投递确认"  # 已发出但结果不确定，事件不得标记为已处理
CONSUMPTION_DONE = "已处理"           # 拿到确定终态（拒收闭环或确认送达）
CONSUMPTION_STALLED = "投递阻断"      # 未发送且被阻断，等待操作员核对

# ---- 投递四态 ----
DELIVERY_NOT_SENT = "not_sent"       # 未发送
DELIVERY_PENDING = "pending_ack"     # 等待确认（结果不确定）
DELIVERY_REJECTED = "rejected"       # 对端拒收（确定否定）
DELIVERY_DELIVERED = "delivered"     # 已确认送达（确定肯定）

DELIVERY_STATUS_LABELS = {
    DELIVERY_NOT_SENT: "未发送",
    DELIVERY_PENDING: "等待确认",
    DELIVERY_REJECTED: "对端拒收",
    DELIVERY_DELIVERED: "已确认送达",
}

# 终态：一旦进入就不再被普通动作改写（冲突裁决除外，且全程留痕）
TERMINAL_STATUSES = {DELIVERY_REJECTED, DELIVERY_DELIVERED}

# ---- 阻断原因：页面要让操作员看见「为什么卡住、下一步能做什么」----
BLOCK_RULE_DISABLED = "rule_disabled"          # 通知规则已停用
BLOCK_CHANNEL_UNKNOWN = "channel_unknown"      # 渠道在本地接收端没有登记
BLOCK_RECEIVER_OFFLINE = "receiver_offline"    # 本地接收端离线
BLOCK_RETRY_IN_FLIGHT = "retry_in_flight"      # 已有在途重试，禁止第二次相同投递
BLOCK_WAIT_ACK = "waiting_ack"                 # 结果不确定，先等回执/人工核对，禁止盲重试
BLOCK_LEGACY_UNVERIFIED = "legacy_unverified"  # 历史记录无投递标识，需先核对
BLOCK_TERMINAL = "terminal_state"              # 已是终态，无需再发

BLOCK_REASON_LABELS = {
    BLOCK_RULE_DISABLED: "通知规则已停用，恢复启用后可继续未完成投递",
    BLOCK_CHANNEL_UNKNOWN: "通知渠道未在本地接收端登记，请先登记接收通道",
    BLOCK_RECEIVER_OFFLINE: "本地接收端当前离线，恢复在线后可续发",
    BLOCK_RETRY_IN_FLIGHT: "该投递已有在途重试，不能生成第二次相同投递",
    BLOCK_WAIT_ACK: "连接超时、结果不确定：请等待回执或人工核对，禁止直接重试",
    BLOCK_LEGACY_UNVERIFIED: "历史记录缺少投递标识，需人工核对原始事件后再决定重发",
    BLOCK_TERMINAL: "投递已处于终态，无需再次发送",
}

# ---- 每个状态允许操作员执行的下一步动作（前端按此渲染按钮，后端同样校验）----
ACTION_DISPATCH = "发起投递"
ACTION_RETRY = "核对后重发"
ACTION_MANUAL_CONFIRM = "人工确认送达"
ACTION_MANUAL_REJECT = "人工判定拒收"
ACTION_RESOLVE_LEGACY = "核对历史记录"
ACTION_RESUME = "恢复续发"

ALLOWED_ACTIONS: dict[str, list[str]] = {
    DELIVERY_NOT_SENT: [ACTION_DISPATCH],
    DELIVERY_PENDING: [ACTION_MANUAL_CONFIRM, ACTION_MANUAL_REJECT],
    DELIVERY_REJECTED: [ACTION_RETRY],
    DELIVERY_DELIVERED: [],
}

# ---- 回执结论（对端/接收端回传）----
RECEIPT_ACCEPTED = "accepted"
RECEIPT_REJECTED = "rejected"

RECEIPT_RESULT_LABELS = {
    RECEIPT_ACCEPTED: "对端确认接收",
    RECEIPT_REJECTED: "对端拒收",
}

# 人工确认的证据级别低于带签名回执；冲突时以可验证证据为准
EVIDENCE_RECEIPT = "signed_receipt"
EVIDENCE_MANUAL = "manual_confirmation"


def status_label(status: str) -> str:
    return DELIVERY_STATUS_LABELS.get(status, status)


def block_label(code: str | None) -> str:
    if not code:
        return ""
    return BLOCK_REASON_LABELS.get(code, code)
