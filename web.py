"""HTTP 层：路由、Bearer 鉴权与 JSON 序列化。

公开端点（/health、/v1/verify/<批次号>）不需要令牌；其余端点按传承人小组、
合作社、景区/商户三类身份授权。
"""

import json
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

import core


class Application:
    def __init__(self, store):
        self.store = store


def _json_dumps(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def make_handler(app):
    store = app.store

    class Handler(BaseHTTPRequestHandler):
        server_version = "CommunityCraftLicense/1.0"

        # -- 基础设施 -------------------------------------------------------

        def log_message(self, *_args):
            return

        def _send(self, payload, status=200):
            body = _json_dumps(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise core.DomainError("bad_json", "请求体须为 UTF-8 JSON", 400)
            if not isinstance(data, dict):
                raise core.DomainError("bad_json", "请求体须为 JSON 对象", 400)
            return data

        def _actor(self):
            """解析 Bearer 令牌并返回主体。无令牌/坏令牌按调用意图区别处理。"""
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                raise core.DomainError("unauthorized", "缺少 Bearer 令牌", 401)
            party = store.token_party(auth[len("Bearer "):].strip())
            if party is None:
                raise core.DomainError("unauthorized", "令牌无效或已吊销", 401)
            return dict(party)

        def _optional_actor(self):
            return self._actor() if self.headers.get("Authorization") else None

        # -- 分派 -----------------------------------------------------------

        def _dispatch(self, method):
            parsed = urlsplit(self.path)
            path, query = parsed.path.rstrip("/") or "/", parse_qs(parsed.query)
            try:
                handled = self._route(method, path, query)
                if not handled:
                    self._send({"error": "not_found",
                                "message": f"未找到路由：{method} {path}"}, 404)
            except core.DomainError as exc:
                self._send(exc.to_dict(), exc.status)
            except Exception:  # noqa: BLE001 - 兜底，不向调用方泄露堆栈
                traceback.print_exc()
                self._send({"error": "internal_error",
                            "message": "服务内部错误，请联系合作社管理员"}, 500)

        def _route(self, method, path, query):
            segs = [s for s in path.split("/") if s]

            if path == "/health" and method == "GET":
                from service import health_payload
                self._send(health_payload())
                return True

            # 主体与令牌
            if segs == ["v1", "parties"] and method == "POST":
                self._handle_register_party()
                return True
            if (len(segs) == 4 and segs[:2] == ["v1", "parties"]
                    and segs[3] == "tokens" and method == "POST"):
                actor = self._actor()
                body = self._read_json()
                self._send(core.issue_token(
                    store, actor, party_id=segs[2],
                    label=body.get("label", "")), 201)
                return True

            # 文化内容
            if segs == ["v1", "contents"] and method == "POST":
                self._handle_create_content()
                return True
            if len(segs) >= 3 and segs[:2] == ["v1", "contents"]:
                return self._route_content(method, segs[2], segs[3:])

            # 商品组合与批次
            if segs == ["v1", "products"] and method == "POST":
                self._handle_create_product()
                return True
            if len(segs) >= 3 and segs[:2] == ["v1", "products"]:
                return self._route_product(method, segs[2], segs[3:])
            if len(segs) >= 3 and segs[:2] == ["v1", "batches"]:
                return self._route_batch(method, segs[2], segs[3:])

            # 批次标识公开核验（无需令牌）
            if (len(segs) == 3 and segs[:2] == ["v1", "verify"]
                    and method == "GET"):
                self._send(core.public_verify(store, segs[2]))
                return True

            # 销售上报与台账
            if segs == ["v1", "sales"] and method == "POST":
                actor = self._actor()
                body = self._read_json()
                self._send(core.report_sale(
                    store, actor, external_key=body["external_key"],
                    batch_id=body["batch_id"], amount_fen=body["amount_fen"],
                    sold_at=body.get("sold_at"),
                    channel=body.get("channel", ""),
                    territory=body.get("territory", "")))
                return True
            if segs == ["v1", "sales"] and method == "GET":
                actor = self._actor()
                self._send({"sales": core.list_sales(
                    store, actor, batch_id=self._qs(query, "batch_id"))})
                return True

            # 收益结算台账
            if segs == ["v1", "settlements"] and method == "GET":
                actor = self._actor()
                self._send({"settlements": core.list_settlements(
                    store, actor,
                    batch_id=self._qs(query, "batch_id"),
                    payee=self._qs(query, "payee"),
                    limit=int(self._qs(query, "limit") or 50))})
                return True
            if (len(segs) == 3 and segs[:2] == ["v1", "settlements"]
                    and method == "GET"):
                actor = self._actor()
                self._send(core.get_settlement(store, actor, segs[2]))
                return True

            return False

        @staticmethod
        def _qs(query, key):
            values = query.get(key)
            return values[0] if values else None

        # -- 主体/内容/商品处理器 -------------------------------------------

        def _handle_register_party(self):
            # 库中尚无主体时允许自举创建第一个合作社，之后必须持合作社令牌
            actor = self._optional_actor()
            body = self._read_json()
            self._send(core.register_party(
                store, actor, party_id=body.get("party_id"),
                name=body["name"], role=body["role"]), 201)

        def _handle_create_content(self):
            actor = self._actor()
            body = self._read_json()
            self._send(core.register_content(
                store, actor, content_id=body.get("content_id"),
                kind=body["kind"], title=body["title"],
                family=body.get("family"),
                description=body.get("description", ""),
                holders=body.get("holders"),
                custodian=body.get("custodian")), 201)

        def _handle_create_product(self):
            actor = self._actor()
            body = self._read_json()
            self._send(core.create_product(
                store, actor, product_id=body.get("product_id"),
                name=body["name"], purpose=body["purpose"],
                territory=body.get("territory", ""),
                channel=body.get("channel", ""),
                content_bps=int(body.get("content_bps", 10000)),
                community_bps=int(body.get("community_bps", 1000)),
                items=body.get("items")), 201)

        def _route_content(self, method, cid, tail):
            if not tail:
                if method == "GET":
                    actor = self._actor()
                    core.require_role(actor, "holder", "coop")
                    self._send(core.get_content(store, cid, actor))
                    return True
                if method == "PATCH":
                    actor = self._actor()
                    self._send(core.update_content(
                        store, actor, cid, **self._read_json()))
                    return True
                return False
            sub = tail[0]
            if sub == "licenses":
                actor = self._actor()
                core.require_role(actor, "holder", "coop")
                if method == "GET":
                    self._send({"versions": core.list_license_versions(store, cid)})
                    return True
                if method == "POST":
                    self._send(core.grant_license(
                        store, actor, cid, **self._read_json()), 201)
                    return True
            if sub == "events" and method == "GET":
                actor = self._actor()
                core.require_role(actor, "holder", "coop")
                self._send({"events": core.list_events(store, cid)})
                return True
            if sub == "revoke" and method == "POST":
                actor = self._actor()
                body = self._read_json()
                self._send(core.revoke_license(
                    store, actor, cid, reason=body.get("reason", "")))
                return True
            if sub == "disputes" and method == "POST":
                actor = self._actor()
                body = self._read_json()
                self._send(core.raise_dispute(
                    store, actor, cid, reason=body.get("reason", "")), 201)
                return True
            if sub == "reviews" and method == "POST":
                actor = self._actor()
                body = self._read_json()
                decision = body.pop("decision")
                reason = body.pop("reason", "")
                self._send(core.resolve_review(
                    store, actor, cid, decision=decision, reason=reason, **body))
                return True
            return False

        def _route_product(self, method, pid, tail):
            if not tail:
                if method == "GET":
                    self._actor()  # 商品组合属内部信息，匿名不可查
                    self._send(core.get_product(store, pid))
                    return True
                if method == "PATCH":
                    actor = self._actor()
                    self._send(core.update_product(
                        store, actor, pid, **self._read_json()))
                    return True
                return False
            if tail == ["batches"] and method == "POST":
                actor = self._actor()
                body = self._read_json()
                self._send(core.issue_batch(
                    store, actor, pid,
                    quantity=int(body.get("quantity", 0))), 201)
                return True
            return False

        def _route_batch(self, method, bid, tail):
            if not tail and method == "GET":
                actor = self._actor()
                core.require_role(actor, "holder", "coop", "scenic", "merchant")
                self._send(core.get_batch(store, bid))
                return True
            if tail == ["pause"] and method == "POST":
                actor = self._actor()
                body = self._read_json()
                self._send(core.pause_batch(
                    store, actor, bid, reason=body.get("reason", "")))
                return True
            if tail == ["close"] and method == "POST":
                actor = self._actor()
                body = self._read_json()
                self._send(core.close_batch(
                    store, actor, bid, reason=body.get("reason", "")))
                return True
            return False

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

    return Handler
