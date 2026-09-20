"""社区许可领域逻辑。

本模块不依赖任何第三方库，负责：

* 内容（纹样、技法、口述故事、药用知识）登记与敏感级别；
* 传承人小组发放的许可及其条款版本（用途、地域、渠道、期限、署名、分账份额）；
* 长者撤回 / 家族异议触发的“暂停新生产—复核—新版本”流程；
* 商品组合的批次门控：组合内每一项许可都有效才签发批次标识；
* 销售上报去重（景区与商户重复上报只结算一次）；
* 按批次签发时锁定的许可版本快照分配收益，并保留每笔分配的依据；
* 对外只暴露标识验证与必要说明，敏感细节仅合作社内部可见。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# 常量与枚举
# ---------------------------------------------------------------------------

KINDS = ("pattern", "technique", "story", "medicine")
KIND_LABELS = {
    "pattern": "纹样",
    "technique": "技法",
    "story": "口述故事",
    "medicine": "药用知识",
}

# public     ：可对外公开（讲解员可讲述、可印制公开说明）
# community  ：仅限合作社 / 族内场景使用
# restricted ：只适合族内传承，不对外披露细节
SENSITIVITIES = ("public", "community", "restricted")

SCOPES = ("tourism", "medicine_tourism")
SCOPE_LABELS = {"tourism": "文旅", "medicine_tourism": "药旅"}

LICENSE_ACTIVE = "active"
LICENSE_SUSPENDED = "suspended"
LICENSE_SUPERSEDED = "superseded"
LICENSE_REVOKED = "revoked"

PRODUCT_ACTIVE = "active"
PRODUCT_SUSPENDED = "suspended"
PRODUCT_DISCONTINUED = "discontinued"

DECISION_RESTORE = "restore"   # 复核认为不成立，恢复
DECISION_REVISE = "revise"     # 复核成立，按新条款形成新版本
DECISION_UPHOLD = "uphold"     # 复核维持撤回 / 异议成立，停止使用

COMMUNITY_FUND = "community_fund"
COMMUNITY_FUND_NAME = "社区基金"


class DomainError(Exception):
    """业务规则被违反。detail 携带结构化原因，便于调用方展示。"""

    def __init__(self, message: str, detail: Any = None):
        super().__init__(message)
        self.detail = detail


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class ConflictError(DomainError):
    """状态冲突（例如批次门控未通过、重复上报）。"""


def _require(value: Any, message: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DomainError(message)
    return value


def _choice(value: str, choices: Iterable[str], message: str) -> str:
    if value not in choices:
        raise DomainError(message, {"got": value, "allowed": list(choices)})
    return value


def _today(clock: Callable[[], datetime]) -> str:
    return clock().date().isoformat()


def amount_to_cents(amount: Any) -> int:
    """把元为单位的金额转为整数分，拒绝负数与超过两位小数的输入。"""
    try:
        dec = Decimal(str(amount))
    except Exception:  # noqa: BLE001 - 输入即文本，统一转业务错误
        raise DomainError("金额无法识别", {"amount": amount})
    if dec < 0:
        raise DomainError("金额不能为负", {"amount": amount})
    cents = (dec * 100).to_integral_value(rounding=ROUND_FLOOR)
    if Decimal(cents) != dec * 100:
        raise DomainError("金额最多保留两位小数", {"amount": amount})
    return int(cents)


def cents_to_yuan(cents: int) -> float:
    return cents / 100


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


def _empty_data() -> dict:
    return {
        "contents": {},
        "licenses": {},
        "products": {},
        "batches": {},
        "sales": {},
        "objections": {},
        "counters": {},
    }


class Store:
    """内存仓库，可选择原子写入单个 JSON 文件持久化。"""

    def __init__(
        self,
        path: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.path = path
        self.clock = clock or (lambda: datetime.now())
        self._lock = threading.RLock()
        self.data = _empty_data()
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.data = payload.get("data", _empty_data())
            for key in _empty_data():
                self.data.setdefault(key, {} if key != "counters" else {})

    # -- 基础设施 -----------------------------------------------------------

    def _now(self) -> str:
        return self.clock().isoformat(timespec="seconds")

    def _new_id(self, prefix: str) -> str:
        counters = self.data["counters"]
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}{counters[prefix]:04d}-{uuid.uuid4().hex[:6]}"

    def _save(self) -> None:
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"schema": 1, "data": self.data}, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _get(self, collection: str, record_id: str) -> dict:
        record = self.data[collection].get(record_id)
        if record is None:
            raise NotFoundError(f"对象不存在: {record_id}", {"id": record_id, "collection": collection})
        return record

    # -- 内容登记 -----------------------------------------------------------

    def register_content(
        self,
        kind: str,
        title: str,
        *,
        source_persons: list[str] | None = None,
        source_families: list[str] | None = None,
        sensitivity: str = "community",
        public_summary: str | None = None,
        custodian_group: str = "传承人小组",
        recorder: str | None = None,
        content_id: str | None = None,
    ) -> dict:
        """登记一项社区知识内容。

        source_persons  必须署名的传承人 / 长者；
        source_families 拥有家族含义的家族；
        sensitivity     公开范围，决定公开查询能看到什么。
        """
        with self._lock:
            _choice(kind, KINDS, "内容类型不合法")
            _choice(sensitivity, SENSITIVITIES, "敏感级别不合法")
            _require(title, "内容名称必填")
            record = {
                "id": content_id or self._new_id("C"),
                "kind": kind,
                "title": title.strip(),
                "source_persons": list(dict.fromkeys(source_persons or [])),
                "source_families": list(dict.fromkeys(source_families or [])),
                "sensitivity": sensitivity,
                "public_summary": (public_summary or "").strip() or None,
                "custodian_group": custodian_group,
                "recorder": recorder,
                "created_at": self._now(),
                "events": [
                    {"at": self._now(), "type": "registered", "by": recorder, "note": "内容登记"}
                ],
            }
            self.data["contents"][record["id"]] = record
            self._save()
            return dict(record)

    def content(self, content_id: str) -> dict:
        with self._lock:
            return dict(self._get("contents", content_id))

    def list_contents(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.data["contents"].values()]

    def public_content_brief(self, content_id: str) -> dict:
        """讲解员 / 对外场景：非公开内容不披露存在与细节。"""
        with self._lock:
            record = self.data["contents"].get(content_id)
            if record is None or record["sensitivity"] != "public":
                raise NotFoundError("公开内容不存在", {"id": content_id})
            return {
                "id": record["id"],
                "kind": record["kind"],
                "kind_label": KIND_LABELS[record["kind"]],
                "title": record["title"],
                "public_summary": record["public_summary"],
                "attribution": "、".join(record["source_persons"]),
                "custodian_group": record["custodian_group"],
            }

    # -- 许可与版本 ---------------------------------------------------------

    def issue_license(
        self,
        content_id: str,
        *,
        scope: str = "tourism",
        territories: list[str] | None = None,
        channels: list[str] | None = None,
        expires_on: str | None = None,
        attribution: list[str] | None = None,
        shares: list[dict] | None = None,
        issued_by: str | None = None,
        effective_on: str | None = None,
        license_id: str | None = None,
    ) -> dict:
        """传承人小组为内容发放第一版许可。"""
        with self._lock:
            content = self._get("contents", content_id)
            _choice(scope, SCOPES, "许可用途不合法")
            terms = self._validate_terms(
                {
                    "scope": scope,
                    "territories": territories or ["*"],
                    "channels": channels or ["*"],
                    "effective_on": effective_on or _today(self.clock),
                    "expires_on": expires_on,
                    "attribution": list(dict.fromkeys(attribution or content["source_persons"])),
                },
                content,
            )
            clean_shares = self._validate_shares(shares or [], content)
            record = {
                "id": license_id or self._new_id("L"),
                "content_id": content_id,
                "current_version": 1,
                "versions": [
                    {
                        "version": 1,
                        "status": LICENSE_ACTIVE,
                        "terms": terms,
                        "shares": clean_shares,
                        "issued_at": self._now(),
                        "issued_by": issued_by,
                        "supersedes": None,
                        "events": [
                            {"at": self._now(), "type": "issued", "by": issued_by, "note": "许可发放"}
                        ],
                    }
                ],
            }
            self.data["licenses"][record["id"]] = record
            self._save()
            return self.license(record["id"])

    def _validate_terms(self, raw: dict, content: dict) -> dict:
        terms = {
            "scope": _choice(raw["scope"], SCOPES, "许可用途不合法"),
            "territories": list(dict.fromkeys(raw["territories"] or ["*"])),
            "channels": list(dict.fromkeys(raw["channels"] or ["*"])),
            "effective_on": raw["effective_on"] or _today(self.clock),
            "expires_on": raw.get("expires_on"),
            "attribution": list(dict.fromkeys(raw["attribution"] or content["source_persons"])),
        }
        if terms["expires_on"] and terms["expires_on"] < terms["effective_on"]:
            raise DomainError("到期日不能早于生效日", terms)
        return terms

    def _validate_shares(self, shares: list[dict], content: dict) -> list[dict]:
        clean: list[dict] = []
        total = Decimal("0")
        for entry in shares:
            payee = _require(entry.get("payee"), "分账收款方必填")
            share = Decimal(str(entry.get("share", 0)))
            if share <= 0 or share > 1:
                raise DomainError("分账份额必须在 0 到 1 之间", {"payee": payee, "share": str(share)})
            total += share
            clean.append({"payee": payee, "share": float(share), "note": entry.get("note")})
        if total > 1:
            raise DomainError("贡献者分账份额合计不能超过 1，余额自动归入社区基金", {"total": str(total)})
        return clean

    def license(self, license_id: str) -> dict:
        with self._lock:
            return self._license_view(self._get("licenses", license_id))

    def _license_view(self, record: dict) -> dict:
        view = dict(record)
        view["versions"] = [dict(v) for v in record["versions"]]
        view["current"] = next(
            v for v in record["versions"] if v["version"] == record["current_version"]
        )
        return view

    def list_licenses(self) -> list[dict]:
        with self._lock:
            return [self._license_view(r) for r in self.data["licenses"].values()]

    def _current_version(self, license_record: dict) -> dict:
        return next(
            v for v in license_record["versions"] if v["version"] == license_record["current_version"]
        )

    def _license_for_content(self, content_id: str, scope: str) -> dict | None:
        for record in self.data["licenses"].values():
            if record["content_id"] != content_id:
                continue
            version = self._current_version(record)
            if version["terms"]["scope"] == scope:
                return record
        return None

    # -- 撤回 / 异议 / 复核 --------------------------------------------------

    def raise_objection(self, content_id: str, raised_by: str, reason: str, *, relation: str = "family_member") -> dict:
        """家族成员等对内容提出异议：暂停该内容全部当前有效许可的新使用。"""
        with self._lock:
            content = self._get("contents", content_id)
            _require(raised_by, "提出人必填")
            _require(reason, "异议理由必填")
            affected: list[dict] = []
            for license_record in self.data["licenses"].values():
                if license_record["content_id"] != content_id:
                    continue
                version = self._current_version(license_record)
                if version["status"] == LICENSE_ACTIVE:
                    version["status"] = LICENSE_SUSPENDED
                    version["events"].append(
                        {"at": self._now(), "type": "suspended", "by": raised_by, "note": reason}
                    )
                    affected.append({"license_id": license_record["id"], "version": version["version"]})
            objection = {
                "id": self._new_id("O"),
                "kind": "objection",
                "content_id": content_id,
                "raised_by": raised_by,
                "relation": relation,
                "reason": reason,
                "status": "open",
                "affected": affected,
                "created_at": self._now(),
                "resolution": None,
            }
            content["events"].append(
                {"at": self._now(), "type": "objection_raised", "by": raised_by, "note": reason}
            )
            self.data["objections"][objection["id"]] = objection
            self._save()
            return dict(objection)

    def withdraw_license(self, license_id: str, withdrawn_by: str, reason: str) -> dict:
        """长者撤回某项许可：立即暂停，等待复核决定。"""
        with self._lock:
            license_record = self._get("licenses", license_id)
            version = self._current_version(license_record)
            _require(withdrawn_by, "撤回人必填")
            _require(reason, "撤回理由必填")
            if version["status"] != LICENSE_ACTIVE:
                raise ConflictError(
                    "只有生效中的许可可以撤回",
                    {"license_id": license_id, "status": version["status"]},
                )
            version["status"] = LICENSE_SUSPENDED
            version["events"].append(
                {"at": self._now(), "type": "suspended", "by": withdrawn_by, "note": f"长者撤回：{reason}"}
            )
            objection = {
                "id": self._new_id("O"),
                "kind": "withdrawal",
                "content_id": license_record["content_id"],
                "raised_by": withdrawn_by,
                "relation": "elder",
                "reason": reason,
                "status": "open",
                "affected": [{"license_id": license_id, "version": version["version"]}],
                "created_at": self._now(),
                "resolution": None,
            }
            self.data["objections"][objection["id"]] = objection
            self._save()
            return dict(objection)

    def review_objection(
        self,
        objection_id: str,
        decision: str,
        reviewer: str,
        *,
        new_terms: dict | None = None,
        note: str | None = None,
    ) -> dict:
        """复核：恢复、按新条款形成新版本，或维持停止。"""
        with self._lock:
            objection = self._get("objections", objection_id)
            _choice(
                decision,
                (DECISION_RESTORE, DECISION_REVISE, DECISION_UPHOLD),
                "复核决定不合法",
            )
            _require(reviewer, "复核人必填")
            if objection["status"] != "open":
                raise ConflictError("该事项已经完成复核", {"objection_id": objection_id})

            resulted_versions: list[dict] = []
            for item in objection["affected"]:
                license_record = self._get("licenses", item["license_id"])
                version = next(
                    v for v in license_record["versions"] if v["version"] == item["version"]
                )
                if decision == DECISION_RESTORE:
                    version["status"] = LICENSE_ACTIVE
                    version["events"].append(
                        {"at": self._now(), "type": "resumed", "by": reviewer, "note": note or "复核恢复"}
                    )
                elif decision == DECISION_UPHOLD:
                    version["status"] = LICENSE_REVOKED
                    version["events"].append(
                        {"at": self._now(), "type": "revoked", "by": reviewer, "note": note or "复核维持撤回"}
                    )
                else:  # revise：旧版作废（保留历史），按新条款形成新版本
                    if not new_terms:
                        raise DomainError("修订重发必须提供 new_terms 新条款")
                    content = self._get("contents", objection["content_id"])
                    merged = dict(version["terms"])
                    for key in ("territories", "channels", "expires_on", "attribution", "scope", "effective_on"):
                        if key in new_terms and new_terms[key] is not None:
                            merged[key] = (
                                list(dict.fromkeys(new_terms[key]))
                                if key in ("territories", "channels", "attribution")
                                else new_terms[key]
                            )
                    merged = self._validate_terms(merged, content)
                    new_shares = (
                        self._validate_shares(new_terms["shares"], content)
                        if new_terms.get("shares") is not None
                        else [dict(s) for s in version["shares"]]
                    )
                    new_number = max(v["version"] for v in license_record["versions"]) + 1
                    version["status"] = LICENSE_SUPERSEDED
                    version["events"].append(
                        {"at": self._now(), "type": "superseded", "by": reviewer, "note": note or "复核修订"}
                    )
                    new_version = {
                        "version": new_number,
                        "status": LICENSE_ACTIVE,
                        "terms": merged,
                        "shares": new_shares,
                        "issued_at": self._now(),
                        "issued_by": reviewer,
                        "supersedes": version["version"],
                        "events": [
                            {
                                "at": self._now(),
                                "type": "reissued_after_review",
                                "by": reviewer,
                                "note": f"依据复核 {objection_id} 形成新版本",
                            }
                        ],
                    }
                    license_record["versions"].append(new_version)
                    license_record["current_version"] = new_number
                    resulted_versions.append({"license_id": license_record["id"], "version": new_number})

            objection["status"] = "closed"
            objection["closed_at"] = self._now()
            objection["resolution"] = {
                "decision": decision,
                "reviewer": reviewer,
                "note": note,
                "resulted_versions": resulted_versions,
            }
            self._save()
            return dict(objection)

    def objection(self, objection_id: str) -> dict:
        with self._lock:
            return dict(self._get("objections", objection_id))

    def list_objections(self, *, status: str | None = None) -> list[dict]:
        with self._lock:
            records = list(self.data["objections"].values())
            if status:
                records = [r for r in records if r["status"] == status]
            return [dict(r) for r in records]

    # -- 商品组合 -----------------------------------------------------------

    def register_product(
        self,
        name: str,
        items: list[dict],
        *,
        purpose: str = "tourism",
        channels: list[str] | None = None,
        territories: list[str] | None = None,
        producer: str | None = None,
        product_id: str | None = None,
    ) -> dict:
        """登记商品组合。items 形如 [{"content_id": "...", "license_id": 可选}]。"""
        with self._lock:
            _require(name, "商品名称必填")
            _choice(purpose, SCOPES, "商品用途不合法")
            if not items:
                raise DomainError("商品组合至少包含一项内容")
            clean_items: list[dict] = []
            for item in items:
                content_id = _require(item.get("content_id"), "组合项缺少 content_id")
                self._get("contents", content_id)
                weight = item.get("weight", 1)
                if not isinstance(weight, (int, float)) or weight <= 0:
                    raise DomainError("组合项权重必须为正数", {"content_id": content_id, "weight": weight})
                clean_items.append(
                    {"content_id": content_id, "license_id": item.get("license_id"), "weight": weight}
                )
            record = {
                "id": product_id or self._new_id("P"),
                "name": name.strip(),
                "purpose": purpose,
                "channels": list(dict.fromkeys(channels or ["*"])),
                "territories": list(dict.fromkeys(territories or ["*"])),
                "producer": producer,
                "revision": 1,
                "status": PRODUCT_ACTIVE,
                "items": clean_items,
                "created_at": self._now(),
                "events": [{"at": self._now(), "type": "registered", "by": producer, "note": "商品登记"}],
            }
            self.data["products"][record["id"]] = record
            self._save()
            return self.product(record["id"])

    def product(self, product_id: str) -> dict:
        with self._lock:
            return dict(self._get("products", product_id))

    def list_products(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.data["products"].values()]

    def report_channel_breach(self, product_id: str, reported_by: str, detail: str) -> dict:
        """商品超出约定渠道：暂停新的生产，已发批次与已发生销售保留。"""
        with self._lock:
            product = self._get("products", product_id)
            if product["status"] == PRODUCT_DISCONTINUED:
                raise ConflictError("商品已停产，无需暂停", {"product_id": product_id})
            product["status"] = PRODUCT_SUSPENDED
            product["events"].append(
                {"at": self._now(), "type": "suspended", "by": reported_by,
                 "note": f"超出约定渠道：{detail}"}
            )
            self._save()
            return dict(product)

    def review_product(
        self,
        product_id: str,
        decision: str,
        reviewer: str,
        *,
        changes: dict | None = None,
        note: str | None = None,
    ) -> dict:
        """商品暂停后的复核：恢复 / 修订约定（新版本）/ 停产。"""
        with self._lock:
            product = self._get("products", product_id)
            _choice(decision, ("restore", "revise", "discontinue"), "商品复核决定不合法")
            _require(reviewer, "复核人必填")
            if product["status"] != PRODUCT_SUSPENDED:
                raise ConflictError("只有暂停中的商品可以复核", {"product_id": product_id})
            if decision == "restore":
                product["status"] = PRODUCT_ACTIVE
                product["events"].append(
                    {"at": self._now(), "type": "resumed", "by": reviewer, "note": note or "复核恢复生产"}
                )
            elif decision == "discontinue":
                product["status"] = PRODUCT_DISCONTINUED
                product["events"].append(
                    {"at": self._now(), "type": "discontinued", "by": reviewer, "note": note or "复核停产"}
                )
            else:
                if not changes:
                    raise DomainError("修订商品约定必须提供 changes")
                for key in ("name", "channels", "territories", "items"):
                    if key in changes and changes[key] is not None:
                        if key in ("channels", "territories"):
                            product[key] = list(dict.fromkeys(changes[key]))
                        elif key == "items":
                            clean = []
                            for item in changes[key]:
                                content_id = _require(item.get("content_id"), "组合项缺少 content_id")
                                self._get("contents", content_id)
                                weight = item.get("weight", 1)
                                if not isinstance(weight, (int, float)) or weight <= 0:
                                    raise DomainError("组合项权重必须为正数", {"content_id": content_id})
                                clean.append(
                                    {"content_id": content_id, "license_id": item.get("license_id"), "weight": weight}
                                )
                            product["items"] = clean
                        else:
                            product[key] = changes[key]
                product["status"] = PRODUCT_ACTIVE
                product["revision"] += 1
                product["events"].append(
                    {"at": self._now(), "type": "revised", "by": reviewer,
                     "note": f"修订后形成第 {product['revision']} 版：{note or ''}"}
                )
            self._save()
            return dict(product)

    # -- 批次门控 -----------------------------------------------------------

    @dataclass
    class _ResolvedItem:
        content_id: str
        license_id: str
        version: dict
        license_record: dict

    def _resolve_product_items(self, product: dict, channel: str, location: str) -> tuple[list[dict], list[str]]:
        """逐项解析当前许可版本并校验；返回（快照, 失败原因）。"""
        today = _today(self.clock)
        snapshot: list[dict] = []
        reasons: list[str] = []
        total_weight = sum(float(item.get("weight", 1)) for item in product["items"])
        for index, item in enumerate(product["items"]):
            content_id = item["content_id"]
            weight = float(item.get("weight", 1))
            content = self._get("contents", content_id)
            license_record = None
            if item.get("license_id"):
                candidate = self.data["licenses"].get(item["license_id"])
                if candidate and candidate["content_id"] == content_id:
                    license_record = candidate
                else:
                    reasons.append(f"内容 {content_id} 指定的许可不存在或不属于该内容")
                    continue
            else:
                license_record = self._license_for_content(content_id, product["purpose"])
                if license_record is None:
                    reasons.append(f"内容 {content['title']} 没有用途为 {SCOPE_LABELS[product['purpose']]} 的许可")
                    continue
            version = self._current_version(license_record)
            label = f"内容「{content['title']}」许可 {license_record['id']} v{version['version']}"
            if version["status"] != LICENSE_ACTIVE:
                status_text = {
                    LICENSE_SUSPENDED: "已暂停（撤回或异议复核中）",
                    LICENSE_SUPERSEDED: "已被新版本取代",
                    LICENSE_REVOKED: "已撤回并停止使用",
                }[version["status"]]
                reasons.append(f"{label} {status_text}")
                continue
            terms = version["terms"]
            if today < terms["effective_on"]:
                reasons.append(f"{label} 尚未到生效日 {terms['effective_on']}")
            if terms["expires_on"] and today > terms["expires_on"]:
                reasons.append(f"{label} 已于 {terms['expires_on']} 到期")
            if terms["scope"] != product["purpose"]:
                reasons.append(f"{label} 用途 {terms['scope']} 与商品用途 {product['purpose']} 不符")
            if "*" not in terms["channels"] and channel not in terms["channels"]:
                reasons.append(f"{label} 不允许渠道 {channel}")
            if "*" not in terms["territories"] and location not in terms["territories"]:
                reasons.append(f"{label} 不允许地域 {location}")
            snapshot.append(
                {
                    "content_id": content_id,
                    "content_title": content["title"],
                    "content_kind": content["kind"],
                    "sensitivity": content["sensitivity"],
                    "license_id": license_record["id"],
                    "version": version["version"],
                    "weight": weight,
                    "pool_share": weight / total_weight,
                    "terms": dict(terms),
                    "shares": [dict(s) for s in version["shares"]],
                }
            )
        return snapshot, reasons

    def issue_batch(
        self,
        product_id: str,
        *,
        quantity: int,
        producer: str,
        channel: str,
        location: str,
        ref: str | None = None,
    ) -> dict:
        """为一次生产签发批次标识；组合内任何一项许可无效都拒绝签发。"""
        with self._lock:
            product = self._get("products", product_id)
            _require(producer, "生产方必填")
            _require(channel, "销售渠道必填")
            _require(location, "销售地域必填")
            if not isinstance(quantity, int) or quantity <= 0:
                raise DomainError("生产数量必须为正整数", {"quantity": quantity})
            if product["status"] != PRODUCT_ACTIVE:
                raise ConflictError(
                    "商品处于暂停或停产状态，不能开始新的生产",
                    {"product_id": product_id, "status": product["status"]},
                )
            if "*" not in product["channels"] and channel not in product["channels"]:
                raise ConflictError(
                    f"生产渠道 {channel} 超出商品约定渠道",
                    {"allowed": product["channels"]},
                )
            if "*" not in product["territories"] and location not in product["territories"]:
                raise ConflictError(
                    f"销售地域 {location} 超出商品约定地域",
                    {"allowed": product["territories"]},
                )

            snapshot, reasons = self._resolve_product_items(product, channel, location)
            if reasons:
                raise ConflictError("组合中存在未获有效许可的内容，不能发放批次标识", {"reasons": reasons})

            record = {
                "id": self._new_id("B"),
                "label": None,
                "product_id": product_id,
                "product_name": product["name"],
                "product_revision": product["revision"],
                "quantity": quantity,
                "producer": producer,
                "channel": channel,
                "location": location,
                "ref": ref,
                "issued_at": self._now(),
                "snapshot": snapshot,
            }
            record["label"] = f"HRXS-{record['id'][1:]}"
            product["events"].append(
                {"at": self._now(), "type": "batch_issued", "by": producer,
                 "note": f"批次 {record['id']}，数量 {quantity}"}
            )
            self.data["batches"][record["id"]] = record
            self._save()
            return dict(record)

    def batch(self, batch_id: str) -> dict:
        with self._lock:
            record = self._get("batches", batch_id)
            view = dict(record)
            view["snapshot"] = [dict(s) for s in record["snapshot"]]
            return view

    def list_batches(self) -> list[dict]:
        with self._lock:
            return [self.batch(r["id"]) for r in self.data["batches"].values()]

    def verify_batch(self, batch_id: str) -> dict:
        """公开查询：只验证标识与必要说明，不暴露族内知识细节。"""
        with self._lock:
            record = self.data["batches"].get(batch_id)
            if record is None:
                raise NotFoundError("批次标识不存在", {"id": batch_id})
            product = self.data["products"][record["product_id"]]
            contents = []
            for snap in record["snapshot"]:
                content = self.data["contents"][snap["content_id"]]
                attribution = "、".join(snap["terms"]["attribution"])
                if content["sensitivity"] == "public":
                    contents.append(
                        {
                            "kind": KIND_LABELS[content["kind"]],
                            "title": content["title"],
                            "attribution": attribution,
                            "note": content["public_summary"],
                        }
                    )
                else:
                    # 必要说明：确认该类知识已获社区许可，但不披露名称与内容
                    contents.append(
                        {
                            "kind": KIND_LABELS[content["kind"]],
                            "title": None,
                            "attribution": attribution,
                            "note": f"{KIND_LABELS[content['kind']]}属社区内部知识，已获社区许可，细节不予公开",
                        }
                    )
            return {
                "label": record["label"],
                "valid": product["status"] != PRODUCT_DISCONTINUED,
                "product_name": record["product_name"],
                "producer": record["producer"],
                "issued_at": record["issued_at"],
                "channel": record["channel"],
                "location": record["location"],
                "contents": contents,
                "verification_note": "标识真实，生产时组合内各项许可均有效；署名信息须随商品展示。",
            }

    # -- 销售上报与分账 ------------------------------------------------------

    def _settle(self, batch: dict, amount_cents: int) -> dict:
        """按批次快照中的许可版本计算分账。

        两级规则（多项内容组合使用时避免重复分钱）：
        1. 按组合项权重把收入切给每项内容对应的许可（取整向下，余量先挂起）；
        2. 每项内容的池子内，按该许可版本的贡献者份额分配，其余归社区基金；
        3. 第一步取整挂起的零头全部归入社区基金，保证总额精确配平。

        每一行都记录所依据的许可与版本，使每笔收入“为何采用那组规则”可解释。
        """
        lines: list[dict] = []
        pool_amounts: list[int] = []
        total_weight = sum((Decimal(str(snap["weight"])) for snap in batch["snapshot"]), Decimal(0))
        allocated = 0
        for index, snap in enumerate(batch["snapshot"]):
            if index == len(batch["snapshot"]) - 1:
                pool_cents = amount_cents - allocated  # 最后一项吸收取整余量
            else:
                exact_share = Decimal(str(snap["weight"])) / total_weight
                pool_cents = int(
                    (exact_share * Decimal(amount_cents)).to_integral_value(rounding=ROUND_FLOOR)
                )
            pool_amounts.append(pool_cents)
            allocated += pool_cents

        for snap, pool_cents in zip(batch["snapshot"], pool_amounts):
            item_allocated = 0
            for share in snap["shares"]:
                cents = int(
                    (Decimal(str(share["share"])) * Decimal(pool_cents)).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                )
                item_allocated += cents
                lines.append(
                    {
                        "payee": share["payee"],
                        "kind": "contributor",
                        "cents": cents,
                        "share": share["share"],
                        "content_pool_share": snap["pool_share"],
                        "content_pool_cents": pool_cents,
                        "content_id": snap["content_id"],
                        "content_title": snap["content_title"],
                        "license_id": snap["license_id"],
                        "license_version": snap["version"],
                    }
                )
            lines.append(
                {
                    "payee": COMMUNITY_FUND,
                    "kind": "community_fund",
                    "cents": pool_cents - item_allocated,
                    "share": None,
                    "content_pool_share": snap["pool_share"],
                    "content_pool_cents": pool_cents,
                    "content_id": snap["content_id"],
                    "content_title": snap["content_title"],
                    "license_id": snap["license_id"],
                    "license_version": snap["version"],
                }
            )
        total = sum(line["cents"] for line in lines)
        if total != amount_cents:  # 防御性配平
            fund_line = lines[-1]
            fund_line["cents"] += amount_cents - total
        return {
            "currency": "CNY",
            "amount_cents": amount_cents,
            "lines": lines,
            "rule_basis": (
                f"批次 {batch['id']} 签发于 {batch['issued_at']}；收入先按组合权重切给各项内容，"
                "再按签发时锁定的各许可版本份额分给贡献者，余额归入社区基金；"
                "撤回或复核产生新版本不改变已结算批次采用的规则。"
            ),
        }

    def report_sale(
        self,
        batch_id: str,
        *,
        amount: Any,
        reporter: str,
        reporter_type: str,
        sale_ref: str,
        channel: str | None = None,
        location: str | None = None,
        sold_on: str | None = None,
    ) -> dict:
        """上报一笔销售。

        同一 (批次, 销售流水号) 由景区与商户分别上报时，仅首次上报产生结算，
        重复上报被记录但不重复分钱。
        """
        with self._lock:
            batch = self.data["batches"].get(batch_id)
            if batch is None:
                raise NotFoundError("批次标识不存在", {"id": batch_id})
            _require(reporter, "上报方必填")
            _choice(reporter_type, ("scenic_area", "merchant"), "上报方类型不合法")
            _require(sale_ref, "销售流水号必填（用于去重）")
            amount_cents = amount_to_cents(amount)

            duplicate = None
            for existing in self.data["sales"].values():
                if existing["batch_id"] == batch_id and existing["sale_ref"] == sale_ref:
                    duplicate = existing
                    break

            if duplicate is not None:
                same_reporter = any(
                    r["reporter"] == reporter and r["reporter_type"] == reporter_type
                    for r in duplicate["reports"]
                )
                if same_reporter:
                    raise ConflictError(
                        "该销售已由同一上报方报过，请勿重复结算",
                        {"sale_id": duplicate["id"], "sale_ref": sale_ref},
                    )
                duplicate["reports"].append(
                    {"reporter": reporter, "reporter_type": reporter_type, "reported_at": self._now()}
                )
                duplicate["duplicate_count"] = len(duplicate["reports"]) - 1
                self._save()
                view = dict(duplicate)
                view["settled_once"] = True
                view["duplicate"] = True
                return view

            sale_channel = channel or batch["channel"]
            sale_location = location or batch["location"]
            out_of_scope: list[str] = []
            for snap in batch["snapshot"]:
                terms = snap["terms"]
                if "*" not in terms["channels"] and sale_channel not in terms["channels"]:
                    out_of_scope.append(
                        f"{snap['content_title']} 的许可不允许渠道 {sale_channel}"
                    )
                if "*" not in terms["territories"] and sale_location not in terms["territories"]:
                    out_of_scope.append(
                        f"{snap['content_title']} 的许可不允许地域 {sale_location}"
                    )

            product = self._get("products", batch["product_id"])
            if "*" not in product["channels"] and sale_channel not in product["channels"]:
                out_of_scope.append(f"商品约定渠道不含 {sale_channel}")
            if "*" not in product["territories"] and sale_location not in product["territories"]:
                out_of_scope.append(f"商品约定地域不含 {sale_location}")

            during_suspension = product["status"] != PRODUCT_ACTIVE
            settlement = self._settle(batch, amount_cents)
            record = {
                "id": self._new_id("S"),
                "batch_id": batch_id,
                "sale_ref": sale_ref,
                "amount_cents": amount_cents,
                "currency": "CNY",
                "sold_on": sold_on or _today(self.clock),
                "channel": sale_channel,
                "location": sale_location,
                "reports": [
                    {"reporter": reporter, "reporter_type": reporter_type, "reported_at": self._now()}
                ],
                "duplicate_count": 0,
                "first_reporter": {"reporter": reporter, "reporter_type": reporter_type},
                "settled_at": self._now(),
                "settlement": settlement,
                "out_of_scope": out_of_scope,
                "during_suspension": during_suspension,
            }
            self.data["sales"][record["id"]] = record

            # 超出约定渠道：暂停新的生产；本笔销售如实保留并完成结算。
            if out_of_scope and product["status"] == PRODUCT_ACTIVE:
                product["status"] = PRODUCT_SUSPENDED
                product["events"].append(
                    {"at": self._now(), "type": "suspended",
                     "by": reporter, "note": f"销售 {record['id']} 超出约定渠道：{'；'.join(out_of_scope)}"}
                )
                record["auto_suspended_product"] = True

            self._save()
            view = dict(record)
            view["settled_once"] = True
            view["duplicate"] = False
            return view

    def sale(self, sale_id: str) -> dict:
        with self._lock:
            return self._sale_view(self._get("sales", sale_id))

    def _sale_view(self, record: dict) -> dict:
        view = dict(record)
        view["amount_yuan"] = cents_to_yuan(record["amount_cents"])
        view["settlement"] = dict(record["settlement"])
        view["settlement"]["lines"] = [dict(line) for line in record["settlement"]["lines"]]
        return view

    def list_sales(self, *, include_duplicates: bool = True) -> list[dict]:
        with self._lock:
            return [self._sale_view(r) for r in self.data["sales"].values()]

    def ledger(self) -> dict:
        """合作社内部：按收款方汇总已结算收益（重复上报不产生第二笔结算）。"""
        with self._lock:
            totals: dict[str, dict] = {}
            for sale in self.data["sales"].values():
                for line in sale["settlement"]["lines"]:
                    entry = totals.setdefault(
                        line["payee"],
                        {
                            "payee": line["payee"],
                            "kind": line["kind"],
                            "total_cents": 0,
                            "sales": [],
                        },
                    )
                    entry["total_cents"] += line["cents"]
                    entry["sales"].append(sale["id"])
            entries = []
            for entry in totals.values():
                entry["total_yuan"] = cents_to_yuan(entry["total_cents"])
                entry["sales"] = list(dict.fromkeys(entry["sales"]))
                entries.append(entry)
            entries.sort(key=lambda e: (e["kind"] != "contributor", e["payee"]))
            return {"currency": "CNY", "entries": entries}
