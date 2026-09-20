# 传统工艺社区许可

服务用于保存传统工艺与社区知识的许可边界、异议决定和收益分配，尊重不同内容的公开范围。面向黑河新生鄂伦春族乡的文旅 / 药旅商品，由传承人小组为**纹样、技法、口述故事、药用知识**标记来源、敏感级别、允许用途、地域、期限与必须署名的人。

## 业务规则

* **许可门控**：一个商品组合使用多项内容时，组合内每一项许可都处于有效状态（未暂停/未到期/用途、地域、渠道相符）才发放批次标识（`HRXS-…`）。批次签发时锁定当时各许可的版本快照。
* **撤回与异议**：长者撤回许可、家族成员提出异议时，相关许可立即暂停，新批次一律停发；已签发批次与已发生销售**不抹去**。复核有三种结果：恢复（restore）、按新条款形成新版本（revise，旧版标记 superseded 并保留历史）、维持停止（uphold）。
* **渠道违约**：商品被发现超出约定渠道，或销售上报落在约定渠道 / 地域之外时，暂停新的生产；该笔销售如实保留并完成结算。商品复核同样支持恢复、修订（商品版本号 +1）、停产。
* **销售去重**：同一次销售由景区与商户分别上报时，以 `(批次, 销售流水号 sale_ref)` 去重，仅首次上报结算一次；重复上报会被记录（`duplicate_count`），同一上报方重复上报直接拒绝。
* **分账可解释**：收入先按组合项权重切给各项内容，再按批次快照中该许可版本的贡献者份额分给传承人，其余归入社区基金；整数分精确配平。每笔分账行都记录内容、许可与版本，销售记录附带 `rule_basis` 说明为何采用这组规则。许可事后修订不影响已结算批次。
* **公开范围**：公开查询（`/v/...`，无需令牌）只验证标识与必要说明——公开内容给出名称与公开简介，社区 / 族内内容只确认“该类知识已获社区许可”，不披露名称与细节，但必须署名的人始终随标识展示。全部明细仅合作社内部接口（`/api/...`，需令牌）可见。

## 运行

```bash
python3 service.py --check                      # 配置自检
COOP_API_TOKEN=合作社内部令牌 \
python3 service.py --port 8000 --store state.json
# 令牌也可用 --internal-token 传入；不提供 --store 时仅内存运行
```

仅依赖 Python 标准库。健康检查：`GET /health`。

## 接口一览（内部接口需 `Authorization: Bearer <令牌>`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/contents` | 登记内容（kind: pattern/technique/story/medicine；sensitivity: public/community/restricted） |
| GET | `/api/contents`、`/api/contents/{id}` | 内容明细（内部，含来源与家族） |
| POST | `/api/contents/{id}/licenses` | 发放许可（scope、territories、channels、expires_on、attribution、shares） |
| GET | `/api/licenses`、`/api/licenses/{id}` | 许可与全部版本 |
| POST | `/api/licenses/{id}/withdrawal` | 长者撤回，进入暂停待复核 |
| POST | `/api/contents/{id}/objections` | 家族成员等提出异议，暂停该内容全部有效许可 |
| POST | `/api/objections/{id}/review` | 复核：restore / revise（new_terms）/ uphold |
| POST | `/api/products` | 登记商品组合（items 含 content_id、可选 license_id 与 weight） |
| POST | `/api/products/{id}/breach` | 报告超出约定渠道，暂停新生产 |
| POST | `/api/products/{id}/review` | 商品复核：restore / revise（changes）/ discontinue |
| POST | `/api/products/{id}/batches` | 门控通过后签发批次标识 |
| POST | `/api/batches/{id}/sales` | 上报销售（景区 / 商户，含 sale_ref 去重） |
| GET | `/api/sales`、`/api/sales/{id}` | 销售与逐笔分账（含 rule_basis） |
| GET | `/api/ledger` | 按收款方（贡献者、社区基金）汇总台账 |
| GET | `/v/batches/{id}` | **公开**：验证批次标识与必要署名说明 |
| GET | `/v/contents/{id}` | **公开**：仅公开内容的简介 |

## 测试

```bash
npm test        # 运行 service_contract / test_licensing / test_http_api 共 31 个用例
```

覆盖：缺许可 / 到期 / 渠道地域不符的批次拦截，撤回—复核—新版本全流程，渠道违约暂停而保留销售，景区与商户重复上报只结算一次，组合权重分账与取整配平，公开接口对族内知识脱敏，以及 JSON 文件重启持久化。
