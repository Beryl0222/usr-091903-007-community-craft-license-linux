"""SQLite 存储层。

所有写操作集中在 :class:`Store` 的短事务里执行，保证同一销售重复上报、
批次发放等并发场景下的一致性。时间戳统一为 UTC ISO8601 字符串。
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA_VERSION = 1


def now_iso():
    """当前 UTC 时间，秒级精度。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 主体：传承人小组成员、合作社、景区、商户
CREATE TABLE IF NOT EXISTS parties (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('holder','coop','scenic','merchant')),
    created_at TEXT NOT NULL
);

-- 调用方令牌（一个主体可持有多个）
CREATE TABLE IF NOT EXISTS api_tokens (
    token      TEXT PRIMARY KEY,
    party_id   TEXT NOT NULL REFERENCES parties(id),
    label      TEXT NOT NULL DEFAULT '',
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- 文化内容：纹样 / 技法 / 口述故事 / 药用知识
CREATE TABLE IF NOT EXISTS contents (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('pattern','technique','story','medicine')),
    title       TEXT NOT NULL,
    family      TEXT,                       -- 家族/支系含义（仅内部可见）
    custodian   TEXT NOT NULL REFERENCES parties(id),  -- 登记的传承人
    description TEXT NOT NULL DEFAULT '',   -- 可公开说明
    holders     TEXT NOT NULL DEFAULT '[]', -- 必须署名的人（JSON 数组）
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 许可版本：每次复核决定都产生新版本，旧版本只读保留
CREATE TABLE IF NOT EXISTS licenses (
    id              TEXT PRIMARY KEY,
    content_id      TEXT NOT NULL REFERENCES contents(id),
    version         INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('active','suspended','revoked','expired')),
    sensitivity     TEXT NOT NULL CHECK (sensitivity IN ('public','community','internal','restricted')),
    purposes        TEXT NOT NULL DEFAULT '[]',   -- 允许用途 JSON 数组，如 ["cultural_tourism","medicine_tourism"]
    territories     TEXT NOT NULL DEFAULT '[]',   -- 地域限制 JSON 数组，空表示不限
    channels        TEXT NOT NULL DEFAULT '[]',   -- 允许渠道 JSON 数组，空表示不限
    valid_from      TEXT NOT NULL,
    valid_until     TEXT,                          -- NULL 表示长期有效
    required_credit TEXT NOT NULL DEFAULT '',     -- 必须署名的人（冗余自内容，可版本覆盖）
    basis           TEXT NOT NULL DEFAULT '',     -- 发放/复核依据说明
    decided_by      TEXT NOT NULL REFERENCES parties(id),
    created_at      TEXT NOT NULL,
    UNIQUE (content_id, version)
);

CREATE INDEX IF NOT EXISTS idx_licenses_content ON licenses(content_id);

-- 生命周期事件：撤回 / 异议 / 复核
CREATE TABLE IF NOT EXISTS license_events (
    id          TEXT PRIMARY KEY,
    content_id  TEXT NOT NULL REFERENCES contents(id),
    event_type  TEXT NOT NULL CHECK (event_type IN ('grant','revoke','dispute','review','reaffirm')),
    from_version INTEGER,
    to_version   INTEGER,
    actor       TEXT NOT NULL REFERENCES parties(id),
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_content ON license_events(content_id);

-- 商品组合：一个商品使用多项文化内容
CREATE TABLE IF NOT EXISTS products (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    purpose        TEXT NOT NULL DEFAULT '',   -- 文旅 cultural_tourism / 药旅 medicine_tourism
    territory      TEXT NOT NULL DEFAULT '',   -- 实际销售地域
    channel        TEXT NOT NULL DEFAULT '',   -- 实际销售渠道
    content_bps    INTEGER NOT NULL DEFAULT 10000, -- 销售额中归文化内容的比例（基点）
    community_bps  INTEGER NOT NULL DEFAULT 1000,  -- 内容收益中社区基金提成（基点）
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_items (
    product_id   TEXT NOT NULL REFERENCES products(id),
    content_id   TEXT NOT NULL REFERENCES contents(id),
    share_bps    INTEGER NOT NULL DEFAULT 0,   -- 该项在内容收益中的分成基点（万分之一）
    PRIMARY KEY (product_id, content_id)
);

-- 批次标识：商品组合所有许可有效才发放；发放时冻结许可版本快照
CREATE TABLE IF NOT EXISTS batches (
    id              TEXT PRIMARY KEY,
    product_id      TEXT NOT NULL REFERENCES products(id),
    status          TEXT NOT NULL CHECK (status IN ('issued','paused','closed')),
    quantity        INTEGER NOT NULL DEFAULT 0,
    content_bps     INTEGER NOT NULL DEFAULT 10000, -- 发放时冻结的内容收益占比
    community_bps   INTEGER NOT NULL DEFAULT 1000,  -- 发放时冻结的社区基金提成
    issued_at       TEXT NOT NULL,
    paused_at       TEXT,
    close_reason    TEXT NOT NULL DEFAULT '',
    license_snapshot TEXT NOT NULL            -- [{content_id, license_id, version, share_bps, title, credit}, ...]
);

CREATE INDEX IF NOT EXISTS idx_batches_product ON batches(product_id);

-- 批次包含的内容（快照的可查询投影，发放时写入，之后不变）
CREATE TABLE IF NOT EXISTS batch_items (
    batch_id   TEXT NOT NULL REFERENCES batches(id),
    content_id TEXT NOT NULL REFERENCES contents(id),
    license_id TEXT NOT NULL REFERENCES licenses(id),
    version    INTEGER NOT NULL,
    share_bps  INTEGER NOT NULL,
    PRIMARY KEY (batch_id, content_id)
);

CREATE INDEX IF NOT EXISTS idx_batch_items_content ON batch_items(content_id);

-- 批次状态事件：发放 / 暂停 / 恢复 / 关闭，全部留痕
CREATE TABLE IF NOT EXISTS batch_events (
    id          TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    event_type  TEXT NOT NULL CHECK (event_type IN ('issued','paused','resumed','closed')),
    actor       TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    snapshot    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batch_events ON batch_events(batch_id);

-- 销售流水：同一笔现实销售（同 external_key）无论上报几次，只结算一次
CREATE TABLE IF NOT EXISTS sales (
    id              TEXT PRIMARY KEY,
    external_key    TEXT NOT NULL UNIQUE,     -- 幂等键：景区/商户对同一笔销售使用相同键
    batch_id        TEXT NOT NULL REFERENCES batches(id),
    product_id      TEXT NOT NULL REFERENCES products(id),
    reporter        TEXT NOT NULL REFERENCES parties(id),
    amount_fen      INTEGER NOT NULL,         -- 销售金额（分）
    community_bps   INTEGER NOT NULL DEFAULT 0,  -- 社区基金提成基点
    sold_at         TEXT NOT NULL,
    reported_at     TEXT NOT NULL,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    settlement_id   TEXT REFERENCES settlements(id),
    reject_reason   TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','settled','rejected'))
);

CREATE INDEX IF NOT EXISTS idx_sales_batch ON sales(batch_id);

-- 重复上报登记表（保留每一次上报痕迹，便于对景区/商户解释）
CREATE TABLE IF NOT EXISTS sales_reports (
    id            TEXT PRIMARY KEY,
    external_key  TEXT NOT NULL,
    reporter      TEXT NOT NULL REFERENCES parties(id),
    batch_id      TEXT NOT NULL,
    amount_fen    INTEGER NOT NULL,
    accepted      INTEGER NOT NULL,           -- 1=首次已受理 0=重复
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_key ON sales_reports(external_key);

-- 结算单：按批次发放时的许可版本快照把收益分到贡献者与社区基金
CREATE TABLE IF NOT EXISTS settlements (
    id          TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    total_fen   INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    rationale   TEXT NOT NULL                 -- 人类可读的分配规则解释
);

CREATE TABLE IF NOT EXISTS settlement_lines (
    id            TEXT PRIMARY KEY,
    settlement_id TEXT NOT NULL REFERENCES settlements(id),
    payee         TEXT NOT NULL,              -- party_id 或 'COMMUNITY_FUND'
    content_id    TEXT,                       -- 社区基金行为 NULL
    license_id    TEXT,
    amount_fen    INTEGER NOT NULL,
    rule          TEXT NOT NULL               -- 该行采用的规则说明
);

CREATE INDEX IF NOT EXISTS idx_lines_settlement ON settlement_lines(settlement_id);
"""


class Store:
    """薄封装：连接管理、JSON 辅助、令牌查询。"""

    def __init__(self, path=":memory:"):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.init_schema()

    def init_schema(self):
        with self._lock, self.conn:
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # -- 基础辅助 -----------------------------------------------------------

    def transaction(self):
        return self.conn  # 配合 with self.conn 使用，锁由调用方持有

    @property
    def lock(self):
        return self._lock

    def get(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        return cur.fetchone()

    def all(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        return cur.fetchall()

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()

    # -- JSON 字段辅助 ------------------------------------------------------

    @staticmethod
    def loads(value, default):
        if not value:
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default

    @staticmethod
    def dumps(value):
        return json.dumps(value, ensure_ascii=False)

    # -- 令牌 ---------------------------------------------------------------

    def token_party(self, token):
        return self.get(
            """
            SELECT p.* FROM api_tokens t JOIN parties p ON p.id = t.party_id
            WHERE t.token = ? AND t.revoked = 0
            """,
            (token,),
        )

    def close(self):
        self.conn.close()
