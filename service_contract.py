"""端到端契约测试：覆盖许可版本化、批次暂停联动、销售去重与分账规则。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import SERVICE_ID, SERVICE_NAME, health_payload


def _import_handler():
    from db import Store
    from web import Application, make_handler
    store = Store(":memory:")
    app = Application(store)
    return make_handler(app), store


class Client:
    """极简 JSON HTTP 客户端。"""

    def __init__(self, base_url, token=None):
        self.base_url = base_url
        self.token = token

    def call(self, method, path, body=None, token=None, expect_error=False):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        token = token if token is not None else self.token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(self.base_url + path, data=data, headers=headers,
                          method=method)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            payload = json.load(exc)
            exc.close()
            if expect_error:
                return exc.code, payload
            raise AssertionError(
                f"{method} {path} 意外失败 {exc.code}: {payload}") from None


class HttpContractTest(unittest.TestCase):
    """每个用例使用独立内存库与临时端口，避免固定 ID 相互干扰。"""

    def setUp(self):
        handler_cls, self.store = _import_handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

        self.anon = Client(self.base_url)
        # 空库自举第一个合作社
        status, _ = self.anon.call("POST", "/v1/parties", {
            "party_id": "P-COOP", "name": "合作社", "role": "coop"})
        self.assertEqual(status, 201)
        # 首个令牌由合作社管理员线下签发（未持令牌不能直接发令牌）
        status, _ = self.anon.call("POST", "/v1/parties/P-COOP/tokens", {},
                                   expect_error=True)
        self.assertEqual(status, 401)
        import core
        issued = core.issue_token(
            self.store, {"id": "P-COOP", "role": "coop"},
            party_id="P-COOP", label="root")
        self.coop = Client(self.base_url, issued["token"])

        # 传承人小组两位成员
        self.holder_token = self._make_party("P-H1", "长者孟古古伦", "holder")
        self.holder2_token = self._make_party("P-H2", "家族成员吴玲玲", "holder")
        # 景区与商户
        self.scenic_token = self._make_party("P-SC", "中俄风情景区", "scenic")
        self.merchant_token = self._make_party("P-M", "山货商户", "merchant")
        self.holder = Client(self.base_url, self.holder_token)
        self.holder2 = Client(self.base_url, self.holder2_token)
        self.scenic = Client(self.base_url, self.scenic_token)
        self.merchant = Client(self.base_url, self.merchant_token)

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()

    def _make_party(self, pid, name, role):
        status, _ = self.coop.call("POST", "/v1/parties",
                                   {"party_id": pid, "name": name, "role": role})
        self.assertEqual(status, 201)
        _, issued = self.coop.call("POST", f"/v1/parties/{pid}/tokens",
                                   {"label": "default"})
        return issued["token"]

    def _register_content(self, cid, kind, title, sensitive="public",
                          holders=None, family=None, channels=None,
                          purposes=None, territories=None):
        _, content = self.holder.call("POST", "/v1/contents", {
            "content_id": cid, "kind": kind, "title": title,
            "family": family, "holders": holders or ["长者孟古古伦"]})
        _, lic = self.holder.call("POST", f"/v1/contents/{cid}/licenses", {
            "sensitivity": sensitive,
            "purposes": purposes or ["cultural_tourism", "medicine_tourism"],
            "territories": territories or ["新生乡", "黑河景区"],
            "channels": channels or ["景区门店", "合作社电商"]})
        return content, lic

    def _make_product(self, gid="G-1", items=None, channel="景区门店",
                      territory="黑河景区", community_bps=1000,
                      content_bps=8000):
        items = items or [
            {"content_id": "C-PAT", "share_bps": 6000},
            {"content_id": "C-STORY", "share_bps": 4000},
        ]
        _, product = self.coop.call("POST", "/v1/products", {
            "product_id": gid, "name": "桦皮盒礼盒", "purpose": "cultural_tourism",
            "territory": territory, "channel": channel,
            "content_bps": content_bps, "community_bps": community_bps,
            "items": items})
        return product

    # -- 基础契约 -----------------------------------------------------------

    def test_health_endpoint_unchanged(self):
        _, payload = self.anon.call("GET", "/health")
        self.assertEqual(payload, {"status": "ok", "service": SERVICE_ID,
                                   "name": SERVICE_NAME})
        self.assertEqual(health_payload()["service"], SERVICE_ID)

    def test_unknown_route_is_404(self):
        status, payload = self.anon.call("GET", "/unknown", expect_error=True)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "not_found")

    # -- 内容与许可 ---------------------------------------------------------

    def test_family_meaning_hidden_from_public_and_partners(self):
        self._register_content("C-PAT", "pattern", "鹿角纹",
                               family="孟古古伦家族长支符号",
                               sensitive="community")
        # 内部可见家族含义
        _, internal = self.holder.call("GET", "/v1/contents/C-PAT")
        self.assertEqual(internal["family"], "孟古古伦家族长支符号")
        # 商户无权查内容
        status, payload = self.merchant.call(
            "GET", "/v1/contents/C-PAT", expect_error=True)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "forbidden")

    def test_only_holder_group_can_grant_license(self):
        self.holder.call("POST", "/v1/contents", {
            "content_id": "C-T", "kind": "technique", "title": "熟皮技法"})
        status, payload = self.coop.call(
            "POST", "/v1/contents/C-T/licenses",
            {"sensitivity": "public", "purposes": ["cultural_tourism"]},
            expect_error=True)
        self.assertEqual(status, 403)

    def test_duplicate_grant_is_blocked_review_makes_new_version(self):
        self._register_content("C-PAT", "pattern", "鹿角纹")
        status, payload = self.holder.call(
            "POST", "/v1/contents/C-PAT/licenses",
            {"sensitivity": "public", "purposes": ["cultural_tourism"]},
            expect_error=True)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "license_exists")
        _, review = self.holder.call("POST", "/v1/contents/C-PAT/reviews", {
            "decision": "active", "reason": "年度复核，渠道增加",
            "channels": ["景区门店", "合作社电商", "省博快闪店"]})
        self.assertEqual(review["license"]["version"], 2)
        _, versions = self.holder.call("GET", "/v1/contents/C-PAT/licenses")
        self.assertEqual([v["version"] for v in versions["versions"]], [1, 2])

    # -- 批次：全部许可有效才发标识 -----------------------------------------

    def test_batch_issued_only_when_all_items_licensed(self):
        # 只有纹样有许可，故事没有
        self._register_content("C-PAT", "pattern", "鹿角纹")
        self.holder.call("POST", "/v1/contents", {
            "content_id": "C-STORY", "kind": "story", "title": "狩猎起源故事"})
        self._make_product()
        status, payload = self.coop.call(
            "POST", "/v1/products/G-1/batches", {"quantity": 100},
            expect_error=True)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "license_blocked")
        blocked_ids = {b["content_id"] for b in payload["details"]["blocked"]}
        self.assertEqual(blocked_ids, {"C-STORY"})

        # 补齐许可后发放成功，并冻结版本快照
        self.holder.call("POST", "/v1/contents/C-STORY/licenses", {
            "sensitivity": "public",
            "purposes": ["cultural_tourism", "medicine_tourism"],
            "territories": ["新生乡", "黑河景区"],
            "channels": ["景区门店", "合作社电商"]})
        _, batch = self.coop.call("POST", "/v1/products/G-1/batches",
                                  {"quantity": 100})
        self.assertEqual(batch["status"], "issued")
        self.assertEqual({s["content_id"] for s in batch["license_snapshot"]},
                         {"C-PAT", "C-STORY"})

    def test_batch_blocked_when_product_out_of_licensed_territory(self):
        self._register_content("C-PAT", "pattern", "鹿角纹",
                               territories=["新生乡"])
        self._register_content("C-STORY", "story", "狩猎起源故事",
                               territories=["新生乡", "外地"])
        self._make_product(territory="外地")
        status, payload = self.coop.call(
            "POST", "/v1/products/G-1/batches", {"quantity": 10},
            expect_error=True)
        self.assertEqual(status, 422)
        blocked = payload["details"]["blocked"][0]
        self.assertEqual(blocked["content_id"], "C-PAT")
        self.assertTrue(any("地域" in r for r in blocked["reasons"]))

    # -- 撤回 / 异议 / 复核联动 ---------------------------------------------

    def test_elder_revocation_pauses_new_production_keeps_past_sales(self):
        self._register_content("C-PAT", "pattern", "鹿角纹")
        self._register_content("C-STORY", "story", "狩猎起源故事")
        self._make_product()
        _, batch = self.coop.call("POST", "/v1/products/G-1/batches",
                                  {"quantity": 50})
        bid = batch["id"]
        # 撤回前有一笔正常销售并已结算
        _, sale = self.scenic.call("POST", "/v1/sales", {
            "external_key": "SC-2026-0001", "batch_id": bid,
            "amount_fen": 10000})
        self.assertTrue(sale["accepted"])
        first_settlement = sale["settlement_id"]

        # 长者撤回
        _, revoked = self.holder.call("POST", "/v1/contents/C-PAT/revoke",
                                      {"reason": "家族不希望继续商用"})
        self.assertEqual(revoked["status"], "revoked")
        _, refreshed = self.coop.call("GET", f"/v1/batches/{bid}")
        self.assertEqual(refreshed["status"], "paused")
        self.assertTrue(any(e["event_type"] == "paused"
                            for e in refreshed["events"]))

        # 已发生销售与结算不被抹去
        _, old = self.coop.call(
            "GET", f"/v1/settlements/{first_settlement}")
        self.assertEqual(old["total_fen"], 10000)

        # 暂停期间不受理新销售
        status, payload = self.scenic.call("POST", "/v1/sales", {
            "external_key": "SC-2026-0002", "batch_id": bid,
            "amount_fen": 10000}, expect_error=True)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "batch_not_active")

        # 复核形成新版本并恢复（条款放宽到全部渠道后满足当前商品）
        _, review = self.holder.call("POST", "/v1/contents/C-PAT/reviews", {
            "decision": "active", "reason": "家族会议讨论后有条件恢复",
            "channels": ["景区门店", "合作社电商"],
            "territories": ["新生乡", "黑河景区"]})
        self.assertEqual(review["license"]["version"], 3)
        self.assertIn(bid, review["resumed_batches"])
        _, refreshed = self.coop.call("GET", f"/v1/batches/{bid}")
        self.assertEqual(refreshed["status"], "issued")

    def test_family_dispute_suspends_and_review_decides(self):
        self._register_content("C-PAT", "pattern", "鹿角纹")
        self._register_content("C-STORY", "story", "狩猎起源故事",
                               sensitive="restricted")
        self._make_product()
        _, batch = self.coop.call("POST", "/v1/products/G-1/batches",
                                  {"quantity": 10})
        bid = batch["id"]
        _, dispute = self.holder2.call(
            "POST", "/v1/contents/C-STORY/disputes",
            {"reason": "该故事只应族内传承，反对商用"})
        self.assertEqual(dispute["status"], "suspended")
        _, refreshed = self.coop.call("GET", f"/v1/batches/{bid}")
        self.assertEqual(refreshed["status"], "paused")

        # 复核维持撤回：批次不恢复
        _, review = self.holder.call("POST", "/v1/contents/C-STORY/reviews", {
            "decision": "revoked", "reason": "小组尊重家族意见，停止商用"})
        self.assertEqual(review["resumed_batches"], [])
        _, refreshed = self.coop.call("GET", f"/v1/batches/{bid}")
        self.assertEqual(refreshed["status"], "paused")

    def test_product_channel_change_beyond_terms_pauses_batch(self):
        self._register_content("C-PAT", "pattern", "鹿角纹",
                               channels=["景区门店"])
        self._register_content("C-STORY", "story", "狩猎起源故事")
        self._make_product(channel="景区门店")
        _, batch = self.coop.call("POST", "/v1/products/G-1/batches",
                                  {"quantity": 5})
        bid = batch["id"]
        self.coop.call("PATCH", "/v1/products/G-1",
                       {"channel": "第三方直播"})
        _, refreshed = self.coop.call("GET", f"/v1/batches/{bid}")
        self.assertEqual(refreshed["status"], "paused")

    # -- 公开核验 -----------------------------------------------------------

    def test_public_verify_reveals_only_minimum(self):
        self._register_content("C-PAT", "pattern", "鹿角纹",
                               family="家族秘义")
        self._register_content("C-STORY", "story", "狩猎起源故事",
                               sensitive="restricted")
        self._make_product()
        _, batch = self.coop.call("POST", "/v1/products/G-1/batches",
                                  {"quantity": 3})
        status, public = self.anon.call(
            "GET", f"/v1/verify/{batch['id']}")
        self.assertEqual(status, 200)
        self.assertTrue(public["valid"])
        self.assertEqual(public["product"], {"name": "桦皮盒礼盒"})
        serialized = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("家族秘义", serialized)
        self.assertNotIn("狩猎起源故事", serialized)  # 受限知识不公开标题
        self.assertNotIn("restricted", serialized)
        self.assertTrue(any("鹿角纹" in c["title"]
                            for c in public["required_credits"]))
        self.assertTrue(any("族内传承" in c["title"]
                            for c in public["required_credits"]))

    def test_public_verify_shows_paused_notice(self):
        self._register_content("C-PAT", "pattern", "鹿角纹")
        self._register_content("C-STORY", "story", "狩猎起源故事")
        self._make_product()
        _, batch = self.coop.call("POST", "/v1/products/G-1/batches", {})
        bid = batch["id"]
        self.coop.call("POST", f"/v1/batches/{bid}/pause",
                       {"reason": "例行核查"})
        _, public = self.anon.call("GET", f"/v1/verify/{bid}")
        self.assertFalse(public["valid"])
        self.assertEqual(public["status"], "paused")
        self.assertIn("此前发生的销售", public["notice"])

    # -- 销售去重与超渠道 ---------------------------------------------------

    def test_same_sale_reported_by_scenic_and_merchant_settles_once(self):
        self._register_content("C-PAT", "pattern", "鹿角纹")
        self._register_content("C-STORY", "story", "狩猎起源故事")
        self._make_product()
        _, batch = self.coop.call("POST", "/v1/products/G-1/batches", {})
        bid = batch["id"]
        payload = {"external_key": "TICKET-7788", "batch_id": bid,
                   "amount_fen": 20000}
        _, first = self.scenic.call("POST", "/v1/sales", payload)
        self.assertTrue(first["accepted"])
        # 商户用同一流水号重复上报
        _, dup = self.merchant.call("POST", "/v1/sales", payload)
        self.assertFalse(dup["accepted"])
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["settlement_id"], first["settlement_id"])
        # 景区自己再报一次仍是重复
        _, dup2 = self.scenic.call("POST", "/v1/sales", payload)
        self.assertTrue(dup2["duplicate"])
        # 台账只有一笔销售，但重复计数为 2
        _, sales = self.coop.call("GET", f"/v1/sales?batch_id={bid}")
        self.assertEqual(len(sales["sales"]), 1)
        self.assertEqual(sales["sales"][0]["duplicate_count"], 2)
        _, settlements = self.coop.call(
            "GET", f"/v1/settlements?batch_id={bid}")
        self.assertEqual(len(settlements["settlements"]), 1)

    def test_sale_outside_channel_pauses_batch_and_is_rejected(self):
        self._register_content("C-PAT", "pattern", "鹿角纹")
        self._register_content("C-STORY", "story", "狩猎起源故事")
        self._make_product()
        _, batch = self.coop.call("POST", "/v1/products/G-1/batches", {})
        bid = batch["id"]
        status, payload = self.scenic.call("POST", "/v1/sales", {
            "external_key": "OUT-1", "batch_id": bid, "amount_fen": 5000,
            "channel": "外地流动摊位", "territory": "外地"}, expect_error=True)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "outside_licensed_terms")
        _, refreshed = self.coop.call("GET", f"/v1/batches/{bid}")
        self.assertEqual(refreshed["status"], "paused")
        # 被拒的一笔没有结算记录
        _, sales = self.coop.call("GET", f"/v1/sales?batch_id={bid}")
        self.assertEqual(sales["sales"], [])

    # -- 分账与解释 ---------------------------------------------------------

    def test_settlement_splits_by_frozen_versions_and_explains(self):
        self._register_content("C-PAT", "pattern", "鹿角纹")
        self._register_content("C-STORY", "story", "狩猎起源故事")
        self._make_product(content_bps=8000, community_bps=1000)
        _, batch = self.coop.call("POST", "/v1/products/G-1/batches", {})
        bid = batch["id"]
        _, sale = self.scenic.call("POST", "/v1/sales", {
            "external_key": "SPLIT-1", "batch_id": bid,
            "amount_fen": 10000})  # 100 元
        sid = sale["settlement_id"]
        _, st = self.coop.call("GET", f"/v1/settlements/{sid}")
        # 内容收益 80 元；社区基金 10% = 8 元；剩余 72 元按 60/40
        # 两项内容保管人都是 P-H1，其两条分账行应合计 72 元
        by_payee = {}
        for ln in st["lines"]:
            by_payee[ln["payee"]] = by_payee.get(ln["payee"], 0) + ln["amount_fen"]
        self.assertEqual(by_payee["COMMUNITY_FUND"], 800)
        self.assertEqual(by_payee["P-H1"], 7200)
        self.assertIn("冻结", st["rationale"])
        self.assertIn("许可 v1", st["lines"][1]["rule"])
        self.assertEqual(sum(ln["amount_fen"] for ln in st["lines"]), 8000)

    def test_settlement_uses_snapshot_even_after_license_review(self):
        self._register_content("C-PAT", "pattern", "鹿角纹")
        self._register_content("C-STORY", "story", "狩猎起源故事")
        self._make_product()
        _, batch = self.coop.call("POST", "/v1/products/G-1/batches", {})
        bid = batch["id"]
        # 许可复核出 v2（恢复批次），再产生销售
        self.holder.call("POST", "/v1/contents/C-PAT/disputes",
                         {"reason": "临时核对"})
        self.holder.call("POST", "/v1/contents/C-PAT/reviews", {
            "decision": "active", "reason": "核对无误"})
        _, sale = self.scenic.call("POST", "/v1/sales", {
            "external_key": "SNAP-1", "batch_id": bid,
            "amount_fen": 10000})
        _, st = self.coop.call("GET", f"/v1/settlements/{sale['settlement_id']}")
        # 批次发放时冻结的是 v1，分账规则解释沿用 v1，不随新版本漂移
        content_lines = [ln for ln in st["lines"]
                         if ln["payee"] != "COMMUNITY_FUND"]
        self.assertTrue(all("v1" in ln["rule"] for ln in content_lines))

    def test_holder_sees_only_own_lines_but_coop_sees_all(self):
        self._register_content("C-PAT", "pattern", "鹿角纹",
                               holders=["长者孟古古伦"])
        # C-STORY 由第二位传承人保管
        self.holder2.call("POST", "/v1/contents", {
            "content_id": "C-STORY", "kind": "story",
            "title": "狩猎起源故事", "holders": ["吴玲玲"]})
        self.holder2.call("POST", "/v1/contents/C-STORY/licenses", {
            "sensitivity": "public",
            "purposes": ["cultural_tourism", "medicine_tourism"],
            "territories": ["新生乡", "黑河景区"],
            "channels": ["景区门店", "合作社电商"]})
        self._make_product()
        _, batch = self.coop.call("POST", "/v1/products/G-1/batches", {})
        _, sale = self.scenic.call("POST", "/v1/sales", {
            "external_key": "VIS-1", "batch_id": batch["id"],
            "amount_fen": 10000})
        sid = sale["settlement_id"]
        _, mine = self.holder2.call("GET", f"/v1/settlements/{sid}")
        self.assertEqual({ln["payee"] for ln in mine["lines"]}, {"P-H2"})
        _, all_lines = self.coop.call("GET", f"/v1/settlements/{sid}")
        self.assertIn("COMMUNITY_FUND",
                      {ln["payee"] for ln in all_lines["lines"]})
        # 商户只能确认结算状态，看不到分账明细
        status, payload = self.merchant.call(
            "GET", f"/v1/settlements/{sid}", expect_error=True)
        self.assertEqual(status, 403)


class LicenseEvaluationUnitTest(unittest.TestCase):
    """许可期限/用途/地域/渠道的逐条评估。"""

    def _lic(self, **overrides):
        lic = {
            "version": 1, "status": "active",
            "valid_from": "2026-01-01T00:00:00+00:00",
            "valid_until": "2026-12-31T00:00:00+00:00",
            "purposes": ["cultural_tourism"], "territories": ["新生乡"],
            "channels": ["景区门店"],
        }
        lic.update(overrides)
        return lic

    def test_valid_license_passes_all_checks(self):
        import core
        ok, reasons = core.evaluate_license(
            self._lic(), at="2026-06-01T00:00:00+00:00",
            purpose="cultural_tourism", territory="新生乡", channel="景区门店")
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_expired_or_suspended_is_blocked(self):
        import core
        ok, reasons = core.evaluate_license(
            self._lic(), at="2027-01-01T00:00:00+00:00")
        self.assertFalse(ok)
        self.assertTrue(any("到期" in r for r in reasons))
        ok, reasons = core.evaluate_license(
            self._lic(status="suspended"), at="2026-06-01T00:00:00+00:00")
        self.assertFalse(ok)
        self.assertTrue(any("suspended" in r for r in reasons))

    def test_wrong_purpose_territory_channel_reported(self):
        import core
        ok, reasons = core.evaluate_license(
            self._lic(), at="2026-06-01T00:00:00+00:00",
            purpose="medicine_tourism", territory="外地", channel="直播")
        self.assertFalse(ok)
        joined = "/".join(reasons)
        self.assertIn("用途", joined)
        self.assertIn("地域", joined)
        self.assertIn("渠道", joined)


class AllocationUnitTest(unittest.TestCase):
    """分账取整与余数规则的直接单元测试。"""

    def test_largest_remainder_leaves_no_loss(self):
        import core

        class Batch(dict):
            pass

        batch = Batch(
            id="B-TEST",
            content_bps=10000, community_bps=0,
            product={"name": "测试商品"},
            license_snapshot=[
                {"custodian_id": "A", "custodian_name": "甲",
                 "content_id": "1", "license_id": "L1", "version": 1,
                 "share_bps": 3333, "title": "一"},
                {"custodian_id": "B", "custodian_name": "乙",
                 "content_id": "2", "license_id": "L2", "version": 1,
                 "share_bps": 3333, "title": "二"},
                {"custodian_id": "C", "custodian_name": "丙",
                 "content_id": "3", "license_id": "L3", "version": 1,
                 "share_bps": 3334, "title": "三"},
            ])
        lines, rationale, pool = core._allocate(10000, batch)
        self.assertEqual(pool, 10000)
        self.assertEqual(sum(ln["amount_fen"] for ln in lines), 10000)
        amounts = sorted(ln["amount_fen"] for ln in lines)
        self.assertEqual(amounts, [3333, 3333, 3334])


if __name__ == "__main__":
    unittest.main()
