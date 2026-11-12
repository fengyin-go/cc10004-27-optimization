"""端到端冒烟：通过真实 HTTP 接口走一遍操作员演示链路。

用法：先启动 uvicorn（默认 127.0.0.1:8000），再运行
    python tests/http_smoke.py [base_url]
脚本只调用本地接口与本地 mock 接收端，不接真实通知服务。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
PASS = 0
FAIL = 0


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def main() -> int:
    print("① 健康检查与接收端初始状态")
    _, health = call("GET", "/api/health")
    check("服务在线", health.get("ok") is True)
    _, receiver = call("GET", "/api/notify/receiver/status")
    check("接收端默认在线", receiver["online"] is True)

    print("② 登记异常事件并建立投递")
    _, created = call("POST", "/api/notify/events", {"values": {
        "event_no": "SMOKE-EVT-1", "metric": "冒烟指标", "summary": "端到端冒烟事件", "level": "P1"}})
    event_id = created["entry"]["id"]
    _, dlv = call("POST", f"/api/notify/events/{event_id}/deliveries",
                  {"values": {"channel": "本地演示通道", "rule_id": 3}})
    delivery_id = dlv["entry"]["id"]
    delivery_no = dlv["entry"]["delivery_no"]
    check("初始为未发送", dlv["entry"]["status"] == "not_sent")

    print("③ 超时场景：结果不确定，进入等待确认，不自动重试")
    call("POST", "/api/notify/receiver/mode", {"values": {"mode": "timeout"}})
    _, dispatched = call("POST", f"/api/notify/deliveries/{delivery_id}/dispatch", {})
    check("超时后等待确认", dispatched["entry"]["status"] == "pending_ack", str(dispatched))
    check("阻断原因为等待回执", dispatched["entry"]["block_reason"] == "waiting_ack")
    _, events = call("GET", "/api/notify/events")
    smoke_event = next(item for item in events["items"] if item["id"] == event_id)
    check("原检测事件未被标为已处理", smoke_event["consumption"] == "等待投递确认")
    _, blind_retry = call("POST", f"/api/notify/deliveries/{delivery_id}/retry",
                          {"values": {}})
    check("盲重试被阻断", blind_retry["ok"] is False and "等待确认" in blind_retry["message"])

    print("④ 迟到的签名回执到达，关联到同一投递项并闭环事件")
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.services.local_receiver import sign_receipt  # noqa: E402
    signature = sign_receipt(delivery_no, "accepted")
    _, receipt_resp = call("POST", "/api/notify/receipts", {"values": {
        "delivery_no": delivery_no, "result": "accepted", "signature": signature,
        "source": "迟到回执", "note": "冒烟补发"}})
    check("回执关联成功", receipt_resp["ok"] is True, str(receipt_resp))
    check("投递更新为已确认送达", receipt_resp["entry"]["status"] == "delivered")
    _, events = call("GET", "/api/notify/events")
    smoke_event = next(item for item in events["items"] if item["id"] == event_id)
    check("事件闭环为已处理", smoke_event["consumption"] == "已处理")
    _, again = call("POST", "/api/notify/receipts", {"values": {
        "delivery_no": delivery_no, "result": "accepted", "signature": signature}})
    check("重复回执不重复变更", "重复" in again["message"])

    print("⑤ 拒收场景与人工裁决冲突裁决")
    _, ev2 = call("POST", "/api/notify/events", {"values": {"summary": "拒收冒烟事件"}})
    _, dlv2 = call("POST", f"/api/notify/events/{ev2['entry']['id']}/deliveries",
                   {"values": {"channel": "本地演示通道", "rule_id": 3}})
    call("POST", "/api/notify/receiver/mode", {"values": {"mode": "hold"}})
    call("POST", f"/api/notify/deliveries/{dlv2['entry']['id']}/dispatch", {})
    _, manual = call("POST", f"/api/notify/deliveries/{dlv2['entry']['id']}/manual-resolve",
                     {"values": {"outcome": "人工判定拒收", "note": "电话未接通"}})
    check("人工裁决生效", manual["entry"]["status"] == "rejected")
    _, release = call("POST", "/api/notify/receiver/release-receipt",
                      {"values": {"delivery_no": dlv2["entry"]["delivery_no"], "result": "accepted"}})
    check("晚到回执覆盖人工结论", release["entry"]["status"] == "delivered", str(release))
    check("证据级别为签名回执", release["entry"]["evidence_level"] == "signed_receipt")

    print("⑥ 历史无标识记录：先核对再重发，恢复续发不碰等待项")
    _, legacy = call("GET", "/api/notify/deliveries?legacy_only=true")
    legacy_item = legacy["items"][0]
    check("历史记录初始被阻断", legacy_item["block_reason"] == "legacy_unverified")
    _, blocked = call("POST", f"/api/notify/deliveries/{legacy_item['id']}/dispatch", {})
    check("未核对不能发送", blocked["ok"] is False)
    call("POST", f"/api/notify/deliveries/{legacy_item['id']}/verify-legacy",
         {"values": {"note": "已核对原始事件"}})
    call("POST", "/api/notify/receiver/mode", {"values": {"mode": "accept"}})
    call("POST", "/api/notify/receiver/channels", {"values": {"channel": "旧版短信网关"}})
    _, resume = call("POST", "/api/notify/resume", {"values": {}})
    check("恢复续发有明确报告", "续发完成" in resume["message"])
    _, detail = call("GET", f"/api/notify/deliveries/{legacy_item['id']}")
    check("历史记录核对后完成送达", detail["delivery"]["status"] == "delivered")

    print("⑦ 未关联回执单独落账")
    _, receipts = call("GET", "/api/notify/receipts")
    check("未关联回执清单非空", len(receipts["unmatched"]) >= 1)

    print(f"\n结果：通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
