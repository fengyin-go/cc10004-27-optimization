"""通知投递处置规则的单元测试。

只用标准库 unittest，不引入额外依赖；每个用例自建事件与投递，
通过本地接收端（mock）编排拒收、超时、挂起迟到回执等场景，不接真实通知服务。
"""
from __future__ import annotations

import unittest

from app.services import notify_domain as domain
from app.services.delivery import delivery_service
from app.services.local_receiver import (
    MODE_ACCEPT,
    MODE_HOLD,
    MODE_REJECT,
    MODE_TIMEOUT,
    local_receiver,
    sign_receipt,
)
from app.store import (
    TABLE_DELIVERIES,
    TABLE_UNMATCHED_RECEIPTS,
    store,
)


def _new_event(summary: str = "单元测试事件") -> dict:
    return delivery_service.create_event({"summary": summary, "metric": "测试指标", "level": "P2"})


def _new_delivery(channel: str = "本地演示通道", event: dict | None = None) -> dict:
    event = event or _new_event()
    delivery, _ = delivery_service.create_delivery(int(event["id"]), {"channel": channel, "rule_id": 1})
    assert delivery is not None
    return delivery


class DeliveryStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        local_receiver.set_online(True)
        local_receiver.set_mode(MODE_ACCEPT)

    # 1. 正常投递：即时回执 → 已确认送达，事件闭环
    def test_accepted_delivery_closes_event(self) -> None:
        event = _new_event()
        delivery = _new_delivery(event=event)
        result, message = delivery_service.dispatch(int(delivery["id"]))
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], domain.DELIVERY_DELIVERED)  # type: ignore[index]
        self.assertEqual(result["evidence_level"], domain.EVIDENCE_RECEIPT)  # type: ignore[index]
        self.assertEqual(delivery_service.get_event(int(event["id"]))["consumption"], domain.CONSUMPTION_DONE)
        self.assertIn("已确认送达", message)

    # 2. 对端拒收：明确的否定终态，区别于等待确认
    def test_rejected_is_terminal_negative(self) -> None:
        local_receiver.set_mode(MODE_REJECT)
        delivery = _new_delivery()
        result, _ = delivery_service.dispatch(int(delivery["id"]))
        self.assertEqual(result["status"], domain.DELIVERY_REJECTED)  # type: ignore[index]
        self.assertEqual(result["block_reason"], None)  # type: ignore[index]

    # 3. 连接超时不能算作未送达：进入等待确认，且禁止无脑重试
    def test_timeout_becomes_pending_not_failed_and_blocks_blind_retry(self) -> None:
        local_receiver.set_mode(MODE_TIMEOUT)
        event = _new_event()
        delivery = _new_delivery(event=event)
        result, message = delivery_service.dispatch(int(delivery["id"]))
        self.assertEqual(result["status"], domain.DELIVERY_PENDING)  # type: ignore[index]
        self.assertEqual(result["attempt"], 1)  # type: ignore[index]
        self.assertEqual(result["block_reason"], domain.BLOCK_WAIT_ACK)  # type: ignore[index]
        self.assertIn("不会自动重试", message)
        # 事件保持「等待投递确认」，不会被标成已处理
        self.assertEqual(
            delivery_service.get_event(int(event["id"]))["consumption"],
            domain.CONSUMPTION_WAITING,
        )
        # 未勾选人工核对时，重试必须被阻断
        blocked, block_msg = delivery_service.retry(int(delivery["id"]))
        self.assertEqual(blocked["status"], domain.DELIVERY_PENDING)  # type: ignore[index]
        self.assertEqual(blocked["attempt"], 1)  # type: ignore[index]
        self.assertIn("等待确认", block_msg)

    # 4. 超时后经人工核对允许重发：复用同一投递项，不产生第二次相同投递
    def test_checked_retry_reuses_same_delivery(self) -> None:
        local_receiver.set_mode(MODE_TIMEOUT)
        delivery = _new_delivery()
        delivery_service.dispatch(int(delivery["id"]))
        before = len(store.rows(TABLE_DELIVERIES))

        local_receiver.set_mode(MODE_ACCEPT)
        result, _ = delivery_service.retry(int(delivery["id"]), acknowledge=True)
        self.assertEqual(result["status"], domain.DELIVERY_DELIVERED)  # type: ignore[index]
        self.assertEqual(result["attempt"], 2)  # type: ignore[index]
        self.assertEqual(len(store.rows(TABLE_DELIVERIES)), before)  # 台账条数不变
        self.assertEqual(result["delivery_no"], delivery["delivery_no"])  # type: ignore[index]

    # 5. 拒收后重发同样复用原投递项
    def test_retry_after_rejection_keeps_single_delivery_item(self) -> None:
        local_receiver.set_mode(MODE_REJECT)
        delivery = _new_delivery()
        delivery_service.dispatch(int(delivery["id"]))
        local_receiver.set_mode(MODE_ACCEPT)
        result, _ = delivery_service.retry(int(delivery["id"]))
        self.assertEqual(result["status"], domain.DELIVERY_DELIVERED)  # type: ignore[index]
        self.assertEqual(result["attempt"], 2)  # type: ignore[index]
        same_channel = [
            row for row in store.rows(TABLE_DELIVERIES)
            if row["delivery_no"] == delivery["delivery_no"]
        ]
        self.assertEqual(len(same_channel), 1)

    # 6. 建立投递台账本身幂等：同事件同渠道的未终态投递只建一次
    def test_create_delivery_is_idempotent(self) -> None:
        event = _new_event()
        first, msg1 = delivery_service.create_delivery(int(event["id"]), {"channel": "值班短信通道", "rule_id": 1})
        second, msg2 = delivery_service.create_delivery(int(event["id"]), {"channel": "值班短信通道", "rule_id": 1})
        self.assertEqual(first["id"], second["id"])  # type: ignore[index]
        self.assertIn("复用原投递项", msg2)
        self.assertNotIn("复用", msg1)

    # 7. 终态不能再发起投递
    def test_terminal_delivery_cannot_dispatch_again(self) -> None:
        delivery = _new_delivery()
        delivery_service.dispatch(int(delivery["id"]))
        blocked, message = delivery_service.dispatch(int(delivery["id"]))
        self.assertEqual(blocked["status"], domain.DELIVERY_DELIVERED)
        self.assertEqual(blocked["block_reason"], domain.BLOCK_TERMINAL)
        self.assertIn("终态", message)


class ReceiptAssociationTests(unittest.TestCase):
    def setUp(self) -> None:
        local_receiver.set_online(True)
        local_receiver.set_mode(MODE_TIMEOUT)

    # 8. 重复回执只更新同一投递项，回执不重复落账
    def test_duplicate_receipt_updates_same_item_once(self) -> None:
        delivery = _new_delivery()
        delivery_service.dispatch(int(delivery["id"]))
        signature = sign_receipt(delivery["delivery_no"], domain.RECEIPT_ACCEPTED)
        first, _ = delivery_service.ingest_receipt(delivery["delivery_no"], "accepted", signature)
        second, message = delivery_service.ingest_receipt(delivery["delivery_no"], "accepted", signature)
        self.assertEqual(first["id"], second["id"])
        self.assertIn("重复", message)
        receipts = delivery_service.list_receipts(int(delivery["id"]))
        self.assertEqual(len(receipts), 1)

    # 9. 乱序回执：先到拒收、后到送达，同一投递项按证据更新且全程留痕
    def test_out_of_order_receipts_stay_on_same_delivery(self) -> None:
        delivery = _new_delivery()
        delivery_service.dispatch(int(delivery["id"]))
        sig_reject = sign_receipt(delivery["delivery_no"], "rejected")
        sig_accept = sign_receipt(delivery["delivery_no"], "accepted")
        delivery_service.ingest_receipt(delivery["delivery_no"], "rejected", sig_reject)
        current = delivery_service.get_delivery(int(delivery["id"]))
        self.assertEqual(current["status"], domain.DELIVERY_REJECTED)
        delivery_service.ingest_receipt(delivery["delivery_no"], "accepted", sig_accept, source="迟到回执")
        current = delivery_service.get_delivery(int(delivery["id"]))
        self.assertEqual(current["status"], domain.DELIVERY_DELIVERED)
        self.assertEqual(len(delivery_service.list_receipts(int(delivery["id"]))), 2)
        actions = [item["action"] for item in current["history"]]
        self.assertTrue(any("回执" in action for action in actions))

    # 10. 人工确认与晚到回执冲突：以可验证的送达证据为准
    def test_late_signed_receipt_overrides_manual_confirmation(self) -> None:
        local_receiver.set_mode(MODE_HOLD)
        delivery = _new_delivery()
        delivery_service.dispatch(int(delivery["id"]))
        # 操作员先人工判定拒收
        manual, _ = delivery_service.manual_resolve(int(delivery["id"]), domain.ACTION_MANUAL_REJECT)
        self.assertEqual(manual["evidence_level"], domain.EVIDENCE_MANUAL)  # type: ignore[index]
        self.assertEqual(manual["status"], domain.DELIVERY_REJECTED)  # type: ignore[index]
        # 接收端补发一张送达的签名回执
        receipt = local_receiver.release_held_receipt(delivery["delivery_no"], "accepted")
        result, message = delivery_service.ingest_receipt(
            receipt["delivery_no"], receipt["result"], receipt["signature"], source="迟到回执"
        )
        self.assertEqual(result["status"], domain.DELIVERY_DELIVERED)  # type: ignore[index]
        self.assertEqual(result["evidence_level"], domain.EVIDENCE_RECEIPT)  # type: ignore[index]
        self.assertIn("冲突", message)
        self.assertTrue(any("冲突" in item["action"] for item in result["history"]))  # type: ignore[index]

    # 11. 已有签名证据后，人工裁决不能覆盖
    def test_manual_cannot_override_signed_receipt(self) -> None:
        local_receiver.set_mode(MODE_ACCEPT)
        delivery = _new_delivery()
        delivery_service.dispatch(int(delivery["id"]))
        blocked, message = delivery_service.manual_resolve(
            int(delivery["id"]), domain.ACTION_MANUAL_REJECT
        )
        self.assertIsNone(blocked)
        self.assertIn("不能覆盖证据", message)

    # 12. 签名无效的回执不改变结论，只留痕
    def test_invalid_signature_is_ignored(self) -> None:
        delivery = _new_delivery()
        delivery_service.dispatch(int(delivery["id"]))
        result, message = delivery_service.ingest_receipt(
            delivery["delivery_no"], "accepted", "bad-signature"
        )
        self.assertEqual(result["status"], domain.DELIVERY_PENDING)
        self.assertIn("签名无效", message)
        self.assertEqual(len(delivery_service.list_receipts(int(delivery["id"]))), 1)  # 仍留痕

    # 13. 关联不上的回执单独落账，不静默丢弃（兼容历史无标识记录）
    def test_unmatched_receipt_is_quarantined(self) -> None:
        before = len(store.rows(TABLE_UNMATCHED_RECEIPTS))
        result, message = delivery_service.ingest_receipt("DLV-GHOST-99", "accepted",
                                                           sign_receipt("DLV-GHOST-99", "accepted"))
        self.assertIsNone(result)
        self.assertIn("未关联回执", message)
        self.assertEqual(len(store.rows(TABLE_UNMATCHED_RECEIPTS)), before + 1)


class LegacyAndBlockingTests(unittest.TestCase):
    def setUp(self) -> None:
        local_receiver.set_online(True)
        local_receiver.set_mode(MODE_ACCEPT)

    def _legacy_delivery(self) -> dict:
        """构造一条历史无投递标识记录。"""
        event = _new_event("历史链路事件")
        delivery, _ = delivery_service.create_delivery(int(event["id"]), {"channel": "旧版短信网关"})
        delivery["legacy"] = True
        delivery["legacy_verified"] = False
        delivery["status"] = domain.DELIVERY_NOT_SENT
        delivery["status_label"] = domain.status_label(domain.DELIVERY_NOT_SENT)
        delivery["block_reason"] = domain.BLOCK_LEGACY_UNVERIFIED
        delivery["block_reason_label"] = domain.block_label(domain.BLOCK_LEGACY_UNVERIFIED)
        return delivery

    # 14. 历史记录未核对前，投递与重发都被阻断
    def test_legacy_must_be_verified_before_resend(self) -> None:
        delivery = self._legacy_delivery()
        blocked, message = delivery_service.dispatch(int(delivery["id"]))
        self.assertEqual(blocked["block_reason"], domain.BLOCK_LEGACY_UNVERIFIED)
        self.assertIn("核对", message)
        self.assertEqual(delivery_service.next_actions(blocked), [domain.ACTION_RESOLVE_LEGACY])

        verified, _ = delivery_service.verify_legacy(int(delivery["id"]), note="已与对端电话核对")
        self.assertTrue(verified["legacy_verified"])  # type: ignore[index]
        # 历史渠道在新接收端补登记后，重发走同一投递项
        local_receiver.register_channel("旧版短信网关")
        result, _ = delivery_service.retry(int(delivery["id"]))
        self.assertEqual(result["status"], domain.DELIVERY_DELIVERED)  # type: ignore[index]
        self.assertEqual(result["delivery_no"], delivery["delivery_no"])  # type: ignore[index]

    # 15. 接收端离线：阻断并给出可读原因，恢复后续发
    def test_receiver_offline_blocks_then_resume_continues(self) -> None:
        local_receiver.set_online(False)
        delivery = _new_delivery()
        blocked, message = delivery_service.dispatch(int(delivery["id"]))
        self.assertEqual(blocked["status"], domain.DELIVERY_NOT_SENT)
        self.assertEqual(blocked["block_reason"], domain.BLOCK_RECEIVER_OFFLINE)
        self.assertIn("离线", message)

        local_receiver.set_online(True)
        report = delivery_service.resume()
        self.assertTrue(any(item["delivery_no"] == delivery["delivery_no"] for item in report["resumed"]))
        self.assertEqual(delivery_service.get_delivery(int(delivery["id"]))["status"],
                         domain.DELIVERY_DELIVERED)

    # 16. 未登记渠道阻断，登记后可续发
    def test_unknown_channel_blocks_until_registered(self) -> None:
        delivery = _new_delivery(channel="未登记的新渠道")
        blocked, _ = delivery_service.dispatch(int(delivery["id"]))
        self.assertEqual(blocked["block_reason"], domain.BLOCK_CHANNEL_UNKNOWN)
        local_receiver.register_channel("未登记的新渠道")
        result, _ = delivery_service.dispatch(int(delivery["id"]))
        self.assertEqual(result["status"], domain.DELIVERY_DELIVERED)  # type: ignore[index]

    # 17. 规则停用阻断未发送投递，重新启用解除阻断
    def test_disabled_rule_blocks_and_enabling_releases(self) -> None:
        from app.services.notify import NotifyService

        notify_service = NotifyService()
        rule, _ = notify_service.create_entry(
            {"规则编号": "NOTI-TEST-1", "触发条件": "测试", "通知渠道": "本地演示通道"}
        )
        notify_service.run_action(int(rule["id"]), "提交校验")  # type: ignore[index]
        notify_service.run_action(int(rule["id"]), "启用通知")
        event = _new_event()
        delivery, _ = delivery_service.create_delivery(
            int(event["id"]), {"channel": "本地演示通道", "rule_id": rule["id"]}
        )
        notify_service.run_action(int(rule["id"]), "停用通知")
        blocked, _ = delivery_service.dispatch(int(delivery["id"]))
        self.assertEqual(blocked["block_reason"], domain.BLOCK_RULE_DISABLED)

        notify_service.run_action(int(rule["id"]), "启用通知")
        result, _ = delivery_service.dispatch(int(delivery["id"]))
        self.assertEqual(result["status"], domain.DELIVERY_DELIVERED)  # type: ignore[index]

    # 18. 恢复续发只处理未发送项，等待确认项不被自动重试
    def test_resume_skips_pending_ack_items(self) -> None:
        local_receiver.set_mode(MODE_TIMEOUT)
        pending = _new_delivery()
        delivery_service.dispatch(int(pending["id"]))
        local_receiver.set_mode(MODE_ACCEPT)
        not_sent = _new_delivery()

        report = delivery_service.resume()
        resumed_nos = [item["delivery_no"] for item in report["resumed"]]
        self.assertIn(not_sent["delivery_no"], resumed_nos)
        self.assertNotIn(pending["delivery_no"], resumed_nos)
        # 等待确认项原封不动，尝试次数不增加
        self.assertEqual(delivery_service.get_delivery(int(pending["id"]))["attempt"], 1)


class CrossModuleSyncTests(unittest.TestCase):
    def test_event_consumption_follows_delivery_certainty(self) -> None:
        event = _new_event()
        self.assertEqual(delivery_service.get_event(int(event["id"]))["consumption"],
                         domain.CONSUMPTION_PENDING)
        delivery = _new_delivery(event=event)
        # 未发送 → 投递阻断口径
        self.assertEqual(delivery_service.get_event(int(event["id"]))["consumption"],
                         domain.CONSUMPTION_STALLED)
        local_receiver.set_mode(MODE_TIMEOUT)
        delivery_service.dispatch(int(delivery["id"]))
        # 等待确认 ≠ 已处理
        self.assertEqual(delivery_service.get_event(int(event["id"]))["consumption"],
                         domain.CONSUMPTION_WAITING)
        signature = sign_receipt(delivery["delivery_no"], "accepted")
        delivery_service.ingest_receipt(delivery["delivery_no"], "accepted", signature)
        self.assertEqual(delivery_service.get_event(int(event["id"]))["consumption"],
                         domain.CONSUMPTION_DONE)
        # 历史留痕完整
        history = delivery_service.get_event(int(event["id"]))["history"]
        self.assertTrue(any(item["action"] == "回执关联" for item in history))


if __name__ == "__main__":
    unittest.main(verbosity=2)
