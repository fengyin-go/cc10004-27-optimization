"""本地通知接收端：只在本机演示投递链路，不接任何真实通知服务。

它扮演「对端接收服务」的角色，支持四种可切换的行为：
- accept  ：正常接收并立即回一张带 HMAC 签名的送达回执；
- reject  ：明确拒收并回拒收回执（演示对端拒收）；
- timeout ：连接超时，不回任何东西（演示结果不确定，投递侧只能进入等待确认）；
- hold    ：先收下但挂起，回执由操作员在控制接口手动补发（演示迟到回执）。

回执用 HMAC-SHA256 签名，投递侧只认签名通过的回执作为「可验证的送达证据」。
所有能力都基于标准库实现，克隆下来即可运行。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

# 演示用签名密钥；真实环境会换成密钥管理服务里的渠道密钥
DEMO_SIGNING_SECRET = "local-demo-receipt-secret"

MODE_ACCEPT = "accept"
MODE_REJECT = "reject"
MODE_TIMEOUT = "timeout"
MODE_HOLD = "hold"

MODE_LABELS = {
    MODE_ACCEPT: "正常接收（即时送达回执）",
    MODE_REJECT: "对端拒收（即时拒收回执）",
    MODE_TIMEOUT: "连接超时（无回执，结果不确定）",
    MODE_HOLD: "挂起接收（回执延迟，手动补发）",
}


class ReceiverTimeout(Exception):
    """模拟连接超时：请求没有拿到任何确定结论。"""


def sign_receipt(delivery_id: str, result: str, *, secret: str = DEMO_SIGNING_SECRET) -> str:
    """对回执内容签名，签名串与投递侧的校验口径必须完全一致。"""
    payload = f"{delivery_id}|{result}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_receipt(delivery_id: str, result: str, signature: str, *, secret: str = DEMO_SIGNING_SECRET) -> bool:
    """恒定时间比较，避免签名校验被计时侧信道利用。"""
    expected = sign_receipt(delivery_id, result, secret=secret)
    return hmac.compare_digest(expected, str(signature or ""))


class LocalReceiver:
    """进程内接收端：投递侧调用 send()，控制接口调用其余方法编排演示场景。"""

    def __init__(self) -> None:
        self._online = True
        self._mode = MODE_ACCEPT
        # 已登记的渠道：只有这些渠道允许投递，其余在投递侧给出阻断原因
        self._channels = {"本地演示通道", "值班短信通道", "值班邮件通道"}
        # 挂起的投递：mode=hold 时先存这里，等操作员补发回执
        self._held: dict[str, dict[str, Any]] = {}
        # 接收端侧的收发日志，便于页面展示对端视角
        self._traffic: list[dict[str, Any]] = []

    # ---- 控制面 ----
    def status(self) -> dict[str, Any]:
        return {
            "online": self._online,
            "mode": self._mode,
            "mode_label": MODE_LABELS.get(self._mode, self._mode),
            "channels": sorted(self._channels),
            "held_count": len(self._held),
            "traffic_count": len(self._traffic),
            "signing_secret": DEMO_SIGNING_SECRET,
        }

    def set_mode(self, mode: str) -> dict[str, Any]:
        if mode not in MODE_LABELS:
            raise ValueError(f"未知接收端模式：{mode}")
        self._mode = mode
        return self.status()

    def set_online(self, online: bool) -> dict[str, Any]:
        self._online = bool(online)
        return self.status()

    def register_channel(self, channel: str) -> None:
        channel = str(channel or "").strip()
        if channel:
            self._channels.add(channel)

    def held_deliveries(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._held.values()]

    def traffic(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._traffic]

    # ---- 数据面：投递侧真正调用的入口 ----
    def send(self, delivery_id: str, channel: str, payload: dict[str, Any]) -> dict[str, Any]:
        """返回一张即时回执；超时场景直接抛 ReceiverTimeout，不返回任何结论。"""
        now = int(time.time())
        self._traffic.append({
            "delivery_id": delivery_id,
            "channel": channel,
            "mode": self._mode,
            "at": now,
            "event": "send",
        })
        if not self._online:
            # 离线同样属于「连不上、结论未知」，按超时口径处理
            raise ReceiverTimeout("本地接收端离线，连接未建立")
        if channel not in self._channels:
            # 未登记渠道在投递侧就应被拦下；这里再兜底一次
            raise ReceiverTimeout(f"渠道 {channel} 未在接收端登记")

        if self._mode == MODE_TIMEOUT:
            raise ReceiverTimeout("模拟连接超时：对端没有任何响应")

        if self._mode == MODE_REJECT:
            return self._build_receipt(delivery_id, "rejected", now, note="对端策略拒收")

        if self._mode == MODE_HOLD:
            self._held[delivery_id] = {
                "delivery_id": delivery_id,
                "channel": channel,
                "received_at": now,
                "payload_digest": hashlib.sha256(
                    str(payload.get("事件摘要") or delivery_id).encode("utf-8")
                ).hexdigest()[:12],
            }
            # 挂起时没有回执，投递侧进入等待确认
            raise ReceiverTimeout("对端已挂起处理，回执将延迟到达")

        return self._build_receipt(delivery_id, "accepted", now, note="对端即时确认")

    def release_held_receipt(self, delivery_id: str, result: str) -> dict[str, Any]:
        """补发一张迟到回执：演示 hold 场景下回执晚于人工核对到达。"""
        if delivery_id not in self._held:
            raise KeyError(f"接收端没有挂起投递 {delivery_id}，无法补发回执")
        if result not in ("accepted", "rejected"):
            raise ValueError("补发回执结论只能是 accepted 或 rejected")
        held = self._held.pop(delivery_id)
        now = int(time.time())
        receipt = self._build_receipt(delivery_id, result, now, note="迟到回执（人工补发）")
        receipt["held_since"] = held["received_at"]
        self._traffic.append({
            "delivery_id": delivery_id,
            "channel": held["channel"],
            "mode": self._mode,
            "at": now,
            "event": "late_receipt",
            "result": result,
        })
        return receipt

    def _build_receipt(self, delivery_id: str, result: str, now: int, *, note: str) -> dict[str, Any]:
        signature = sign_receipt(delivery_id, result)
        return {
            "delivery_id": delivery_id,
            "delivery_no": delivery_id,
            "result": result,
            "signature": signature,
            "issued_at": now,
            "note": note,
        }


# 单例：与内存仓储同生命周期，演示期间所有投递都走这一个本地接收端
local_receiver = LocalReceiver()
