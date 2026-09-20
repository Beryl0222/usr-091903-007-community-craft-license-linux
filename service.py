"""传统工艺社区许可后端 HTTP 入口。

接口分两类：

* ``/v/...`` 公开接口：只验证批次标识、返回必要说明，不暴露族内知识；
* ``/api/...`` 合作社内部接口：登记内容、发放许可、处理异议与复核、
  签发批次、上报销售、查看分账，需要内部令牌（``--internal-token``
  或环境变量 ``COOP_API_TOKEN``）。

仅依赖 Python 标准库。
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from licensing import (
    ConflictError,
    DomainError,
    NotFoundError,
    Store,
)

SERVICE_ID = "community-craft-license"
SERVICE_NAME = "传统工艺社区许可"

INTERNAL_TOKEN_ENV = "COOP_API_TOKEN"


def _require_field(body: dict, key: str):
    if key not in body:
        raise DomainError(f"缺少必填字段: {key}", {"field": key})
    return body.pop(key)


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """路由公开验证接口与合作社内部接口。"""

    store: Store | None = None  # 由 make_server 注入（类属性，便于测试替换）
    internal_token: str | None = None

    # -- HTTP 基础 ----------------------------------------------------------

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端提前断开时无需再写响应
            self.close_connection = True

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DomainError("请求体不是合法 JSON")
        if not isinstance(payload, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return payload

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, NotFoundError):
            status, code = 404, "not_found"
        elif isinstance(exc, ConflictError):
            status, code = 409, "conflict"
        elif isinstance(exc, DomainError):
            status, code = 400, "invalid_request"
        else:
            status, code = 500, "internal_error"
        payload: dict = {"error": code, "message": str(exc)}
        if isinstance(exc, DomainError) and exc.detail is not None:
            payload["detail"] = exc.detail
        self._send_json(status, payload)

    def _authorized(self) -> bool:
        if not self.internal_token:
            return False
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else self.headers.get("X-Coop-Token", "")
        return token == self.internal_token

    def log_message(self, *_args):
        return

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            parts = [unquote(p) for p in urlsplit(self.path).path.strip("/").split("/") if p]
            if method == "GET" and parts == ["health"]:
                self._send_json(200, health_payload())
                return
            if parts and parts[0] == "v":
                self._route_public(method, parts[1:])
                return
            if parts and parts[0] == "api":
                if not self._authorized():
                    self._send_json(401, {"error": "unauthorized", "message": "内部接口需要有效令牌"})
                    return
                self._route_internal(method, parts[1:])
                return
            self._send_json(404, {"error": "not_found", "message": "路径不存在"})
        except Exception as exc:  # noqa: BLE001 - 统一转换为错误响应
            self._error(exc)

    def _route_public(self, method: str, parts: list[str]) -> None:
        store = self.store
        if method == "GET" and len(parts) == 2 and parts[0] == "batches":
            self._send_json(200, store.verify_batch(parts[1]))
            return
        if method == "GET" and len(parts) == 2 and parts[0] == "contents":
            self._send_json(200, store.public_content_brief(parts[1]))
            return
        self._send_json(404, {"error": "not_found", "message": "公开路径不存在"})

    def _route_internal(self, method: str, parts: list[str]) -> None:
        store = self.store

        # 内容
        if method == "POST" and parts == ["contents"]:
            body = self._read_json()
            self._send_json(201, store.register_content(
                _require_field(body, "kind"), _require_field(body, "title"), **body))
        if method == "GET" and parts == ["contents"]:
            self._send_json(200, {"contents": store.list_contents()})
        if method == "GET" and len(parts) == 2 and parts[0] == "contents":
            self._send_json(200, store.content(parts[1]))

        # 许可
        if method == "POST" and len(parts) == 3 and parts[0] == "contents" and parts[2] == "licenses":
            self._send_json(201, store.issue_license(parts[1], **self._read_json()))
        if method == "GET" and parts == ["licenses"]:
            self._send_json(200, {"licenses": store.list_licenses()})
        if method == "GET" and len(parts) == 2 and parts[0] == "licenses":
            self._send_json(200, store.license(parts[1]))
        if method == "POST" and len(parts) == 3 and parts[0] == "licenses" and parts[2] == "withdrawal":
            body = self._read_json()
            self._send_json(201, store.withdraw_license(
                parts[1], _require_field(body, "withdrawn_by"), _require_field(body, "reason"), **body))

        # 异议与复核
        if method == "POST" and len(parts) == 3 and parts[0] == "contents" and parts[2] == "objections":
            body = self._read_json()
            self._send_json(201, store.raise_objection(
                parts[1], _require_field(body, "raised_by"), _require_field(body, "reason"), **body))
        if method == "GET" and parts == ["objections"]:
            self._send_json(200, {"objections": store.list_objections()})
        if method == "GET" and len(parts) == 2 and parts[0] == "objections":
            self._send_json(200, store.objection(parts[1]))
        if method == "POST" and len(parts) == 3 and parts[0] == "objections" and parts[2] == "review":
            body = self._read_json()
            self._send_json(200, store.review_objection(
                parts[1], _require_field(body, "decision"), _require_field(body, "reviewer"), **body))

        # 商品
        if method == "POST" and parts == ["products"]:
            body = self._read_json()
            self._send_json(201, store.register_product(
                _require_field(body, "name"), _require_field(body, "items"), **body))
        if method == "GET" and parts == ["products"]:
            self._send_json(200, {"products": store.list_products()})
        if method == "GET" and len(parts) == 2 and parts[0] == "products":
            self._send_json(200, store.product(parts[1]))
        if method == "POST" and len(parts) == 3 and parts[0] == "products" and parts[2] == "breach":
            body = self._read_json()
            self._send_json(200, store.report_channel_breach(
                parts[1], _require_field(body, "reported_by"), _require_field(body, "detail"), **body))
        if method == "POST" and len(parts) == 3 and parts[0] == "products" and parts[2] == "review":
            body = self._read_json()
            self._send_json(200, store.review_product(
                parts[1], _require_field(body, "decision"), _require_field(body, "reviewer"), **body))

        # 批次
        if method == "POST" and len(parts) == 3 and parts[0] == "products" and parts[2] == "batches":
            body = self._read_json()
            self._send_json(201, store.issue_batch(parts[1], **body))
        if method == "GET" and parts == ["batches"]:
            self._send_json(200, {"batches": store.list_batches()})
        if method == "GET" and len(parts) == 2 and parts[0] == "batches":
            self._send_json(200, store.batch(parts[1]))

        # 销售与台账
        if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "sales":
            body = self._read_json()
            self._send_json(201, store.report_sale(parts[1], **body))
        if method == "GET" and parts == ["sales"]:
            self._send_json(200, {"sales": store.list_sales()})
        if method == "GET" and len(parts) == 2 and parts[0] == "sales":
            self._send_json(200, store.sale(parts[1]))
        if method == "GET" and parts == ["ledger"]:
            self._send_json(200, store.ledger())
            return

        self._send_json(404, {"error": "not_found", "message": "内部路径不存在"})


def make_server(host: str, port: int, store: Store, internal_token: str | None):
    """构建绑定了仓库与令牌的 HTTP 服务（供 main 与测试共用）。"""
    handler = type("BoundHandler", (Handler,), {"store": store, "internal_token": internal_token})
    return ThreadingHTTPServer((host, port), handler)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--store", default=None, help="JSON 持久化文件路径；不提供则仅内存运行")
    parser.add_argument(
        "--internal-token",
        default=None,
        help=f"内部接口令牌；缺省读取环境变量 {INTERNAL_TOKEN_ENV}，均无则内部接口拒绝访问",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        Store()  # 领域模块可初始化
        print("基础检查通过")
        return
    token = args.internal_token or os.environ.get(INTERNAL_TOKEN_ENV)
    store = Store(args.store)
    if not token:
        print(f"警告：未配置内部令牌（--internal-token 或 {INTERNAL_TOKEN_ENV}），/api 内部接口将全部拒绝访问")
    server = make_server(args.host, args.port, store, token)
    print(f"{SERVICE_NAME} 监听 {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
