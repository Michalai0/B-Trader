# B-Trader

在本机运行、通过 Telegram 控制的 Binance 现货与 USDⓈ-M 合约下单工具。它按预设止损金额计算仓位，在开仓成交后自动放置交易所侧保护单，并默认在 `1.5R` 止盈 `75%`，随后把剩余仓位止损推到实际开仓均价。

> 这是交易执行软件，不是投资建议。默认是 `paper` 模拟模式。请先完成模拟和 Binance Testnet 验证；实盘的滑点、手续费、资金费、强平、网络故障、交易所接口变更与止损限价穿透风险都可能让实际亏损超过输入金额。

## 交易逻辑

仓位不是“风险金额 × 杠杆”。本工具使用：

```text
数量 = 止损金额 / (开仓价 - 止损价的绝对值 + 可选手续费缓冲/单位)
名义价值 = 数量 × 开仓价
预计保证金 = 名义价值 / 杠杆
```

例如 BTC 参考开仓 61,000、止损 60,000、止损金额 100 USDT，则未取交易所步进前数量约为 `0.1 BTC`。10 倍杠杆改变保证金需求，不改变止损价格触发时约 100 USDT 的价格亏损。

成交后：

- 合约：用 Binance 新版 `/fapi/v1/algoOrder` 放置全仓 `STOP_MARKET`，同时放置 75% 的 `TAKE_PROFIT_MARKET`。止盈成交后先创建剩余数量的保本止损，再撤原止损。
- 现货：75% 数量放进交易所 OCO（止盈限价 + 原止损限价），剩余 25% 单独放原止损限价。止盈成交后用 cancel-replace 原子替换为开仓价止损。
- 限价开仓出现部分成交时：撤销未成交余量，并立即按实际成交量建立保护，不让部分仓位长期裸奔。
- 初始止损创建失败时：尝试市价紧急平仓，并向 Telegram 报警。
- 每个市场/交易对只允许一个由本工具管理的活动交易；合约拒绝叠加已有仓位或挂单。

## 安全设计

- Telegram `user_id` 与 `chat_id` 双白名单。
- 每笔交易先预览，再要求一次性 TOTP 动态码；同一时间片验证码不能重复使用。
- 确认单默认 120 秒过期，且只能由创建它的用户和会话使用。
- Telegram token、Binance API key/secret、TOTP secret 默认存入 macOS 钥匙串，不写入仓库或 SQLite。
- `.env` 已被 Git 忽略，日志不记录请求签名、密钥或动态码。
- `live` 模式有第二道本地开关：必须同时设置 `BTRADER_LIVE_TRADING_ENABLED=true`。
- 5xx/超时不会盲目重复下单；客户端订单号用于后续状态核对。

Binance API Key 建议：只开启现货交易和/或合约交易；**绝对不要开启提现权限**；在 Binance 后台绑定这台电脑的固定公网 IP；最好使用余额受限的独立子账户。Telegram Bot 只需出站 long polling，本机不必开放公网端口。

## 安装

要求 Python 3.9+。在仓库目录执行：

```bash
cd /Users/michaelwu/Documents/GitHub/B-Trader
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e '.[dev]'
cp .env.example .env
```

先在 Telegram 的 `@BotFather` 创建 Bot。第一次可暂时把 `.env` 里的两个白名单留空并启动 Bot；此时除了 `/whoami` 之外的所有命令都会被拒绝。向 Bot 发送 `/whoami`，取得 ID 后停止 Bot并将两个 ID 写入 `.env`：

```dotenv
BTRADER_ALLOWED_USER_IDS=你的数字UserID
BTRADER_ALLOWED_CHAT_IDS=你的数字ChatID
```

保存机密（终端不会回显输入）：

```bash
btrader secrets set telegram-token
btrader secrets set binance-spot-api-key
btrader secrets set binance-spot-api-secret
btrader secrets set binance-futures-api-key
btrader secrets set binance-futures-api-secret
btrader totp init
```

`btrader totp init` 会打印一个一次性的 `otpauth://` URI 和手动密钥；立即添加到 1Password、Google Authenticator、Authy 等验证器。关闭终端后不要把 URI 留在聊天或笔记中。

检查并启动：

```bash
btrader check
btrader run
```

`paper` 模式使用 Binance 实时公开价格，但不发送真实订单。`paper` 模拟订单保存在进程内，重启后请重新建立模拟交易。

## Telegram 命令

合约市价做多，10 倍杠杆，最多承受约 100 USDT 的价格止损亏损：

```text
/trade 合约 BTCUSDT 多 10x sl=60000 risk=100 entry=market
```

合约限价做空，自定义 2R、止盈 60%，止盈后不推保本：

```text
/trade 合约 ETHUSDT 空 5x sl=4200 risk=100 entry=4000 rr=2 tp=60 protect=no
```

现货限价买入：

```text
/trade 现货 BTCUSDT 买 sl=60000 risk=100 entry=61000
```

Bot 返回预览和确认单 ID 后：

```text
/confirm 确认单ID 六位动态码
```

其他命令：

```text
/status
/cancel 确认单ID或尚未完全成交的交易ID
/help
/whoami
```

## 从模拟切换到测试网和实盘

测试网：

```dotenv
BTRADER_TRADING_MODE=testnet
BTRADER_LIVE_TRADING_ENABLED=false
```

测试网 API Key 与实盘 API Key 不通用，而且现货测试网与合约测试网通常使用不同凭据，所以本工具支持分别保存两组 key/secret。实盘若同一组 Key 已同时获得现货与合约交易权限，也可只配置兼容的 `binance-api-key` 和 `binance-api-secret`。

实盘必须同时设置：

```dotenv
BTRADER_TRADING_MODE=live
BTRADER_LIVE_TRADING_ENABLED=true
```

实盘前至少验证：

1. `btrader check` 通过。
2. Binance 合约账户是单向持仓模式（One-way Mode）。
3. 交易对没有现有仓位、普通挂单或条件单。
4. `.env` 中的 `MAX_RISK`、`MAX_NOTIONAL`、`MAX_LEVERAGE` 是你愿意接受的硬上限。
5. 用最小仓位完整测试开仓、原止损、1.5R 止盈、保本止损和重启恢复。

## 配置

主要配置都在 `.env.example` 中：

- `BTRADER_MAX_RISK_USDT`：单笔允许的最大止损金额。
- `BTRADER_MAX_NOTIONAL_USDT`：单笔最大名义价值，防止极近止损算出巨大仓位。
- `BTRADER_MAX_LEVERAGE`：本地最大杠杆。
- `BTRADER_DEFAULT_RR` / `BTRADER_DEFAULT_TAKE_PROFIT_PERCENT`：默认 1.5R / 75%。
- `BTRADER_DEFAULT_PROTECT_BREAKEVEN`：是否默认止盈后推保本。
- `BTRADER_FEE_BUFFER_BPS`：把预估手续费缓冲计入仓位公式；默认 0。
- `BTRADER_STOP_LIMIT_BUFFER_BPS`：现货止损触发价与限价的距离；默认 10 bps。

## 已知边界

- 当前只支持 USDT/USDC 等线性 USDⓈ-M 合约，不支持币本位、期权、杠杆代币或杠杆现货。
- 合约仅支持单向持仓模式。工具不会替你切换整个账户的持仓模式。
- 现货“止损”是止损限价。快速跳空时可能触发但无法成交；这是交易所订单类型的固有限制。
- 保本价是实际成交均价，不含手续费与资金费，因此经济意义上的净保本可能仍略亏。
- 本工具使用轮询恢复和管理状态；初始保护单在交易所侧，但止盈后的止损上调需要本机进程在线。
- 不要手动修改由 `bt-` 客户端 ID 创建的订单。若人工干预仓位，立刻用 Binance 客户端核对全部保护单。

## 开发与测试

```bash
pytest -q
ruff check .
```

接口依据：[Binance Spot Trade API](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade) 与 [Binance USDⓈ-M Futures Trade API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade)。Binance 会调整接口，实盘部署前应再次核对变更日志。
