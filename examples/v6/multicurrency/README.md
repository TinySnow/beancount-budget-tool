# 合成多币种收支示例

所有账户和金额都是合成示例。运行：

```sh
beancount-budget-tool --config examples/v6/multicurrency/config.yml --format text
beancount-budget-tool report --config examples/v6/multicurrency/config.yml --from 2025-01 --through 2025-02 --format csv
```

1 月支出 550 JPY，收入 25 USD；2 月支出 5 CNY。原币完整时签发 `CERTIFIED_NATIVE_SCOPED`，报表按币种分别累计，不要求汇率，也不生成跨币种总额。外币预算和剩余额度未设置，显示为空或 `-`。

人民币消费目标与最多能花只包含原生人民币消费。550 JPY 不扣减人民币目标，25 USD 不加入人民币收入或实际可花。本例 1 月人民币目标剩余 100 CNY，实际可花 1000 CNY；2 月目标剩余 95 CNY，实际可花 995 CNY。

月报、区间、薪资周期、TUI、JSON 和 CSV 使用相同原币数据。缺少分类、用途或责任凭据仍须待审；原币报告不会替代资产用途审核或外债清偿协议。
