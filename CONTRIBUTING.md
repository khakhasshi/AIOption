# 贡献指南

感谢你参与 AIOption。该项目涉及高风险金融工作流，因此“行为可解释、失败可观察、变更可回滚”比功能数量更重要。

## 开发流程

1. 先创建 issue，说明用户场景、数据来源和预期行为。
2. 从最新主分支创建小范围分支。
3. 不提交密钥、数据库、订单记录、账户标识、下载数据或生产日志。
4. 为行为变化补测试；涉及前端时同时验证桌面和窄屏布局。
5. 运行 README 中的后端、前端、API 文档和仓库卫生检查。
6. PR 中写清风险、失败模式、验证证据和回滚方式。

## 交易与数据改动

- 下单、撤单、平仓、止损和多腿撮合改动必须覆盖部分成交、重试和幂等。
- 市场数据改动必须保留 provider、时间戳、新鲜度、调整方式和 fallback 来源。
- 代理数据、延迟数据或模型估算不得伪装成真实成交或供应商原始字段。
- 新功能必须保持新安装的交易能力默认关闭。

## 提交规范

建议使用简洁的 Conventional Commits，例如：

```text
feat(scanner): add quote freshness gate
fix(trading): preserve residual-leg monitoring
docs: clarify ThetaData subscription boundary
test: cover duplicate broker submission
```

提交代码即表示你同意按 Apache-2.0 许可证提供贡献。
