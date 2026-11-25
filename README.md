# README

# 四大行黄金价格监控 · 部署说明

盯盘对象：**工商银行 / 建设银行 / 中国银行 / 农业银行** 投资金条价 + 上金所基准(Au99.99)
预警档位：**800 / 750 / 700 / 650 / 600 / 550 / 500 元/克**（四大行最低价跌破任意一档即预警）
每日播报：每个工作日 **09:38**（北京时间）推送一遍当前金价

> ⚠️ 重要前提：本脚本需要跑在一台**常开机的主机 / 云服务器 / 树莓派**上才能 7×24 盯盘并主动推送。
> 你现在的对话环境（沙箱）无法常驻、也无法直接向你的手机发消息。把下面脚本部署到你的机器上，
> 再接一个推送通道（Bark / 企业微信·Server酱 / Telegram / 邮件），就能实现"跌破即叫你 + 早晚播报"。

---

## 一、文件

- `gold_monitor.py` —— 监控主程序（纯标准库，无需 pip 安装）
- `gold_alert_state.json` —— 运行后自动生成，记录各档位是否已跌破（防刷屏用，别删）

## 二、两种跑法

### 方案 A：cron 定时（推荐，最稳）

```bash
# 每 5 分钟检查阈值（跌破才推送，平时静默）
*/5 * * * *  /usr/bin/python3 /workspace/gold_monitor.py --mode check

# 每个工作日 09:38 播报一遍（本地时区为北京时间）
38 9 * * 1-5  /usr/bin/python3 /workspace/gold_monitor.py --mode report
```

### 方案 B：常驻进程（不想配 cron 时）

```bash
# 用 systemd 或 nohup 跑一个循环进程，每 300 秒检查一次
nohup /usr/bin/python3 /workspace/gold_monitor.py --mode watch --interval 300 > gold_monitor.log 2>&1 &
```

## 三、接推送通道（选一个，设好环境变量即可）

脚本默认只在**控制台/日志**输出。要"叫你"，在运行环境里配置以下变量之一：

| 通道 | 需要的环境变量 | 说明 |
| --- | --- | --- |
| **Bark**（iOS 推送，最简单） | `BARK_KEY` | App Store 装 Bark，复制设备钥匙；可选 `BARK_SERVER` 自建服务地址 |
| **企业微信 / Server酱** | `WEBHOOK_URL` | 企业微信机器人直接填 Webhook 地址；Server酱填 `https://sctapi.ftqq.com/<你的KEY>.send`，脚本自动识别走 GET 格式 |
| **Telegram** | `TG_TOKEN`、`TG_CHAT_ID` | @BotFather 建 bot 拿 token，发给 @userinfobot 拿 chat\_id |
| **邮件** | `SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASS`、`EMAIL_TO` | 例如 QQ 邮箱 SMTP\_HOST=smtp.qq.com PORT=465 |

示例（Bark）：

```bash
export BARK_KEY="你的bark钥匙"
/usr/bin/python3 /workspace/gold_monitor.py --mode report
```

## 四、可调参数（环境变量）

- `GM_MONITOR`：`min`（默认，四大行最低价触发）或 `sge`（改用上金所基准价触发）
- `GM_RECOVER_NOTIFY=1`：金价回升到档位之上时也发一条"已回升"提醒（默认关闭，避免打扰）
- `GM_SOURCE_URL`：数据源地址（页面结构变了再改这里）
- `GM_STATE_FILE`：状态文件路径
- `GM_FAIL_ALERT_N`：数据源连续抓取失败多少次推告警，默认 `3`（约 15 分钟，只推一次不刷屏）
- `GM_FAIL_RECOVER_NOTIFY`：恢复时是否推一条"已恢复"通知，默认 `1`（设 `0` 关闭）

## 五、调试

```bash
# 只打印当前行情，不发推送、不写状态
python3 gold_monitor.py --mode report --dry-run
python3 gold_monitor.py --mode check  --dry-run
```

## 六、数据源说明

当前主源为 `rijinjia.com/goldbar`（投资金条页，静态渲染，含四大行金条价 + 上金所基准，2026-11-25 验证可稳定解析）。
> 历史：原主源 `rijinjia.com/gold/936.html` 于 2026-11 下旬改版为「按克重估值」计算器，四大行 `data-pa` 数据被移除，导致 11-23 起解析失败、播报中断；现已迁至 `/goldbar`。
若日后页面改版解析失败，脚本会打印 `[抓取异常]` 并跳过本轮（**不会误报**），届时按 `GM_SOURCE_URL` 换源或微调 `parse_prices` 即可。

> **连续失败自动告警**：数据源连续失败达到 `GM_FAIL_ALERT_N`（默认 3）次时，会主动推一条「金价监控数据源异常」告警（含数据源地址、错误原因、处理建议），只推一次不刷屏；期间恢复则再推一条「已恢复」通知，形成闭环。计数存在 state 文件里，靠 GitHub Actions 的 cache 跨运行持久化，check 与 report 两个 workflow 共享同一份计数。

> 注：监控的是"投资金条买入价"（价格随行情波动，请以防真价为准）。
> 若你想盯"黄金积存/纸黄金"买入价（通常比金条低约 10–20 元/克），把 `GM_MONITOR=sge` 即可更贴近盘中实时价。
