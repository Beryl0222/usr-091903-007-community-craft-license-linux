"""核心领域逻辑：社区许可、批次标识、销售去重与收益分配。

所有写方法都在 ``store.lock`` 保护的单事务内完成；HTTP 层只负责鉴权与序列化。
"""

import uuid
from datetime import datetime, timezone

from db import now_iso

COMMUNITY_FUND = "COMMUNITY_FUND"

CONTENT_KINDS = ("pattern", "technique", "story", "medicine")
SENSITIVITIES = ("public", "community", "internal", "restricted")
ROLES = ("holder", "coop", "scenic", "merchant")


class DomainError(Exception):
    """业务规则违反，``code`` 供调用方稳定判别，``status`` 映射 HTTP 状态。"""

    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def to_dict(self):
        return {"error": self.code, "message": self.message, "details": self.details}


def _new_id(prefix=""):
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _batch_id():
    return f"B{datetime.now(timezone.utc):%Y%m%d}{uuid.uuid4().hex[:6].upper()}"


def _row(row):
    return dict(row) if row is not None else None


def require_role(actor, *roles):
    if actor is None or actor["role"] not in roles:
        raise DomainError(
            "forbidden",
            f"该操作需要 {'/'.join(roles)} 身份，当前身份不可用",
            status=403,
        )


# ---------------------------------------------------------------------------
# 主体与令牌
# ---------------------------------------------------------------------------

def register_party(store, actor, *, party_id=None, name, role):
    if role not in ROLES:
        raise DomainError("invalid_role", f"未知身份类型：{role}")
    # 首个主体允许自举注册；之后仅合作社可登记主体
    existing = store.get("SELECT COUNT(*) AS n FROM parties")
    if existing["n"] > 0:
        require_role(actor, "coop")
    party_id = party_id or _new_id("P-")
    with store.lock, store.conn:
        if store.get("SELECT 1 FROM parties WHERE id = ?", (party_id,)):
            raise DomainError("duplicate_party", f"主体已存在：{party_id}", 409)
        store.execute(
            "INSERT INTO parties(id, name, role, created_at) VALUES(?,?,?,?)",
            (party_id, name, role, now_iso()),
        )
    return _row(store.get("SELECT * FROM parties WHERE id = ?", (party_id,)))


def issue_token(store, actor, *, party_id, label=""):
    require_role(actor, "coop")
    party = store.get("SELECT * FROM parties WHERE id = ?", (party_id,))
    if party is None:
        raise DomainError("unknown_party", f"主体不存在：{party_id}", 404)
    token = f"ccl_{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"
    with store.lock, store.conn:
        store.execute(
            "INSERT INTO api_tokens(token, party_id, label, created_at) VALUES(?,?,?,?)",
            (token, party_id, label, now_iso()),
        )
    return {"token": token, "party_id": party_id, "label": label}


# ---------------------------------------------------------------------------
# 文化内容
# ---------------------------------------------------------------------------

def register_content(store, actor, *, content_id=None, kind, title, family=None,
                     description="", holders=None, custodian=None):
    require_role(actor, "holder")
    if kind not in CONTENT_KINDS:
        raise DomainError("invalid_kind", f"内容类型须为 {CONTENT_KINDS} 之一")
    custodian = custodian or actor["id"]
    custodian_row = store.get("SELECT * FROM parties WHERE id = ?", (custodian,))
    if custodian_row is None or custodian_row["role"] != "holder":
        raise DomainError("invalid_custodian", "保管人须是传承人小组成员", 422)
    holders = holders or []
    if not isinstance(holders, list):
        raise DomainError("invalid_holders", "署名人须为名单数组")
    content_id = content_id or _new_id("C-")
    ts = now_iso()
    with store.lock, store.conn:
        if store.get("SELECT 1 FROM contents WHERE id = ?", (content_id,)):
            raise DomainError("duplicate_content", f"内容已存在：{content_id}", 409)
        store.execute(
            """INSERT INTO contents(id, kind, title, family, custodian, description,
                                    holders, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (content_id, kind, title, family, custodian, description,
             store.dumps(holders), ts, ts),
        )
    return get_content(store, content_id, actor)


def update_content(store, actor, content_id, **fields):
    content = store.get("SELECT * FROM contents WHERE id = ?", (content_id,))
    if content is None:
        raise DomainError("unknown_content", f"内容不存在：{content_id}", 404)
    require_role(actor, "holder")
    if actor["id"] != content["custodian"] and actor["role"] != "coop":
        raise DomainError("forbidden", "仅登记保管人可修改该内容", 403)
    allowed = {"title", "family", "description", "holders"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "holders":
            if not isinstance(value, list):
                raise DomainError("invalid_holders", "署名人须为名单数组")
            value = store.dumps(value)
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        raise DomainError("empty_update", "没有可更新字段")
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(content_id)
    with store.lock, store.conn:
        store.execute(f"UPDATE contents SET {', '.join(sets)} WHERE id = ?", params)
    return get_content(store, content_id, actor)


def get_content(store, content_id, actor=None):
    row = store.get("SELECT * FROM contents WHERE id = ?", (content_id,))
    if row is None:
        raise DomainError("unknown_content", f"内容不存在：{content_id}", 404)
    data = _row(row)
    data["holders"] = store.loads(data["holders"], [])
    # family 只承载家族含义，不进入公开视图
    if actor is None or actor["role"] not in ("holder", "coop"):
        data.pop("family", None)
    return data


# ---------------------------------------------------------------------------
# 许可版本
# ---------------------------------------------------------------------------

def _license_to_dict(store, row):
    data = _row(row)
    for field, default in (("purposes", []), ("territories", []), ("channels", [])):
        data[field] = store.loads(data[field], default)
    return data


def latest_license(store, content_id):
    row = store.get(
        "SELECT * FROM licenses WHERE content_id = ? ORDER BY version DESC LIMIT 1",
        (content_id,),
    )
    return _license_to_dict(store, row) if row else None


def list_license_versions(store, content_id):
    rows = store.all(
        "SELECT * FROM licenses WHERE content_id = ? ORDER BY version",
        (content_id,),
    )
    return [_license_to_dict(store, r) for r in rows]


def list_events(store, content_id):
    rows = store.all(
        "SELECT * FROM license_events WHERE content_id = ? ORDER BY created_at, id",
        (content_id,),
    )
    return [_row(r) for r in rows]


def _validate_terms(sensitivity, purposes, territories, channels, valid_until):
    if sensitivity not in SENSITIVITIES:
        raise DomainError("invalid_sensitivity", f"敏感级别须为 {SENSITIVITIES} 之一")
    if not isinstance(purposes, list) or not purposes:
        raise DomainError("invalid_purposes", "允许用途至少填写一项")
    for name, value in (("territories", territories), ("channels", channels)):
        if value is not None and not isinstance(value, list):
            raise DomainError(f"invalid_{name}", f"{name} 须为名单数组")
    if valid_until is not None:
        try:
            datetime.fromisoformat(valid_until)
        except ValueError:
            raise DomainError("invalid_date", "valid_until 须为 ISO8601 时间")


def _create_version(store, actor, content_id, status, terms, event_type, reason,
                    prev, basis=""):
    ts = now_iso()
    version = (prev["version"] + 1) if prev else 1
    required_credit = terms.get("required_credit")
    if not required_credit:
        required_credit = prev["required_credit"] if prev else ""
    row = store.get("SELECT holders, custodian FROM contents WHERE id = ?", (content_id,))
    if row is None:
        raise DomainError("unknown_content", f"内容不存在：{content_id}", 404)
    if not required_credit:
        required_credit = "、".join(store.loads(row["holders"], [])) or row["custodian"]
    with store.lock, store.conn:
        license_id = _new_id("L-")
        store.execute(
            """INSERT INTO licenses(id, content_id, version, status, sensitivity,
                                    purposes, territories, channels,
                                    valid_from, valid_until, required_credit,
                                    basis, decided_by, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (license_id, content_id, version, status,
             terms["sensitivity"], store.dumps(terms["purposes"]),
             store.dumps(terms.get("territories") or []),
             store.dumps(terms.get("channels") or []),
             terms.get("valid_from") or ts, terms.get("valid_until"),
             required_credit, terms.get("basis") or basis,
             actor["id"], ts),
        )
        event_id = _new_id("E-")
        store.execute(
            """INSERT INTO license_events(id, content_id, event_type, from_version,
                                          to_version, actor, reason, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (event_id, content_id, event_type,
             prev["version"] if prev else None, version,
             actor["id"], reason, ts),
        )
    return _license_to_dict(
        store,
        store.get("SELECT * FROM licenses WHERE id = ?", (license_id,)),
    )


def grant_license(store, actor, content_id, *, sensitivity, purposes,
                  territories=None, channels=None, valid_from=None,
                  valid_until=None, required_credit=None, basis=""):
    """传承人小组首次发放许可（v1）。"""
    require_role(actor, "holder")
    _validate_terms(sensitivity, purposes, territories, channels, valid_until)
    prev = latest_license(store, content_id)
    if prev is not None:
        raise DomainError(
            "license_exists",
            "该内容已有许可；复核决定请使用 review 接口形成新版本",
            409,
        )
    terms = {"sensitivity": sensitivity, "purposes": purposes,
             "territories": territories, "channels": channels,
             "valid_from": valid_from, "valid_until": valid_until,
             "required_credit": required_credit, "basis": basis}
    return _create_version(store, actor, content_id, "active", terms,
                           "grant", basis or "首次发放许可", None)


def _review_terms_from_request(prev, payload):
    """复核未显式修改的条款沿用上一版本。"""
    return {
        "sensitivity": payload.get("sensitivity", prev["sensitivity"]),
        "purposes": payload.get("purposes", prev["purposes"]),
        "territories": payload.get("territories", prev["territories"]),
        "channels": payload.get("channels", prev["channels"]),
        "valid_from": payload.get("valid_from"),
        "valid_until": payload.get("valid_until", prev["valid_until"]),
        "required_credit": payload.get("required_credit"),
        "basis": payload.get("basis", ""),
    }


def revoke_license(store, actor, content_id, *, reason):
    """长者撤回许可：新版本 revoked，不抹去任何已发批次与已发生销售；
    使用该内容的在发批次立即暂停新生产。"""
    require_role(actor, "holder")
    prev = latest_license(store, content_id)
    if prev is None:
        raise DomainError("no_license", "该内容尚无许可", 404)
    terms = _review_terms_from_request(prev, {})
    lic = _create_version(store, actor, content_id, "revoked", terms,
                          "revoke", reason, prev)
    _pause_batches_for_content(
        store, content_id,
        f"传承人撤回许可（{content_id} v{lic['version']}）：{reason}",
        actor["id"],
    )
    return lic


def raise_dispute(store, actor, content_id, *, reason):
    """家族成员提出异议：新版本 suspended 冻结现状，等待复核；
    不改动被异议版本的条款，相关在发批次暂停新生产。"""
    require_role(actor, "holder")
    prev = latest_license(store, content_id)
    if prev is None:
        raise DomainError("no_license", "该内容尚无许可，无从提出异议", 404)
    terms = _review_terms_from_request(prev, {})
    lic = _create_version(store, actor, content_id, "suspended", terms,
                          "dispute", reason, prev)
    _pause_batches_for_content(
        store, content_id,
        f"家族成员对内容 {content_id} 提出异议（v{lic['version']}）：{reason}",
        actor["id"],
    )
    return lic


def resolve_review(store, actor, content_id, *, decision, reason, **changes):
    """传承人小组复核后形成新版本；恢复有效时自动评估可否恢复批次。"""
    require_role(actor, "holder")
    if decision not in ("active", "suspended", "revoked"):
        raise DomainError("invalid_decision",
                          "decision 须为 active / suspended / revoked")
    prev = latest_license(store, content_id)
    if prev is None:
        raise DomainError("no_license", "该内容尚无许可", 404)
    terms = _review_terms_from_request(prev, changes)
    _validate_terms(terms["sensitivity"], terms["purposes"],
                    terms["territories"], terms["channels"], terms["valid_until"])
    lic = _create_version(store, actor, content_id, decision, terms,
                          "review", reason, prev)
    resumed = []
    if decision == "active":
        resumed = _try_resume_batches_for_content(store, content_id, actor["id"])
    return {"license": lic, "resumed_batches": resumed}


def evaluate_license(lic, *, at=None, purpose="", territory="", channel=""):
    """按时间、用途、地域、渠道评估某许可版本是否可用，返回 (是否可用, 原因)。"""
    reasons = []
    at = at or now_iso()
    if lic["status"] != "active":
        reasons.append(f"许可版本 v{lic['version']} 状态为 {lic['status']}")
    if lic["valid_from"] and at < lic["valid_from"]:
        reasons.append("许可尚未生效")
    if lic["valid_until"] and at > lic["valid_until"]:
        reasons.append(f"许可已于 {lic['valid_until']} 到期")
    if purpose and purpose not in lic["purposes"]:
        reasons.append(f"用途 {purpose} 不在允许范围 {lic['purposes']} 内")
    if lic["territories"] and territory and territory not in lic["territories"]:
        reasons.append(f"地域 {territory} 不在约定范围 {lic['territories']} 内")
    if lic["channels"] and channel and channel not in lic["channels"]:
        reasons.append(f"渠道 {channel} 不在约定范围 {lic['channels']} 内")
    return (not reasons, reasons)


def sweep_expired(store):
    """把到期许可标记为 expired 并暂停相关在发批次。"""
    ts = now_iso()
    affected = []
    for row in store.all("SELECT * FROM licenses WHERE status = 'active'"):
        lic = _license_to_dict(store, row)
        if lic["valid_until"] and ts > lic["valid_until"]:
            with store.lock, store.conn:
                store.execute(
                    "UPDATE licenses SET status = 'expired' WHERE id = ?",
                    (lic["id"],),
                )
            affected.append(lic["content_id"])
            _pause_batches_for_content(
                store, lic["content_id"],
                f"许可 v{lic['version']} 已于 {lic['valid_until']} 到期",
                "system",
            )
    return affected


# ---------------------------------------------------------------------------
# 商品组合
# ---------------------------------------------------------------------------

def create_product(store, actor, *, product_id=None, name, purpose,
                   territory="", channel="", content_bps=10000,
                   community_bps=1000, items=None):
    require_role(actor, "coop")
    if not purpose:
        raise DomainError("invalid_purpose", "商品须标明用途（文旅/药旅）")
    for label, value in (("content_bps", content_bps),
                         ("community_bps", community_bps)):
        if not 0 <= value <= 10000:
            raise DomainError(f"invalid_{label}", f"{label} 须在 0..10000 之间")
    items = _validate_items(items or [])
    product_id = product_id or _new_id("G-")
    ts = now_iso()
    with store.lock, store.conn:
        if store.get("SELECT 1 FROM products WHERE id = ?", (product_id,)):
            raise DomainError("duplicate_product", f"商品已存在：{product_id}", 409)
        store.execute(
            """INSERT INTO products(id, name, purpose, territory, channel,
                                    content_bps, community_bps, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (product_id, name, purpose, territory, channel,
             content_bps, community_bps, ts, ts),
        )
        for it in items:
            store.execute(
                "INSERT INTO product_items(product_id, content_id, share_bps) VALUES(?,?,?)",
                (product_id, it["content_id"], it["share_bps"]),
            )
    return get_product(store, product_id)


def _validate_items(items):
    if not items:
        raise DomainError("empty_items", "商品组合至少包含一项文化内容")
    total = 0
    seen = set()
    normalized = []
    for it in items:
        cid = it.get("content_id")
        share = int(it.get("share_bps", 0))
        if not cid or share <= 0:
            raise DomainError("invalid_item", "每项须有 content_id 与正整数 share_bps")
        if cid in seen:
            raise DomainError("duplicate_item", f"内容 {cid} 在组合中重复")
        seen.add(cid)
        total += share
        normalized.append({"content_id": cid, "share_bps": share})
    if total != 10000:
        raise DomainError(
            "invalid_shares",
            f"组合内分成基点之和须为 10000，当前为 {total}",
            details={"total": total},
        )
    return normalized


def get_product(store, product_id):
    row = store.get("SELECT * FROM products WHERE id = ?", (product_id,))
    if row is None:
        raise DomainError("unknown_product", f"商品不存在：{product_id}", 404)
    data = _row(row)
    data["items"] = [
        {"content_id": r["content_id"], "share_bps": r["share_bps"]}
        for r in store.all(
            "SELECT content_id, share_bps FROM product_items WHERE product_id = ? ORDER BY rowid",
            (product_id,),
        )
    ]
    return data


def update_product(store, actor, product_id, **fields):
    """商品渠道/地域等变更。新渠道若超出任一在发批次的许可约定，暂停该批次。"""
    require_role(actor, "coop")
    before = get_product(store, product_id)
    allowed = {"name", "purpose", "territory", "channel",
               "content_bps", "community_bps"}
    sets, params = [], []
    for key, value in fields.items():
        if key in allowed and value is not None:
            sets.append(f"{key} = ?")
            params.append(value)
    if "items" in fields:
        items = _validate_items(fields["items"])
    else:
        items = None
    if not sets and items is None:
        raise DomainError("empty_update", "没有可更新字段")
    with store.lock, store.conn:
        if sets:
            sets.append("updated_at = ?")
            params.append(now_iso())
            params.append(product_id)
            store.execute(
                f"UPDATE products SET {', '.join(sets)} WHERE id = ?", params
            )
        if items is not None:
            store.execute("DELETE FROM product_items WHERE product_id = ?", (product_id,))
            for it in items:
                store.execute(
                    "INSERT INTO product_items(product_id, content_id, share_bps) VALUES(?,?,?)",
                    (product_id, it["content_id"], it["share_bps"]),
                )
    after = get_product(store, product_id)
    _pause_batches_outside_terms(store, before, after, actor["id"])
    return after


# ---------------------------------------------------------------------------
# 批次标识
# ---------------------------------------------------------------------------

def _freeze_snapshot(store, product):
    """校验组合内每项内容的最新许可均有效，返回冻结快照；任一无效则整体拒发。"""
    snapshot, failures = [], []
    for it in product["items"]:
        content = store.get("SELECT * FROM contents WHERE id = ?", (it["content_id"],))
        if content is None:
            failures.append({"content_id": it["content_id"],
                             "reasons": ["内容不存在"]})
            continue
        lic = latest_license(store, it["content_id"])
        if lic is None:
            failures.append({"content_id": it["content_id"],
                             "reasons": ["尚未取得任何许可"]})
            continue
        ok, reasons = evaluate_license(
            lic, purpose=product["purpose"],
            territory=product["territory"], channel=product["channel"],
        )
        if not ok:
            failures.append({"content_id": it["content_id"],
                             "version": lic["version"], "reasons": reasons})
            continue
        custodian = store.get("SELECT * FROM parties WHERE id = ?",
                              (content["custodian"],))
        snapshot.append({
            "content_id": content["id"],
            "content_kind": content["kind"],
            "title": content["title"],
            "license_id": lic["id"],
            "version": lic["version"],
            "share_bps": it["share_bps"],
            "custodian_id": content["custodian"],
            "custodian_name": custodian["name"] if custodian else content["custodian"],
            "credit": lic["required_credit"],
            "sensitivity": lic["sensitivity"],
        })
    return snapshot, failures


def issue_batch(store, actor, product_id, *, quantity=0):
    """组合内全部许可有效才发放批次标识，并冻结许可版本快照。"""
    require_role(actor, "coop")
    product = get_product(store, product_id)
    sweep_expired(store)
    product = get_product(store, product_id)
    snapshot, failures = _freeze_snapshot(store, product)
    if failures:
        raise DomainError(
            "license_blocked",
            "组合内存在无效许可，批次标识不予发放",
            status=422,
            details={"blocked": failures},
        )
    batch_id = _batch_id()
    ts = now_iso()
    with store.lock, store.conn:
        store.execute(
            """INSERT INTO batches(id, product_id, status, quantity, content_bps,
                                   community_bps, issued_at, license_snapshot)
               VALUES(?,?, 'issued', ?,?,?,?,?)""",
            (batch_id, product_id, quantity, product["content_bps"],
             product["community_bps"], ts, store.dumps(snapshot)),
        )
        for item in snapshot:
            store.execute(
                "INSERT INTO batch_items(batch_id, content_id, license_id, version, share_bps) VALUES(?,?,?,?,?)",
                (batch_id, item["content_id"], item["license_id"],
                 item["version"], item["share_bps"]),
            )
        store.execute(
            """INSERT INTO batch_events(id, batch_id, event_type, actor, reason,
                                        snapshot, created_at)
               VALUES(?,?, 'issued', ?,?,?,?)""",
            (_new_id("BE-"), batch_id, actor["id"],
             "组合内全部许可有效，发放批次标识",
             store.dumps([{"content_id": s["content_id"], "version": s["version"]}
                          for s in snapshot]), ts),
        )
    return get_batch(store, batch_id)


def get_batch(store, batch_id):
    row = store.get("SELECT * FROM batches WHERE id = ?", (batch_id,))
    if row is None:
        raise DomainError("unknown_batch", f"批次标识不存在：{batch_id}", 404)
    data = _row(row)
    data["license_snapshot"] = store.loads(data["license_snapshot"], [])
    data["product"] = _row(store.get(
        "SELECT id, name, purpose, territory, channel FROM products WHERE id = ?",
        (row["product_id"],),
    ))
    data["events"] = [
        _row(r) for r in store.all(
            "SELECT id, event_type, actor, reason, created_at FROM batch_events WHERE batch_id = ? ORDER BY created_at",
            (batch_id,),
        )
    ]
    return data


def _pause_batch_locked(store, batch_id, reason, actor):
    ts = now_iso()
    store.execute(
        "UPDATE batches SET status = 'paused', paused_at = ? WHERE id = ? AND status = 'issued'",
        (ts, batch_id),
    )
    store.execute(
        """INSERT INTO batch_events(id, batch_id, event_type, actor, reason, created_at)
           VALUES(?,?,'paused',?,?,?)""",
        (_new_id("BE-"), batch_id, actor, reason, ts),
    )


def pause_batch(store, actor, batch_id, *, reason):
    """合作社手工暂停（例如巡查发现超渠道销售）。"""
    require_role(actor, "coop")
    get_batch(store, batch_id)  # 存在性校验
    with store.lock, store.conn:
        _pause_batch_locked(store, batch_id, reason, actor["id"])
    return get_batch(store, batch_id)


def close_batch(store, actor, batch_id, *, reason):
    """关闭批次：不再接受新销售，历史销售保留。"""
    require_role(actor, "coop")
    get_batch(store, batch_id)
    ts = now_iso()
    with store.lock, store.conn:
        store.execute(
            "UPDATE batches SET status = 'closed', close_reason = ? WHERE id = ?",
            (reason, batch_id),
        )
        store.execute(
            """INSERT INTO batch_events(id, batch_id, event_type, actor, reason, created_at)
               VALUES(?,?,'closed',?,?,?)""",
            (_new_id("BE-"), batch_id, actor["id"], reason, ts),
        )
    return get_batch(store, batch_id)


def _pause_batches_for_content(store, content_id, reason, actor):
    with store.lock, store.conn:
        rows = store.all(
            "SELECT batch_id FROM batch_items WHERE content_id = ?",
            (content_id,),
        )
        for r in rows:
            batch = store.get(
                "SELECT status FROM batches WHERE id = ?", (r["batch_id"],)
            )
            if batch and batch["status"] == "issued":
                _pause_batch_locked(store, r["batch_id"], reason, actor)
        return [r["batch_id"] for r in rows]


def _try_resume_batches_for_content(store, content_id, actor):
    """复核后许可恢复 active：含该内容的暂停批次，仅当全部内容重新合规才恢复。"""
    resumed = []
    rows = store.all(
        """SELECT DISTINCT b.id FROM batches b JOIN batch_items bi ON bi.batch_id = b.id
           WHERE bi.content_id = ? AND b.status = 'paused'""",
        (content_id,),
    )
    for r in rows:
        batch = get_batch(store, r["id"])
        product = get_product(store, batch["product_id"])
        snapshot, failures = _freeze_snapshot(store, product)
        if failures:
            continue
        ts = now_iso()
        with store.lock, store.conn:
            store.execute(
                "UPDATE batches SET status = 'issued', paused_at = NULL WHERE id = ?",
                (r["id"],),
            )
            store.execute(
                """INSERT INTO batch_events(id, batch_id, event_type, actor, reason, created_at)
                   VALUES(?,?,'resumed',?,?,?)""",
                (_new_id("BE-"), r["id"], actor,
                "复核后全部内容许可重新有效，恢复生产", ts),
            )
        resumed.append(r["id"])
    return resumed


def _pause_batches_outside_terms(store, before, after, actor):
    """商品渠道/地域变更后，超出许可约定的在发批次暂停。"""
    if (before["channel"] == after["channel"]
            and before["territory"] == after["territory"]
            and before["purpose"] == after["purpose"]):
        return
    rows = store.all(
        "SELECT id FROM batches WHERE product_id = ? AND status = 'issued'",
        (after["id"],),
    )
    for r in rows:
        batch = get_batch(store, r["id"])
        failures = []
        for item in batch["license_snapshot"]:
            lic_row = store.get(
                "SELECT * FROM licenses WHERE id = ?", (item["license_id"],)
            )
            lic = _license_to_dict(store, lic_row)
            ok, reasons = evaluate_license(
                lic, purpose=after["purpose"],
                territory=after["territory"], channel=after["channel"],
            )
            if not ok:
                failures.append((item["content_id"], reasons))
        if failures:
            detail = "；".join(f"{cid}:{'/'.join(rs)}" for cid, rs in failures)
            with store.lock, store.conn:
                _pause_batch_locked(
                    store, r["id"],
                    f"商品销售安排超出许可约定，暂停新生产（{detail}）", actor,
                )


def public_verify(store, batch_id):
    """公开核验：只验证标识真伪与必要说明，不暴露敏感级别、家族含义、条款细节。"""
    batch = get_batch(store, batch_id)
    kind_label = {"pattern": "纹样", "technique": "技法",
                  "story": "口述故事", "medicine": "药用知识"}
    credits = []
    for s in batch["license_snapshot"]:
        if s["sensitivity"] in ("internal", "restricted"):
            # 族内/受限知识不公开标题与署名单，只确认其使用已经过社区许可
            credits.append({
                "kind": s["content_kind"],
                "title": f"社区{kind_label.get(s['content_kind'], '内容')}（族内传承，不公开）",
                "credit": "经鄂伦春族乡传承人小组许可使用",
            })
        else:
            credits.append({
                "kind": s["content_kind"],
                "title": s["title"],
                "credit": s["credit"],
            })
    return {
        "batch_id": batch["id"],
        "valid": batch["status"] == "issued",
        "status": batch["status"],
        "product": {"name": batch["product"]["name"]},
        "issued_at": batch["issued_at"],
        "required_credits": credits,
        "notice": ("标识真实有效" if batch["status"] == "issued"
                   else "标识真实，但该批次当前已暂停新生产或关闭；此前发生的销售记录仍然有效"),
    }


# ---------------------------------------------------------------------------
# 销售上报（幂等去重）与结算
# ---------------------------------------------------------------------------

def _allocate(amount_fen, batch):
    """按批次发放时冻结的规则把一笔销售分到社区基金与各贡献者。"""
    content_bps, community_bps = batch["content_bps"], batch["community_bps"]
    content_pool = amount_fen * content_bps // 10000
    community_base = content_pool * community_bps // 10000
    holder_pool = content_pool - community_base

    items = batch["license_snapshot"]
    exact = [holder_pool * it["share_bps"] / 10000 for it in items]
    floors = [int(x) for x in exact]
    leftover = holder_pool - sum(floors)
    # 最大余数法分配取整余数
    order = sorted(range(len(items)),
                   key=lambda i: (exact[i] - floors[i]), reverse=True)
    for i in range(leftover):
        floors[order[i % len(order)]] += 1

    lines, allocated = [], 0
    for it, amount in zip(items, floors):
        if amount <= 0:
            continue
        lines.append({
            "payee": it["custodian_id"],
            "payee_name": it["custodian_name"],
            "content_id": it["content_id"],
            "license_id": it["license_id"],
            "version": it["version"],
            "amount_fen": amount,
            "rule": (f"《{it['title']}》登记贡献者：内容收益扣除社区基金后，"
                     f"按组合内约定比例 {it['share_bps'] / 100:.2f}% 分配"
                     f"（依据许可 v{it['version']}）"),
        })
        allocated += amount
    community_total = content_pool - allocated
    if community_total > 0:
        lines.insert(0, {
            "payee": COMMUNITY_FUND,
            "payee_name": "社区基金",
            "content_id": None,
            "license_id": None,
            "version": None,
            "amount_fen": community_total,
            "rule": (f"社区基金：文化内容收益的 {community_bps / 100:.2f}%"
                     "（含取整余数），比例在批次发放时冻结"),
        })
    rationale = (
        f"批次 {batch['id']}（商品《{batch['product']['name']}》）销售 {amount_fen / 100:.2f} 元："
        f"按批次发放时冻结的规则，销售额的 {content_bps / 100:.2f}%（{content_pool / 100:.2f} 元）"
        f"为文化内容收益；其中 {community_bps / 100:.2f}% 划入社区基金，"
        f"其余按商品组合内各项约定比例分给登记贡献者，每项均沿用发放时冻结的许可版本；"
        f"销售额剩余 {(amount_fen - content_pool) / 100:.2f} 元为商品其他部分，不参与文化内容分配。"
        f"取整产生的余数并入社区基金。"
    )
    return lines, rationale, content_pool


def report_sale(store, actor, *, external_key, batch_id, amount_fen,
                sold_at=None, channel="", territory=""):
    """景区/商户上报销售：同一 external_key 只受理、只结算一次。"""
    require_role(actor, "scenic", "merchant")
    if not external_key:
        raise DomainError("missing_external_key", "上报须带同一笔销售的唯一流水号")
    if int(amount_fen) <= 0:
        raise DomainError("invalid_amount", "销售金额须为正整数（分）")
    amount_fen = int(amount_fen)
    batch = get_batch(store, batch_id)
    product = get_product(store, batch["product_id"])
    channel = channel or product["channel"]
    territory = territory or product["territory"]
    ts = now_iso()

    with store.lock, store.conn:
        existing = store.get(
            "SELECT * FROM sales WHERE external_key = ?", (external_key,)
        )
        if existing is not None:
            # 重复上报：留痕、累加计数，绝不二次结算
            store.execute(
                "UPDATE sales SET duplicate_count = duplicate_count + 1 WHERE id = ?",
                (existing["id"],),
            )
            store.execute(
                """INSERT INTO sales_reports(id, external_key, reporter, batch_id,
                                             amount_fen, accepted, created_at)
                   VALUES(?,?,?,?,?,0,?)""",
                (_new_id("R-"), external_key, actor["id"], batch_id,
                 amount_fen, ts),
            )
            return {
                "accepted": False,
                "duplicate": True,
                "sale_id": existing["id"],
                "settled_once": True,
                "settlement_id": existing["settlement_id"],
                "message": "该笔销售此前已上报并结算，本次为重复上报，不再重复结算",
            }

        # 首次上报：批次暂停/关闭期间不受理新销售（历史销售不抹去）
        if batch["status"] != "issued":
            store.execute(
                """INSERT INTO sales_reports(id, external_key, reporter, batch_id,
                                             amount_fen, accepted, created_at)
                   VALUES(?,?,?,?,?,0,?)""",
                (_new_id("R-"), external_key, actor["id"], batch_id,
                 amount_fen, ts),
            )
            store.commit()  # 保留上报痕迹后再拒绝
            raise DomainError(
                "batch_not_active",
                f"批次 {batch_id} 当前状态为 {batch['status']}，不接受新销售；"
                "此前已发生的销售记录保留不变",
                status=409,
            )

        # 防超渠道/超地域：实际销售地与约定不符时暂停批次并拒绝本笔
        violations = []
        for item in batch["license_snapshot"]:
            lic = _license_to_dict(
                store,
                store.get("SELECT * FROM licenses WHERE id = ?",
                          (item["license_id"],)),
            )
            ok, reasons = evaluate_license(
                lic, purpose=product["purpose"],
                territory=territory, channel=channel,
            )
            if not ok:
                violations.append((item, reasons))
        if violations:
            detail = "；".join(
                f"《{it['title']}》v{it['version']}:{'/'.join(rs)}"
                for it, rs in violations
            )
            _pause_batch_locked(
                store, batch_id,
                f"销售实际渠道/地域超出许可约定，暂停新生产（{detail}）",
                actor["id"],
            )
            store.execute(
                """INSERT INTO sales_reports(id, external_key, reporter, batch_id,
                                             amount_fen, accepted, created_at)
                   VALUES(?,?,?,?,?,0,?)""",
                (_new_id("R-"), external_key, actor["id"], batch_id,
                 amount_fen, ts),
            )
            store.commit()  # 暂停决定与上报痕迹先落库，再拒绝本笔
            raise DomainError(
                "outside_licensed_terms",
                "该笔销售超出许可约定的渠道/地域，批次已暂停，本笔不予结算",
                status=409,
                details={"violations": [
                    {"content_id": it["content_id"], "reasons": rs}
                    for it, rs in violations
                ]},
            )

        sale_id = _new_id("S-")
        store.execute(
            """INSERT INTO sales(id, external_key, batch_id, product_id, reporter,
                                 amount_fen, community_bps, sold_at, reported_at,
                                 status)
               VALUES(?,?,?,?,?,?,?,?,?, 'pending')""",
            (sale_id, external_key, batch_id, batch["product_id"], actor["id"],
             amount_fen, batch["community_bps"], sold_at or ts, ts),
        )
        store.execute(
            """INSERT INTO sales_reports(id, external_key, reporter, batch_id,
                                         amount_fen, accepted, created_at)
               VALUES(?,?,?,?,?,1,?)""",
            (_new_id("R-"), external_key, actor["id"], batch_id, amount_fen, ts),
        )

        # 同事务内立即按冻结版本结算，保证“一笔销售只结算一次”
        lines, rationale, content_pool = _allocate(amount_fen, batch)
        settlement_id = _new_id("ST-")
        store.execute(
            "INSERT INTO settlements(id, batch_id, total_fen, created_at, rationale) VALUES(?,?,?,?,?)",
            (settlement_id, batch_id, amount_fen, ts, rationale),
        )
        for line in lines:
            store.execute(
                """INSERT INTO settlement_lines(id, settlement_id, payee, content_id,
                                                license_id, amount_fen, rule)
                   VALUES(?,?,?,?,?,?,?)""",
                (_new_id("SL-"), settlement_id, line["payee"], line["content_id"],
                 line["license_id"], line["amount_fen"], line["rule"]),
            )
        store.execute(
            "UPDATE sales SET status = 'settled', settlement_id = ? WHERE id = ?",
            (settlement_id, sale_id),
        )

    return {
        "accepted": True,
        "duplicate": False,
        "sale_id": sale_id,
        "settlement_id": settlement_id,
        "settled_once": True,
        "lines": lines,
        "rationale": rationale,
    }


def _settlement_dict(store, row, viewer=None):
    data = _row(row)
    lines = [_row(r) for r in store.all(
        "SELECT * FROM settlement_lines WHERE settlement_id = ? ORDER BY rowid",
        (row["id"],),
    )]
    if viewer is not None and viewer["role"] == "holder":
        # 贡献者只能看到与自己相关的分账行，但保留总额与规则解释
        lines = [ln for ln in lines if ln["payee"] == viewer["id"]]
    data["lines"] = lines
    return data


def get_settlement(store, actor, settlement_id):
    row = store.get("SELECT * FROM settlements WHERE id = ?", (settlement_id,))
    if row is None:
        raise DomainError("unknown_settlement", f"结算单不存在：{settlement_id}", 404)
    if actor["role"] in ("scenic", "merchant"):
        # 上报方可确认自己那笔销售的结算状态，但不看分账明细
        sale = store.get(
            "SELECT * FROM sales WHERE settlement_id = ? AND reporter = ?",
            (settlement_id, actor["id"]),
        )
        if sale is None:
            raise DomainError("forbidden", "只能查询自己上报销售的结算结果", 403)
        return {"id": row["id"], "batch_id": row["batch_id"],
                "total_fen": row["total_fen"], "created_at": row["created_at"],
                "status": "settled"}
    require_role(actor, "holder", "coop")
    viewer = actor if actor["role"] == "holder" else None
    return _settlement_dict(store, row, viewer)


def list_settlements(store, actor, *, batch_id=None, payee=None, limit=50):
    """合作社看全量台账；传承人只看与自己相关的结算；每笔附带规则解释。"""
    require_role(actor, "holder", "coop")
    sql = "SELECT * FROM settlements WHERE 1=1"
    params = []
    if batch_id:
        sql += " AND batch_id = ?"
        params.append(batch_id)
    if actor["role"] == "holder":
        payee = actor["id"]
    if payee:
        sql += (" AND id IN (SELECT settlement_id FROM settlement_lines WHERE payee = ?)")
        params.append(payee)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(min(int(limit), 200))
    viewer = actor if actor["role"] == "holder" else None
    return [_settlement_dict(store, r, viewer)
            for r in store.all(sql, params)]


def list_sales(store, actor, *, batch_id=None):
    if actor["role"] not in ("coop", "scenic", "merchant"):
        require_role(actor, "coop")
    sql = """SELECT s.id, s.external_key, s.batch_id, s.product_id, s.amount_fen,
                    s.status, s.settlement_id, s.duplicate_count, s.sold_at,
                    s.reported_at, p.name AS reporter_name
             FROM sales s JOIN parties p ON p.id = s.reporter WHERE 1=1"""
    params = []
    if actor["role"] in ("scenic", "merchant"):
        sql += " AND s.reporter = ?"
        params.append(actor["id"])
    if batch_id:
        sql += " AND s.batch_id = ?"
        params.append(batch_id)
    sql += " ORDER BY s.reported_at DESC"
    return [_row(r) for r in store.all(sql, params)]
