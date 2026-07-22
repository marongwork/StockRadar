# Mazhi Invest 技术手册

本文档面向开发和部署维护。产品概览见 [README](README.md)，详细设计见 [系统架构](docs/ARCHITECTURE.md) 与 [策略设计](docs/STRATEGY.md)。

## 核心入口

| 文件 | 用途 |
|---|---|
| `stock_picker.py` | 选股全流程、盘前复核、飞书推送 |
| `agent_council.py` | 多 Agent 加权投票和风险否决 |
| `market_intraday_monitor.py` | 指数冲高回落、翻绿、加速下跌检测 |
| `strategy_backtest.py` | 单策略回测入口 |
| `run_daily_backtest.py` | 每日多策略回测 |
| `virtual_trader.py` | 模拟账户执行与 T+1 约束 |
| `generate_stock_lab.py` | 公开实验室和详情页生成 |

## 常用命令

```bash
python3 stock_picker.py --print
python3 stock_picker.py
python3 stock_picker.py --premarket
python3 market_intraday_monitor.py
python3 run_daily_backtest.py
python3 generate_stock_lab.py .
python3 -m unittest test_trading_rules.py
```

## 配置

运行密钥只允许通过环境变量或服务器私有文件提供：

```bash
export IWENCAI_BASE_URL="https://openapi.iwencai.com"
export IWENCAI_API_KEY="your-key"
export FEISHU_WEBHOOK="your-webhook"
```

不要在代码、HTML、JSON 或 Git 历史中写入生产 Key、密码、Cookie、数据库和个人持仓。

## 页面生成

`generate_stock_lab.py` 从 SQLite 读取公开信号、市场快照、回测和模拟账户摘要，生成：

- `stock-lab.html`
- `stock-lab/{code}.html`
- `stock-lab/signals/{id}.html`
- `stock-lab/virtual/{trade}.html`

详情页使用内容指纹做增量生成。公共 CSS 和 JavaScript 位于 `assets/`，生产部署时需要确保站点根目录与 `/invest/assets/` 引用一致。

## 数据维护

- `backfill_agent_reviews.py`：只更新历史信号的 Agent 元数据，不改写当时评分和收益。
- `backfill_sector_flow.py`：补齐历史板块资金快照。
- `sync_stock_db.py`：按现有部署约定同步选股数据库。

回填前应备份数据库，但备份不得提交 Git。

## 验证清单

```bash
python3 -m py_compile \
  agent_council.py stock_picker.py strategy_backtest.py \
  virtual_trader.py market_intraday_monitor.py generate_stock_lab.py

python3 -m unittest test_trading_rules.py
node --check assets/stock-lab.js
```

页面验收使用桌面端宽度 1440px 以上，至少检查：

1. 大板块净流入和净流出双榜均有数据且排序正确。
2. 连板金字塔的渲染数量等于快照源数据数量。
3. Agent 投票、数据完整度、分歧和风险否决可见。
4. 回测周期、三大指数基准和成本参数一致。
5. 个股与信号详情链接可访问。

## 故障排查

### 页面仍是旧版本

检查生成时间、静态资源版本参数和 Nginx 实际读取路径。若 CSS/JS 在多个静态根目录存在副本，应同步更新并比较文件哈希。

### 板块出现 `NaN`

所有外部数值先转换为有限浮点数。无法解析的值应保存为 `NULL` 并在页面显示 `-`，不能参与排序。

### 飞书没有推送

依次检查定时任务、市场交易日判断、候选是否被执行闸门过滤、飞书凭据和发送日志。无可执行买点时仍应发送观察摘要，而不是静默失败。

### 回测数据重复

按策略、周期和参数保留最新验收结果；交易层按信号日、代码和策略去重。页面不能把不同生成时间但相同参数的结果当成三套策略。

## 安全发布

提交前必须执行：

```bash
git status --short
git ls-files | grep -Ei '(\.env|key|secret|token|\.db|\.log|\.bak)'
```

同时扫描 Key 形态和服务器密码。发现敏感文件已进入本地历史时，不要直接推送；应从远端干净基线建立脱敏快照。

