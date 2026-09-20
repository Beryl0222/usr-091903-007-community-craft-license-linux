"""领域规则测试：覆盖许可门控、撤回异议复核、渠道违约、销售去重与分账解释。"""

import os
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal

from licensing import (
    COMMUNITY_FUND,
    ConflictError,
    DomainError,
    NotFoundError,
    Store,
)

FIXED_NOW = datetime(2026, 9, 20, 10, 0, 0)


def make_store(**kwargs):
    return Store(clock=lambda: FIXED_NOW, **kwargs)


class CommunityScenarioTest(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        # 可公开的家族纹样（讲解员可讲、可印制公开说明）
        self.pattern = self.store.register_content(
            "pattern", "桦皮盒云卷纹",
            source_persons=["吴奶奶"], source_families=["吴氏家族"],
            sensitivity="public", public_summary="鄂伦春桦皮盒上的云卷纹样",
            recorder="合作社",
        )
        # 族内传承的药用知识（不公开）
        self.medicine = self.store.register_content(
            "medicine", "草本配伍方·内部",
            source_persons=["葛大夫"], source_families=["葛氏家族"],
            sensitivity="restricted", recorder="合作社",
        )
        # 社区范围的口述故事
        self.story = self.store.register_content(
            "story", "古驿道迁徙故事",
            source_persons=["孟长老"], sensitivity="community",
            public_summary="鄂伦春人沿古驿道迁徙的片段", recorder="合作社",
        )

    def _issue_licenses(self):
        lp = self.store.issue_license(
            self.pattern["id"], scope="medicine_tourism",
            territories=["黑河市"], channels=["景区门店", "线上旗舰店"],
            expires_on="2026-12-31",
            shares=[{"payee": "吴奶奶", "share": 0.6, "note": "纹样传承人"}],
            issued_by="传承人小组",
        )
        lm = self.store.issue_license(
            self.medicine["id"], scope="medicine_tourism",
            territories=["黑河市"], channels=["景区门店"],
            shares=[{"payee": "葛大夫", "share": 0.5}],
            issued_by="传承人小组",
        )
        ls = self.store.issue_license(
            self.story["id"], scope="tourism",
            channels=["*"], territories=["*"],
            shares=[{"payee": "孟长老", "share": 0.4}],
            issued_by="传承人小组",
        )
        return lp, lm, ls

    def _combo_product(self):
        return self.store.register_product(
            "云卷纹药香礼盒",
            [
                {"content_id": self.pattern["id"], "weight": 1},
                {"content_id": self.medicine["id"], "weight": 1},
            ],
            purpose="medicine_tourism", channels=["景区门店"], territories=["黑河市"],
            producer="乡合作社加工厂",
        )

    # -- 基础登记 -----------------------------------------------------------

    def test_invalid_kind_and_sensitivity_rejected(self):
        with self.assertRaises(DomainError):
            self.store.register_content("song", "x")
        with self.assertRaises(DomainError):
            self.store.register_content("pattern", "x", sensitivity="secret")
        with self.assertRaises(DomainError):
            self.store.register_content("pattern", "  ")

    def test_share_total_cannot_exceed_one(self):
        with self.assertRaises(DomainError):
            self.store.issue_license(
                self.pattern["id"],
                shares=[{"payee": "甲", "share": 0.7}, {"payee": "乙", "share": 0.5}],
            )

    def test_attribution_defaults_to_source_persons(self):
        license_, _, _ = self._issue_licenses()
        self.assertEqual(license_["current"]["terms"]["attribution"], ["吴奶奶"])

    # -- 批次门控 -----------------------------------------------------------

    def test_batch_requires_every_license_valid(self):
        self._issue_licenses()
        product = self._combo_product()
        batch = self.store.issue_batch(
            product["id"], quantity=100, producer="乡合作社加工厂",
            channel="景区门店", location="黑河市",
        )
        self.assertTrue(batch["label"].startswith("HRXS-"))
        self.assertEqual(len(batch["snapshot"]), 2)
        self.assertEqual([s["version"] for s in batch["snapshot"]], [1, 1])

    def test_batch_blocked_when_license_missing(self):
        # 只为纹样发许可，药用知识没有许可
        self.store.issue_license(self.pattern["id"], scope="medicine_tourism", issued_by="小组")
        product = self.store.register_product(
            "半成品礼盒",
            [{"content_id": self.pattern["id"]}, {"content_id": self.medicine["id"]}],
            purpose="medicine_tourism", channels=["*"], territories=["*"],
        )
        with self.assertRaises(ConflictError) as ctx:
            self.store.issue_batch(
                product["id"], quantity=10, producer="厂",
                channel="景区门店", location="黑河市",
            )
        self.assertIn("没有用途为 药旅 的许可", "；".join(ctx.exception.detail["reasons"]))

    def test_batch_blocked_for_expired_license_or_wrong_channel(self):
        self.store.issue_license(
            self.pattern["id"], scope="medicine_tourism",
            channels=["景区门店"], territories=["黑河市"],
            effective_on="2025-01-01", expires_on="2026-01-01", issued_by="小组",
        )
        self.store.issue_license(
            self.medicine["id"], scope="medicine_tourism",
            channels=["景区门店"], territories=["黑河市"], issued_by="小组",
        )
        product = self._combo_product()
        with self.assertRaises(ConflictError) as ctx:
            self.store.issue_batch(
                product["id"], quantity=10, producer="厂",
                channel="景区门店", location="黑河市",
            )
        self.assertTrue(any("到期" in r for r in ctx.exception.detail["reasons"]))

        # 新商品放开商品级渠道限制后，许可本身不允许“外地集市”，仍应被拦
        open_product = self.store.register_product(
            "开放渠道礼盒",
            [{"content_id": self.pattern["id"]}, {"content_id": self.medicine["id"]}],
            purpose="medicine_tourism", channels=["*"], territories=["*"],
        )
        with self.assertRaises(ConflictError) as other:
            self.store.issue_batch(
                open_product["id"], quantity=10, producer="厂",
                channel="外地集市", location="黑河市",
            )
        self.assertTrue(any("不允许渠道" in r for r in other.exception.detail["reasons"]))

    def test_product_level_channel_guard(self):
        self._issue_licenses()
        product = self._combo_product()
        with self.assertRaises(ConflictError):
            self.store.issue_batch(
                product["id"], quantity=10, producer="厂",
                channel="线上旗舰店", location="黑河市",  # 商品只允许景区门店
            )

    # -- 撤回：暂停新生产，不动已发生销售 -----------------------------------

    def test_elder_withdrawal_freezes_new_batches_but_keeps_past_sales(self):
        lp, lm, _ = self._issue_licenses()
        product = self._combo_product()
        batch = self.store.issue_batch(
            product["id"], quantity=50, producer="乡合作社加工厂",
            channel="景区门店", location="黑河市",
        )
        sale = self.store.report_sale(
            batch["id"], amount="100.00", reporter="景区服务中心",
            reporter_type="scenic_area", sale_ref="T-0001",
        )
        self.assertEqual(sale["duplicate"], False)

        # 长者撤回纹样许可
        self.store.withdraw_license(lp["id"], withdrawn_by="吴奶奶", reason="家族纹样使用方式需重新商定")
        with self.assertRaises(ConflictError) as ctx:
            self.store.issue_batch(
                product["id"], quantity=50, producer="乡合作社加工厂",
                channel="景区门店", location="黑河市",
            )
        self.assertIn("已暂停", "；".join(ctx.exception.detail["reasons"]))

        # 已发生的销售仍在、仍结算
        kept = self.store.sale(sale["id"])
        self.assertEqual(kept["amount_cents"], 10000)

    def test_review_revise_creates_new_version_and_batches_use_it(self):
        lp, _, _ = self._issue_licenses()
        product = self._combo_product()
        objection = self.store.withdraw_license(lp["id"], withdrawn_by="吴奶奶", reason="缩小渠道")
        with self.assertRaises(ConflictError):
            self.store.issue_batch(
                product["id"], quantity=10, producer="厂",
                channel="景区门店", location="黑河市",
            )
        self.store.review_objection(
            objection["id"], "revise", reviewer="传承人小组",
            new_terms={"channels": ["景区门店"], "shares": [{"payee": "吴奶奶", "share": 0.8}]},
            note="提高署名传承人份额，仅限景区门店",
        )
        updated = self.store.license(lp["id"])
        self.assertEqual(updated["current_version"], 2)
        self.assertEqual(updated["versions"][0]["status"], "superseded")
        self.assertEqual(updated["current"]["status"], "active")

        batch = self.store.issue_batch(
            product["id"], quantity=10, producer="厂",
            channel="景区门店", location="黑河市",
        )
        pattern_snap = next(s for s in batch["snapshot"] if s["content_id"] == self.pattern["id"])
        self.assertEqual(pattern_snap["version"], 2)
        self.assertEqual(pattern_snap["shares"][0]["share"], 0.8)

    def test_review_restore_and_uphold(self):
        lp, _, _ = self._issue_licenses()
        objection = self.store.withdraw_license(lp["id"], withdrawn_by="吴奶奶", reason="待核实")
        self.store.review_objection(objection["id"], "restore", reviewer="小组", note="误会已澄清")
        self.assertEqual(self.store.license(lp["id"])["current"]["status"], "active")

        again = self.store.withdraw_license(lp["id"], withdrawn_by="吴奶奶", reason="不同意继续")
        self.store.review_objection(again["id"], "uphold", reviewer="小组")
        self.assertEqual(self.store.license(lp["id"])["current"]["status"], "revoked")
        # 两次撤回/复核过程都保留可追溯
        self.assertEqual(len(self.store.list_objections()), 2)
        self.assertTrue(all(o["status"] == "closed" for o in self.store.list_objections()))

    def test_family_objection_suspends_all_active_licenses_of_content(self):
        lp, _, ls = self._issue_licenses()
        # 纹样同时在文旅场景有另一个商品/许可时也应一并暂停；这里直接复核同一许可
        objection = self.store.raise_objection(
            self.pattern["id"], raised_by="吴氏家族成员·吴小军",
            reason="纹样具有家族含义，商户在随意印制", relation="family_member",
        )
        affected = {(a["license_id"], a["version"]) for a in objection["affected"]}
        self.assertIn((lp["id"], 1), affected)
        self.assertEqual(self.store.license(lp["id"])["current"]["status"], "suspended")

    # -- 渠道违约 -----------------------------------------------------------

    def test_channel_breach_suspends_new_production_and_review_versions(self):
        self._issue_licenses()
        product = self._combo_product()
        self.store.issue_batch(
            product["id"], quantity=50, producer="厂",
            channel="景区门店", location="黑河市",
        )
        self.store.report_channel_breach(product["id"], reported_by="巡店员", detail="商品流入外地批发市场")
        self.assertEqual(self.store.product(product["id"])["status"], "suspended")

        # 修订渠道约定后形成新版本并恢复
        revised = self.store.review_product(
            product["id"], "revise", reviewer="理事会",
            changes={"channels": ["景区门店", "游客中心"]}, note="加贴防伪，扩展游客中心",
        )
        self.assertEqual(revised["status"], "active")
        self.assertEqual(revised["revision"], 2)

    # -- 销售去重 -----------------------------------------------------------

    def _one_batch(self):
        self._issue_licenses()
        product = self._combo_product()
        return self.store.issue_batch(
            product["id"], quantity=50, producer="乡合作社加工厂",
            channel="景区门店", location="黑河市",
        )

    def test_duplicate_sale_from_scenic_area_and_merchant_settles_once(self):
        batch = self._one_batch()
        first = self.store.report_sale(
            batch["id"], amount="200.00", reporter="景区服务中心",
            reporter_type="scenic_area", sale_ref="T-20260920-007",
        )
        second = self.store.report_sale(
            batch["id"], amount="200.00", reporter="山珍商户老李",
            reporter_type="merchant", sale_ref="T-20260920-007",
        )
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["duplicate_count"], 1)
        # 台账只有一笔销售
        sales = self.store.list_sales()
        self.assertEqual(len(sales), 1)

        # 同一上报方再报则直接拒绝
        with self.assertRaises(ConflictError):
            self.store.report_sale(
                batch["id"], amount="200.00", reporter="景区服务中心",
                reporter_type="scenic_area", sale_ref="T-20260920-007",
            )

    def test_sale_outside_agreed_channel_is_kept_and_suspends_production(self):
        batch = self._one_batch()
        sale = self.store.report_sale(
            batch["id"], amount="80.00", reporter="商户老李",
            reporter_type="merchant", sale_ref="T-0099",
            channel="外地批发市场", location="外地",
        )
        # 销售不抹去，照样结算
        self.assertTrue(sale["out_of_scope"])
        self.assertEqual(sale["amount_cents"], 8000)
        # 新生产被暂停
        self.assertEqual(self.store.product(batch["product_id"])["status"], "suspended")

    # -- 分账与解释 ---------------------------------------------------------

    def test_settlement_splits_by_weight_then_shares_and_balances(self):
        batch = self._one_batch()
        sale = self.store.report_sale(
            batch["id"], amount="100.00", reporter="景区服务中心",
            reporter_type="scenic_area", sale_ref="T-100",
        )
        settlement = sale["settlement"]
        lines = settlement["lines"]
        # 两项内容等权：各 50.00 元池子
        pools = {}
        for line in lines:
            pools.setdefault(line["content_id"], line["content_pool_cents"])
        self.assertEqual(set(pools.values()), {5000})

        by_payee = {}
        for line in lines:
            by_payee[line["payee"]] = by_payee.get(line["payee"], 0) + line["cents"]
        # 纹样：吴奶奶 60% × 50 = 30；药：葛大夫 50% × 50 = 25；基金 = 20+25 = 45
        self.assertEqual(by_payee["吴奶奶"], 3000)
        self.assertEqual(by_payee["葛大夫"], 2500)
        self.assertEqual(by_payee[COMMUNITY_FUND], 4500)
        self.assertEqual(sum(l["cents"] for l in lines), 10000)
        # 每行可追溯到具体许可版本，且整体规则有文字说明
        self.assertTrue(all(l["license_version"] == 1 for l in lines))
        self.assertIn("锁定", settlement["rule_basis"])

    def test_settlement_rounding_leaves_no_cent_behind(self):
        batch = self._one_batch()
        sale = self.store.report_sale(
            batch["id"], amount="0.05", reporter="商户", reporter_type="merchant",
            sale_ref="T-tiny",
        )
        lines = sale["settlement"]["lines"]
        self.assertEqual(sum(l["cents"] for l in lines), 5)

    def test_ledger_aggregates_and_remains_stable_after_license_change(self):
        batch = self._one_batch()
        self.store.report_sale(
            batch["id"], amount="100.00", reporter="景区", reporter_type="scenic_area",
            sale_ref="T-1",
        )
        # 许可修订为新版本
        lp = self.store._license_for_content(self.pattern["id"], "medicine_tourism")
        objection = self.store.withdraw_license(lp["id"], withdrawn_by="吴奶奶", reason="调整")
        self.store.review_objection(
            objection["id"], "revise", reviewer="小组",
            new_terms={"shares": [{"payee": "吴奶奶", "share": 0.9}]},
        )
        self.store.report_sale(
            batch["id"], amount="100.00", reporter="商户", reporter_type="merchant",
            sale_ref="T-2",
        )
        ledger = self.store.ledger()
        totals = {e["payee"]: e["total_cents"] for e in ledger["entries"]}
        # 旧批次两笔销售都按 v1 快照：吴奶奶 30+30，葛大夫 25+25，基金 45+45
        self.assertEqual(totals["吴奶奶"], 6000)
        self.assertEqual(totals["葛大夫"], 5000)
        self.assertEqual(totals[COMMUNITY_FUND], 9000)

    def test_amount_validation(self):
        batch = self._one_batch()
        with self.assertRaises(DomainError):
            self.store.report_sale(
                batch["id"], amount="1.234", reporter="x",
                reporter_type="merchant", sale_ref="T-bad",
            )
        with self.assertRaises(DomainError):
            self.store.report_sale(
                batch["id"], amount=-1, reporter="x",
                reporter_type="merchant", sale_ref="T-bad2",
            )

    # -- 公开查询脱敏 -------------------------------------------------------

    def test_public_verify_hides_restricted_knowledge(self):
        batch = self._one_batch()
        view = self.store.verify_batch(batch["id"])
        self.assertTrue(view["valid"])
        kinds = {c["kind"] for c in view["contents"]}
        self.assertEqual(kinds, {"纹样", "药用知识"})
        medicine_brief = next(c for c in view["contents"] if c["kind"] == "药用知识")
        self.assertIsNone(medicine_brief["title"])
        self.assertIn("不予公开", medicine_brief["note"])
        # 必要署名仍给出
        self.assertIn("葛大夫", medicine_brief["attribution"])
        pattern_brief = next(c for c in view["contents"] if c["kind"] == "纹样")
        self.assertEqual(pattern_brief["title"], "桦皮盒云卷纹")

    def test_public_content_brief_only_for_public(self):
        brief = self.store.public_content_brief(self.pattern["id"])
        self.assertEqual(brief["title"], "桦皮盒云卷纹")
        with self.assertRaises(NotFoundError):
            self.store.public_content_brief(self.medicine["id"])

    def test_verify_unknown_batch_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.store.verify_batch("B9999-nope")


class PersistenceTest(unittest.TestCase):
    def test_store_roundtrips_through_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            store = Store(path=path, clock=lambda: FIXED_NOW)
            content = store.register_content("pattern", "持久化纹样", sensitivity="public")
            store.issue_license(content["id"], scope="tourism",
                                shares=[{"payee": "甲", "share": 0.3}])

            reopened = Store(path=path, clock=lambda: FIXED_NOW)
            self.assertEqual(reopened.content(content["id"])["title"], "持久化纹样")
            licenses = reopened.list_licenses()
            self.assertEqual(len(licenses), 1)
            self.assertEqual(licenses[0]["current"]["shares"][0]["payee"], "甲")


if __name__ == "__main__":
    unittest.main()
