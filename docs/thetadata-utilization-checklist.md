# ThetaData 利用率测试清单 / 双数据源架构验证

> 架构原则：**ThetaData Standard 同时用于正股与期权行情**；yfinance / Longbridge 是故障回退或显式替代源。
> 凡涉及这两类数据的入口都提供 **两个数据源选单**（`market_data_source` = 正股源；`option_data_source` = 期权源）。
> ThetaData 当前档位**无原生实时 Greeks**，因此 Greeks 由 **BSM 模型基于 ThetaData IV 计算**（仅当 ThetaData 无 IV 时回落 yfinance IV）。

## 0. 档位能力基线（每个节点跑一次）

当前目标档位：股票与期权 **STANDARD**。部署节点必须通过 `account_capabilities()` 复核实际授权状态。

| 检查项 | 期望 | 命令 |
| --- | --- | --- |
| 期权到期列表 | OK | `option_list_expirations` |
| 期权快照报价 | OK | `option_snapshot_quote` |
| 期权 IV 快照 | OK | `option_snapshot_greeks_implied_volatility` |
| 期权原生全 Greeks | DENIED（预期） | `option_snapshot_greeks_all` |
| 正股快照 | OK | `stock_snapshot_quote` |
| 指数快照 | DENIED（预期） | `index_snapshot_price` |

> 结论：期权链可用、IV 可用 → BSM Greeks 可算；正股 quote、日线和分钟线优先 ThetaData，新闻仍不由 ThetaData 提供。

## 1. 分析器（Scanner）

- [ ] `option_data_source` 默认 `thetadata`；正股 `market_data_source` 可显式选择 `thetadata`。
- [ ] 选 `option_data_source=thetadata` 扫描：候选 `pricing_source` 含 thetadata，且每个候选有 IV。
- [ ] Greeks 来自 BSM（`enrich_option_greeks`）；ThetaData 有 IV 时**不**借用 yfinance IV（无 “uses yfinance implied volatility” 警告）。
- [ ] 选 `market_data_source=thetadata` 后，正股 K 线 / 分时来自 ThetaData；ThetaData 无新闻端点，新闻为空并保留来源说明。
- [ ] `payload.option_data_source` 与 `payload.market_data_source` 双双回写；前端「期权源 / 行情源」两枚徽标正确。
- [ ] 历史记录恢复后两个选单都回填正确值。

## 2. 机会雷达（Scan Loop / Radar）

- [ ] 实例表 `scan_loop_instances.option_data_source` 持久化（默认 thetadata）。
- [ ] 雷达 GEX 快照用 ThetaData 取期权链；未传 spot 时先用 ThetaData 正股快照，失败再回退。
- [ ] 雷达触发的子扫描把 `option_data_source` 透传给 `submit_scan`。
- [ ] 触发器（trigger）：`option_quote` 类型用 `option_data_source`（默认 thetadata）；`technical_indicator` / `underlying_price` 用 `market_data_source`，支持 ThetaData 正股行情。
- [ ] 雷达实例创建表单含「行情源（正股）」+「期权数据源」两个选单。

## 3. 实盘交易（Trading Monitor）

- [ ] 期权报价：`_monitor_source_order` 恒为 `thetadata → yfinance → longbridge`（期权恒先用 ThetaData）。
- [ ] 正股报价：`_monitor_underlying_source_order` 为 `thetadata → yfinance → longbridge`，选择不同源时对应源优先。
- [ ] 交易配置面板含「行情源」+「期权数据源」两个选单；`option_data_source` 默认 thetadata。

## 4. 端到端持久化回归

- [ ] `create_scan_run(option_data_source=...)` → `get_scan_run` 回读一致。
- [ ] `create_scan_loop_instance({option_data_source})` → `update_scan_loop_instance` 改值生效。
- [ ] 前端 `npm run build` 通过。

## 5. 执行验证脚本

```bash
# 本地（无 ThetaData 档位时只验证持久化与回落逻辑）
.venv/bin/python tests/verify_dual_data_source.py

# 私有部署节点（有相应 ThetaData 档位时验证真实期权链 + IV + BSM）
ssh your-server \
  'cd /opt/aioption && docker compose exec -T app python tests/verify_dual_data_source.py'
```

脚本退出码 `0` = 全部通过；非零 = 有断言失败（打印明细）。
