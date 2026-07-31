# Gold Insight

黄金走势预测平台：同时覆盖 **伦敦金** 与 **浙商积存金**，提供实时行情、技术指标、统计预测与品种对比。

> 仅供研究学习，预测结果不构成投资建议。

## 产品定位

面向关注国际金价与银行积存金的个人投资者，解决「双品种报价分散、趋势难读、缺少可解释预测」的问题。

| 能力 | 说明 |
|------|------|
| 双品种行情 | 伦敦金现货（XAU/USD）+ 浙商积存金（京东金融 SKU） |
| 技术面 | MA / MACD / RSI / 布林带 / ATR，并给出短线倾向摘要 |
| 走势预测 | Gradient Boosting 滚动预测 1–30 日，含置信区间与特征重要性 |
| 品种对比 | 归一化相对走势与相关性 |
| 本地落库 | MySQL 缓存快照与日线，定时刷新行情 |

## 快速开始

本项目使用 [uv](https://docs.astral.sh/uv/) 管理依赖。

```bash
cd gold-insight
uv sync
# 确保本机 MySQL 已启动（默认库名 gold_insight，账号见 .env）
uv run python run.py
```

若 `uv run gold-insight` 报「应用程序控制策略已阻止此文件」(os error 4551)，用上面的 `uv run python run.py` 即可（被拦的是 venv 里自动生成的 `.exe` 入口）。

浏览器打开：http://127.0.0.1:8765

默认数据库连接（可在 `.env` 修改）：

```
mysql+pymysql://root:123456@localhost:3306/gold_insight
```

首次启动会自动创建数据库 `gold_insight` 及表结构。

## API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/quotes` | 实时/缓存行情 |
| GET | `/api/history/{symbol}` | 历史 + 指标，`LONDON_GOLD` / `ZHESHANG_GOLD` |
| GET | `/api/predict/{symbol}?horizon=7` | 预测 |
| GET | `/api/compare` | 双品种对比 |
| POST | `/api/sync/{symbol}` | 强制同步历史 |

## 数据说明

- **伦敦金**：实时用现货源（goldprice.dev / gold-api.com，约 XAU/USD）；历史日线辅助用 Yahoo `GC=F`，当日会被现货价校准。
- **浙商积存金**：京东金融 `stdLatestPrice`（SKU `1961543816`），单位元/克。
- **积存金长历史**：优先东财「黄金9999」日线并按京东现价锚定；失败则用「伦敦金 × 逐日美元兑人民币」估算；有采集快照的交易日用真实 OHLC 覆盖。

## 目录结构

```
gold-insight/
├── app/
│   ├── api.py              # REST API
│   ├── main.py             # FastAPI 入口
│   ├── db.py               # SQLite 模型
│   ├── services/           # 行情 / 指标 / 预测
│   ├── static/             # 前端资源
│   └── templates/          # 页面
├── data/                   # 可选本地缓存目录
├── pyproject.toml
├── uv.lock          # uv sync 后生成
├── .env             # 数据库等本地配置（勿提交）
└── run.py
```

## 后续可扩展

- 接入更多银行积存金 SKU
- 加入利率/美元指数等宏观特征
- 预测回测看板与准确率跟踪
- 价格提醒 / 企业微信推送
