# 传统工艺社区许可（community-craft-license）

服务用于保存传统工艺与社区知识的许可边界、异议决定和收益分配，尊重不同内容的公开范围。
面向黑河新生鄂伦春族乡的场景：传承人小组为**纹样、技法、口述故事、药用知识**标记来源、
敏感级别、允许用途、地域、期限及必须署名的人；合作社据此为文旅/药旅商品发放批次标识，
并按发放时冻结的许可版本把收益分到贡献者与社区基金。

## 运行

```bash
python3 service.py --check                       # 配置自检
python3 service.py --init-db --db ccl.db         # 初始化数据库
python3 service.py --bootstrap --db ccl.db       # 空库自举合作社并输出首个令牌
python3 service.py --db ccl.db --port 8000       # 启动服务（默认 ccl.db / :8000）
curl http://127.0.0.1:8000/health

npm test                                         # 运行全部契约测试（21 项）
```

仅使用 Python 标准库，无第三方依赖；数据存于 SQLite。

## 角色与鉴权

| 角色 | 含义 | 主要权限 |
|---|---|---|
| `holder` | 传承人小组成员（含长者、家族成员） | 登记文化内容、发放/撤回许可、提异议、复核 |
| `coop` | 乡合作社 | 登记主体与令牌、商品组合、发放批次标识、暂停/关闭批次、查全量台账 |
| `scenic` / `merchant` | 景区 / 商户 | 上报销售、查询自己的销售与结算状态 |
| 匿名 | 公众 | 仅健康检查与批次标识公开核验 |

除 `/health` 与 `/v1/verify/<批次号>` 外，所有接口要求 `Authorization: Bearer <token>`。
首个合作社可在空库时无令牌自举（`POST /v1/parties`），之后主体登记与令牌签发均需合作社令牌。

## 核心业务规则

1. **许可版本化，不就地修改。** 首次发放为 v1；长者撤回、家族异议、复核决定都产生新版本，
   旧版本永久只读保留，全部决定写入事件流（`/v1/contents/<id>/events`）。
2. **异议/撤回先暂停新生产。** 某内容许可变为 `suspended`/`revoked`/到期时，所有包含该内容的
   在发批次自动暂停；**已发生的销售与结算不抹去**。复核恢复有效且组合内全部内容重新合规时，
   批次才恢复。
3. **组合许可“全部有效”才发标识。** 发放批次时逐项校验最新许可（状态、期限、用途、地域、渠道），
   任一无效返回 `422 license_blocked` 及逐项原因；通过后冻结许可版本快照，之后分账与署名均以快照为准。
4. **超渠道即暂停。** 商品销售安排变更（渠道/地域/用途）超出任一快照许可约定，或上报销售时
   实际渠道/地域不符，批次立即暂停、该笔销售不结算。
5. **同一笔销售只结算一次。** 上报以 `external_key`（景区与商户对同一笔销售共用的流水号）为
   幂等键：首次受理并在同一事务内结算，景区与商户重复上报只累加 `duplicate_count`，绝不二次结算。
6. **公开核验最小披露。** `GET /v1/verify/<批次号>` 只返回标识真伪、状态、商品名、必要署名；
   不返回敏感级别、家族含义、条款细节；`internal`/`restricted` 内容的标题与署名单不公开。
7. **收益按冻结版本分配且可解释。** 每笔结算单（settlement）含一段 `rationale` 与逐行 `rule`，
   说明销售额多少比例进入文化内容收益、社区基金比例、各贡献者比例及其所依据的许可版本；
   传承人只能看到与自己相关的分账行，合作社可见全量台账。

敏感级别：`public`（可公开）、`community`（社区内）、`internal`（族内传承）、`restricted`（受限，如药用知识核心细节）。

## API 一览

```
POST   /v1/parties                         登记主体（空库可自举，其余需 coop）
POST   /v1/parties/<id>/tokens             签发 API 令牌（coop）

POST   /v1/contents                         登记文化内容（holder）
GET    /v1/contents/<id>                   查询（family 字段仅 holder/coop 可见）
PATCH  /v1/contents/<id>                   更新说明/家族标记/署名单（保管人）
POST   /v1/contents/<id>/licenses          首次发放许可 v1（holder）
GET    /v1/contents/<id>/licenses          全部许可版本
POST   /v1/contents/<id>/revoke            长者撤回（新版本 revoked + 暂停批次）
POST   /v1/contents/<id>/disputes          家族成员异议（新版本 suspended + 暂停批次）
POST   /v1/contents/<id>/reviews           复核决定（active/suspended/revoked + 条款变更）
GET    /v1/contents/<id>/events            生命周期事件流

POST   /v1/products                         登记商品组合（coop；items 分成基点合计 10000）
GET    /v1/products/<id>
PATCH  /v1/products/<id>                   变更渠道/地域/组合（可能触发批次暂停）
POST   /v1/products/<id>/batches           发放批次标识（全部许可有效才通过）

GET    /v1/batches/<id>                    批次详情（含版本快照与事件）
POST   /v1/batches/<id>/pause              合作社手工暂停（如巡查发现超渠道）
POST   /v1/batches/<id>/close              关闭批次（历史销售保留）
GET    /v1/verify/<批次号>                  公开核验（匿名、最小披露）

POST   /v1/sales                            景区/商户上报销售（external_key 幂等）
GET    /v1/sales[?batch_id=]               销售流水（上报方只见自己的；coop 全量）

GET    /v1/settlements[?batch_id=&payee=]  结算台账（holder 只见自己相关行）
GET    /v1/settlements/<id>                单笔结算（含 rationale 与逐行 rule）
```

### 许可条款示例

```json
{
  "sensitivity": "community",
  "purposes": ["cultural_tourism"],
  "territories": ["新生乡", "黑河景区"],
  "channels": ["景区门店", "合作社电商"],
  "valid_until": "2027-12-31T23:59:59+00:00",
  "required_credit": "孟古古伦家族",
  "basis": "传承人小组2026年秋例会决定"
}
```

复核（`reviews`）未显式提供的条款沿用上一版本；`valid_until` 留空表示长期有效。

### 分配规则示例（100 元销售，`content_bps=8000`、`community_bps=1000`）

- 80 元为文化内容收益（销售额的 80%，发放批次时冻结）；
- 其中 10%（8 元）入社区基金；
- 其余 72 元按商品组合内各项 `share_bps` 比例分给登记贡献者；
- 最大余数法处理取整，余数并入社区基金，分账总额无丢失。

## 代码结构

| 文件 | 职责 |
|---|---|
| `db.py` | SQLite schema 与连接/JSON 辅助（内容、许可版本、事件、批次快照、销售去重、结算台账、令牌） |
| `core.py` | 领域规则：版本化、暂停联动、组合校验、幂等结算、分配与解释、公开核验脱敏 |
| `web.py` | HTTP 路由、Bearer 鉴权、角色授权、错误映射 |
| `service.py` | 入口：`--check` / `--init-db` / `--bootstrap` / `--port` |
| `service_contract.py` | 21 项端到端契约测试与单元测试 |
