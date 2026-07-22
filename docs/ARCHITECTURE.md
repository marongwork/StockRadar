# 系统架构

## 设计目标

系统将数据采集、研究决策、交易执行、效果验证和公开展示分离，避免页面生成逻辑直接决定选股结果，也避免回测修改历史信号。

## 分层结构

```mermaid
flowchart TB
    subgraph L1[采集层]
        A1[问财行业 / 概念 / 个股]
        A2[公共指数与个股行情]
        A3[公告 / 新闻 / 研报]
    end
    subgraph L2[领域层]
        B1[候选池生成]
        B2[多 Agent 决策]
        B3[风险排雷]
        B4[市场异动监控]
    end
    subgraph L3[数据层]
        C1[(stock_picks)]
        C2[(market_fund_snapshots)]
        C3[(backtest_runs / trades)]
        C4[(virtual_positions / trades)]
    end
    subgraph L4[应用层]
        D1[策略回测]
        D2[模拟交易]
        D3[静态页面生成]
        D4[飞书通知]
    end
    subgraph L5[交付层]
        E1[Nginx 静态站点]
        E2[个股 / 信号详情]
        E3[资金 / 回测工作台]
    end
    L1 --> L2 --> L3 --> L4 --> L5
```

## 关键数据流

```mermaid
sequenceDiagram
    participant Cron as 定时任务
    participant Picker as stock_picker.py
    participant Source as 数据源
    participant Council as agent_council.py
    participant DB as SQLite
    participant Backtest as strategy_backtest.py
    participant Generator as generate_stock_lab.py
    participant Web as Nginx
    participant Feishu as 飞书

    Cron->>Picker: 盘前 / 盘中 / 盘后扫描
    Picker->>Source: 候选池与补全查询
    Source-->>Picker: 行情、资金、事件、板块
    Picker->>Council: 候选证据
    Council-->>Picker: 加权投票、分歧、风险否决
    Picker->>DB: 保存带分钟时间戳的信号
    Picker->>Feishu: 推送可执行与观察信号
    Cron->>Backtest: 每日验收
    Backtest->>DB: 读取历史信号并写入结果
    Cron->>Generator: 增量生成
    Generator->>DB: 读取公开数据
    Generator->>Web: HTML / CSS / JS / 详情页
```

## 模块职责

| 模块 | 职责 | 不负责 |
|---|---|---|
| `stock_picker.py` | 发现候选、补全数据、生成交易计划 | 修改历史回测结果 |
| `agent_council.py` | 独立观点、加权共识、风险否决 | 调用外部 API |
| `strategy_backtest.py` | 真实规则回放与基准比较 | 重新打分历史信号 |
| `virtual_trader.py` | 模拟账户、T+1、仓位和交易日志 | 用未来数据成交 |
| `generate_stock_lab.py` | 公开数据展示和增量详情生成 | 决定候选是否买入 |
| `market_intraday_monitor.py` | 指数反转与风险提醒 | 高频逐笔交易 |

## 存储边界

SQLite 是当前单机部署的权威数据源。主要表：

- `stock_picks`：信号、批次、时间、评分、交易区间、Agent 元数据。
- `market_fund_snapshots`：指数、板块、涨停、龙虎榜和情绪快照。
- `backtest_runs` / `backtest_trades`：策略级与交易级回测结果。
- `virtual_positions` / `virtual_trades`：模拟持仓与真实交易规则日志。
- `market_intraday_samples` / `market_intraday_alerts`：盘中指数样本与异动。

数据库、密钥和用户数据属于运行态资产，不应进入 Git。

## 部署拓扑

```mermaid
flowchart LR
    Timer[systemd timer / cron] --> Jobs[Python Jobs]
    Jobs --> DB[(SQLite)]
    Jobs --> Static[Generated Static Files]
    Static --> Nginx[Nginx HTTPS]
    Nginx --> Browser[Desktop Browser]
    Jobs --> Feishu[Feishu]
```

静态优先的部署方式降低了公开页面的运行时依赖；选股和回测失败不会让已生成页面立即不可用。

