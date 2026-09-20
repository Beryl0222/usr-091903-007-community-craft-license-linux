"""传统工艺社区许可后端入口。

用法：
  python3 service.py --check                 # 配置自检
  python3 service.py --init-db --db ccl.db   # 初始化数据库
  python3 service.py --db ccl.db --port 8000 # 启动服务
"""

import argparse
import json
import os
from http.server import ThreadingHTTPServer

SERVICE_ID = "community-craft-license"
SERVICE_NAME = "传统工艺社区许可"

DEFAULT_DB = os.environ.get("CCL_DB", "ccl.db")


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_app(db_path=DEFAULT_DB):
    """组装存储、领域服务与 HTTP 应用。"""
    from db import Store
    from web import Application

    store = Store(db_path)
    return Application(store)


def bootstrap_coop(db_path=DEFAULT_DB):
    """空库自举：创建第一个合作社并发放令牌，避免系统无人可管理。"""
    import core
    from db import Store

    store = Store(db_path)
    if store.get("SELECT COUNT(*) AS n FROM parties")["n"] > 0:
        return None
    coop = core.register_party(
        store, None, party_id="P-COOP",
        name="新生鄂伦春族乡文旅合作社", role="coop")
    token = core.issue_token(store, {"id": coop["id"], "role": "coop"},
                             party_id=coop["id"], label="bootstrap")
    return {"party": coop, "token": token["token"]}


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite 数据库路径")
    parser.add_argument("--check", action="store_true", help="配置自检后退出")
    parser.add_argument("--init-db", action="store_true",
                        help="初始化/迁移数据库表后退出")
    parser.add_argument("--bootstrap", action="store_true",
                        help="空库时创建初始合作社并打印令牌后退出")
    args = parser.parse_args()

    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 同时做一次内存库全链路装配检查
        app = build_app(":memory:")
        assert app.store.get("SELECT 1 AS ok")["ok"] == 1
        app.store.close()
        print("基础检查通过")
        return

    if args.init_db:
        app = build_app(args.db)
        app.store.close()
        print(f"数据库已就绪：{args.db}")
        return

    if args.bootstrap:
        result = bootstrap_coop(args.db)
        if result is None:
            print("库中已有主体，无需自举")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    app = build_app(args.db)
    server = ThreadingHTTPServer(("0.0.0.0", args.port),
                                 __import__("web").make_handler(app))
    print(f"{SERVICE_NAME} 监听 :{args.port}（数据库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.store.close()


if __name__ == "__main__":
    main()
