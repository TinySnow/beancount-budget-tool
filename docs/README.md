# 文档导航

日常使用只需下面两页：

| 文档 | 什么时候看 |
|---|---|
| [从零开始，逐月核对](../QUICKSTART.md) | 安装、迁移旧配置、修改账本、审核 7 月后继续 8 月、清理旧文件 |
| [两份配置的字段参考](CONFIGURATION.md) | 查账户、期初、默认科目、目标和每月覆盖能怎样设置 |

Windows 下载包也带有[启动说明](../packaging/WINDOWS-START.md)。可先运行[两文件虚拟例子](../examples/v6/simple/config.yml)，不必拿真实账本试配置。

## 开发与验收资料

下面这些不要求普通使用者逐篇阅读：

| 文档 | 用途 |
|---|---|
| [用户手写需求](REQUIREMENTS.md) | 作者维护的规则和验收例子，实现应与其核对 |
| [建议补充的操作条款](REQUIREMENTS-CLARIFICATIONS.md) | 供作者补入需求的建议，未改动手写需求 |
| [v6 架构](budget-v6-review.md) | 输入、计算、认证和拒绝规则 |
| [冻结判定](v6-freeze-decision.md) | 架构取舍与变更边界 |
| [实施状态](v6-implementation-status.md) | 当前能力、测试记录与限制 |

## 兼容与高级入口

[旧工作流](../LEGACY-WORKFLOWS.md)保留填写表、手工编译和项目登记命令；[高级月报输入](v6-monthly-report.md)解释 schema 6 侧文件。它们用于旧配置及复杂协议维护，不是两文件入口的日常步骤。

公开标签例子在 [labeling.bean](../examples/v6/labeling.bean)。真实账本和私人审阅决定不随仓库发布。
