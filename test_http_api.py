"""HTTP 接口契约测试：鉴权、错误码、公开脱敏与完整业务链路。"""

import json
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from licensing import Store, _empty_data
from service import make_server

TOKEN = "coop-test-token"
FIXED_NOW = datetime(2026, 9, 20, 10, 0, 0)


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(clock=lambda: FIXED_NOW)
        cls.server = make_server("127.0.0.1", 0, cls.store, TOKEN)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    def setUp(self):
        # 每个用例使用干净数据，但复用同一个服务器
        self.store.data = _empty_data()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, body=None, *, token=None, raw=False):
        data = None
        headers = {}
        if body is not None:
            data = body if raw else json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            payload = json.load(error)
            error.close()
            return error.code, payload

    def test_internal_endpoint_requires_token(self):
        status, payload = self.request("GET", "/api/contents")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "unauthorized")

        status, payload = self.request("GET", "/api/contents", token="wrong")
        self.assertEqual(status, 401)

    def test_missing_field_is_bad_request(self):
        status, payload = self.request("POST", "/api/contents", {"kind": "pattern"}, token=TOKEN)
        self.assertEqual(status, 400)
        self.assertEqual(payload["detail"]["field"], "title")

    def test_malformed_json_is_bad_request(self):
        status, payload = self.request(
            "POST", "/api/contents", b"{not-json", token=TOKEN, raw=True
        )
        self.assertEqual(status, 400)

    def test_full_community_workflow_over_http(self):
        # 1) 登记两项内容
        status, pattern = self.request("POST", "/api/contents", {
            "kind": "pattern", "title": "神雀纹",
            "source_persons": ["吴奶奶"], "sensitivity": "public",
            "public_summary": "鄂伦春信仰中的神雀纹样",
        }, token=TOKEN)
        self.assertEqual(status, 201)
        status, medicine = self.request("POST", "/api/contents", {
            "kind": "medicine", "title": "外用草药配伍·内部",
            "source_persons": ["葛大夫"], "sensitivity": "restricted",
        }, token=TOKEN)
        self.assertEqual(status, 201)

        # 2) 先登记商品组合，此时无许可，批次必须被门控拦下
        status, product = self.request("POST", "/api/products", {
            "name": "神雀养生伴手礼",
            "items": [
                {"content_id": pattern["id"], "weight": 2},
                {"content_id": medicine["id"], "weight": 1},
            ],
            "purpose": "medicine_tourism",
            "channels": ["景区门店"], "territories": ["黑河市"],
            "producer": "加工厂",
        }, token=TOKEN)
        self.assertEqual(status, 201)

        status, blocked = self.request(
            "POST", f"/api/products/{product['id']}/batches",
            {"quantity": 30, "producer": "加工厂", "channel": "景区门店", "location": "黑河市"},
            token=TOKEN,
        )
        self.assertEqual(status, 409)
        self.assertTrue(blocked["detail"]["reasons"])

        # 3) 两项许可齐备后才能签发批次
        for content, shares in (
            (pattern, [{"payee": "吴奶奶", "share": 0.6}]),
            (medicine, [{"payee": "葛大夫", "share": 0.5}]),
        ):
            status, license_ = self.request(
                "POST", f"/api/contents/{content['id']}/licenses",
                {"scope": "medicine_tourism", "channels": ["景区门店"],
                 "territories": ["黑河市"], "shares": shares, "issued_by": "传承人小组"},
                token=TOKEN,
            )
            self.assertEqual(status, 201)

        status, batch = self.request(
            "POST", f"/api/products/{product['id']}/batches",
            {"quantity": 30, "producer": "加工厂", "channel": "景区门店", "location": "黑河市"},
            token=TOKEN,
        )
        self.assertEqual(status, 201)
        self.assertTrue(batch["label"].startswith("HRXS-"))

        # 4) 公开验证无需令牌：只暴露必要说明
        status, public_view = self.request("GET", f"/v/batches/{batch['id']}")
        self.assertEqual(status, 200)
        self.assertTrue(public_view["valid"])
        self.assertEqual(public_view["product_name"], "神雀养生伴手礼")
        medicine_brief = next(c for c in public_view["contents"] if c["kind"] == "药用知识")
        self.assertIsNone(medicine_brief["title"])
        self.assertNotIn("外用草药配伍", json.dumps(public_view, ensure_ascii=False))

        status, missing = self.request("GET", "/v/batches/B0000-xxxx")
        self.assertEqual(status, 404)

        # 5) 景区与商户重复上报同一笔销售，只结算一次
        sale_payload = {
            "amount": "90.00", "sale_ref": "T-HTTP-1",
            "reporter": "景区服务中心", "reporter_type": "scenic_area",
        }
        status, first = self.request(
            "POST", f"/api/batches/{batch['id']}/sales", sale_payload, token=TOKEN)
        self.assertEqual(status, 201)
        self.assertFalse(first["duplicate"])

        sale_payload.update({"reporter": "商户老李", "reporter_type": "merchant"})
        status, second = self.request(
            "POST", f"/api/batches/{batch['id']}/sales", sale_payload, token=TOKEN)
        self.assertEqual(status, 201)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["id"], first["id"])

        status, sales = self.request("GET", "/api/sales", token=TOKEN)
        self.assertEqual(len(sales["sales"]), 1)

        # 权重 2:1 → 纹样池 60 元、药用池 30 元
        # 吴奶奶 60×0.6=36；葛大夫 30×0.5=15；基金 = 24+15 = 39
        status, ledger = self.request("GET", "/api/ledger", token=TOKEN)
        self.assertEqual(status, 200)
        totals = {e["payee"]: e["total_cents"] for e in ledger["entries"]}
        self.assertEqual(totals["吴奶奶"], 3600)
        self.assertEqual(totals["葛大夫"], 1500)
        self.assertEqual(totals["community_fund"], 3900)

        # 6) 家族异议：新批次暂停；复核修订后按新版本放行
        status, objection = self.request(
            "POST", f"/api/contents/{pattern['id']}/objections",
            {"raised_by": "吴氏家族·吴小军", "reason": "家族纹样被商户随意印制",
             "relation": "family_member"},
            token=TOKEN,
        )
        self.assertEqual(status, 201)
        status, blocked_after = self.request(
            "POST", f"/api/products/{product['id']}/batches",
            {"quantity": 10, "producer": "加工厂", "channel": "景区门店", "location": "黑河市"},
            token=TOKEN,
        )
        self.assertEqual(status, 409)

        status, reviewed = self.request(
            "POST", f"/api/objections/{objection['id']}/review",
            {"decision": "revise", "reviewer": "传承人小组",
             "new_terms": {"shares": [{"payee": "吴奶奶", "share": 0.8}]}},
            token=TOKEN,
        )
        self.assertEqual(status, 200)
        self.assertEqual(reviewed["resolution"]["decision"], "revise")

        status, batch2 = self.request(
            "POST", f"/api/products/{product['id']}/batches",
            {"quantity": 10, "producer": "加工厂", "channel": "景区门店", "location": "黑河市"},
            token=TOKEN,
        )
        self.assertEqual(status, 201)
        pattern_snap = next(s for s in batch2["snapshot"] if s["content_id"] == pattern["id"])
        self.assertEqual(pattern_snap["version"], 2)

        # 7) 已发生的那笔销售仍可查询、没有被抹去
        status, kept = self.request("GET", f"/api/sales/{first['id']}", token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(kept["amount_cents"], 9000)

    def test_channel_breach_on_sale_suspends_product(self):
        status, content = self.request("POST", "/api/contents", {
            "kind": "pattern", "title": "鹿头纹", "sensitivity": "public",
        }, token=TOKEN)
        self.assertEqual(status, 201)
        status, product = self.request("POST", "/api/products", {
            "name": "鹿头纹杯垫",
            "items": [{"content_id": content["id"]}],
            "purpose": "tourism", "channels": ["景区门店"], "territories": ["黑河市"],
        }, token=TOKEN)
        self.assertEqual(status, 201)
        # 商品是文旅，许可也要文旅
        status, _ = self.request("POST", f"/api/contents/{content['id']}/licenses", {
            "scope": "tourism", "channels": ["景区门店"], "territories": ["黑河市"],
        }, token=TOKEN)
        self.assertEqual(status, 201)
        status, batch = self.request("POST", f"/api/products/{product['id']}/batches", {
            "quantity": 5, "producer": "厂", "channel": "景区门店", "location": "黑河市",
        }, token=TOKEN)
        self.assertEqual(status, 201)

        # 商户在外地批发市场销售：销售保留并结算，商品自动暂停
        status, sale = self.request("POST", f"/api/batches/{batch['id']}/sales", {
            "amount": "20.00", "reporter": "商户", "reporter_type": "merchant",
            "sale_ref": "T-BREACH", "channel": "外地批发市场", "location": "外地",
        }, token=TOKEN)
        self.assertEqual(status, 201)
        self.assertTrue(sale["out_of_scope"])
        self.assertTrue(sale["auto_suspended_product"])

        status, product_view = self.request("GET", f"/api/products/{product['id']}", token=TOKEN)
        self.assertEqual(product_view["status"], "suspended")

    def test_unknown_routes(self):
        for path in ("/nothing", "/v/else", "/api/else"):
            status, _ = self.request("GET", path, token=TOKEN)
            self.assertEqual(status, 404, path)


if __name__ == "__main__":
    unittest.main()
