#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
四大行黄金价格监控 · 阈值预警 · 每日播报
============================================================
数据源 : 日金价(rijinjia.com/goldbar) 投资金条页
         含 工商银行 / 建设银行 / 中国银行 / 农业银行 投资金条价 + 上金所基准(Au99.99)
阈值   : 800 / 750 / 700 / 650 / 600 / 550 / 500 元/克
         （四大行最低价跌破任意一档即预警，边沿触发，不会每次轮询刷屏）
通知   : 控制台/日志（默认） + 可选 Bark / 企业微信·Server酱 Webhook / Telegram / 邮件

用法
----
  # 单次检查 + 阈值预警（建议用 cron 每 5 分钟跑一次）
  python3 gold_monitor.py --mode check

  # 每日金价播报（建议用 cron 在 10:00 与 20:00 各跑一次）
  python3 gold_monitor.py --mode report

  # 常驻循环模式（不想配 cron 时，用 systemd/nohup 跑一个进程）
  python3 gold_monitor.py --mode watch --interval 300

  # 仅打印当前行情，不发送、不写状态（调试用）
  python3 gold_monitor.py --mode check --dry-run

配置：直接改下方 CONFIG，或用同名环境变量覆盖（见 NOTIFIER 区）。
============================================================
"""
import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

# ============================ CONFIG ============================
# 预警阈值（元/克），从高到低。跌破任意一档触发。
THRESHOLDS = [800, 750, 700, 650, 600, 550, 500]

# 触发基准价：'min' = 四大行最低价；'sge' = 上金所基准价
MONITOR = os.getenv("GM_MONITOR", "min")

# 数据源 URL（四大行金条 + 上金所基准，静态渲染）
SOURCE_URL = os.getenv(
    "GM_SOURCE_URL",
    "https://www.rijinjia.com/goldbar",
)

# 状态文件：记录各阈值当前是否已跌破，用于边沿触发（防刷屏）
STATE_FILE = os.getenv("GM_STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_alert_state.json"))

# 价格抓取失败时的重试次数与间隔(秒)
FETCH_RETRIES = 3
FETCH_RETRY_WAIT = 10

# 跌破后价格回升到阈值之上时，是否也发一条“已回升”提醒
RECOVER_NOTIFY = os.getenv("GM_RECOVER_NOTIFY", "0") in ("1", "true", "True")

# 四行在页面 data-pa 中的属性名 -> 中文名
BANK_FIELDS = {
    "icbc-gold": "工商银行",
    "ccb-gold":  "建设银行",
    "boc-gold":  "中国银行",
    "abc-gold":  "农业银行",
}
SGE_FIELD = "sge-gold"   # 上金所基准
SGE_NAME = "上金所基准"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ============================ 抓取 ============================
def fetch_html(url, retries=FETCH_RETRIES):
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
            # 优先按声明编码解码，失败回退 utf-8
            enc = r.headers.get_content_charset() or "utf-8"
            try:
                return raw.decode(enc, errors="ignore")
            except LookupError:
                return raw.decode("utf-8", errors="ignore")
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i < retries - 1:
                time.sleep(FETCH_RETRY_WAIT)
    raise RuntimeError(f"抓取失败(重试{retries}次): {last_err}")


def parse_prices(html_text):
    """从页面抽取四大行金条价 + 上金所基准。返回 {名称: float}。"""
    prices = {}
    for field, name in BANK_FIELDS.items():
        m = re.search(r'data-pa="%s"[^>]*>([0-9]{3,4}\.[0-9]{2})' % re.escape(field), html_text)
        if m:
            prices[name] = float(m.group(1))
    m = re.search(r'data-pa="%s"[^>]*>([0-9]{3,4}\.[0-9]{2})' % re.escape(SGE_FIELD), html_text)
    if m:
        prices[SGE_NAME] = float(m.group(1))
    return prices


def get_prices():
    html_text = fetch_html(SOURCE_URL)
    prices = parse_prices(html_text)
    if not prices:
        raise RuntimeError("页面解析未拿到任何价格，可能页面结构已变更")
    return prices


def monitor_price(prices):
    """按 MONITOR 配置返回用于阈值判断的基准价。"""
    if MONITOR == "sge":
        return prices.get(SGE_NAME)
    # 默认：四大行最低价
    bank_vals = [v for k, v in prices.items() if k in BANK_FIELDS.values()]
    return min(bank_vals) if bank_vals else None


# ============================ 状态/阈值 ============================
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def check_thresholds(price, state):
    """边沿触发：返回 [(事件类型, 阈值), ...]。事件类型 'breach'=跌破, 'recover'=回升。"""
    events = []
    for thr in THRESHOLDS:
        below = price <= thr
        key = str(thr)
        prev = state.get(key, False)
        if below and not prev:
            events.append(("breach", thr))
        elif (not below) and prev and RECOVER_NOTIFY:
            events.append(("recover", thr))
        state[key] = below
    return events


# ============================ 文案 ============================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_report(prices):
    title = "📊 每日金价播报"
    lines = [f"时间：{now_str()}（北京时间）", "—" * 18]
    for name in list(BANK_FIELDS.values()) + [SGE_NAME]:
        if name in prices:
            lines.append(f"{name}：{prices[name]:.2f} 元/克")
    mp = monitor_price(prices)
    if mp is not None:
        lines.append("—" * 18)
        lines.append(f"四大行最低价：{mp:.2f} 元/克  ← 预警基准")
    breached = [t for t in THRESHOLDS if mp is not None and mp <= t]
    if breached:
        lines.append("⚠️ 当前已跌破档位：" + "、".join(f"{t}" for t in breached) + " 元/克")
    else:
        lines.append("✅ 当前未跌破任何预警档位")
    return title, "\n".join(lines)


def format_alert(events, prices):
    title = "🚨 四大行金价跌破预警"
    lines = [f"时间：{now_str()}（北京时间）", "—" * 18]
    mp = monitor_price(prices)
    lines.append(f"触发基准价（四大行最低）：{mp:.2f} 元/克" if mp is not None else "触发基准价：获取失败")
    for etype, thr in events:
        if etype == "breach":
            lines.append(f"🔻 金价已跌破【{thr}】元/克！")
        else:
            lines.append(f"🔺 金价已回升至【{thr}】元/克以上")
    lines.append("—" * 18)
    for name in BANK_FIELDS.values():
        if name in prices:
            lines.append(f"{name}：{prices[name]:.2f} 元/克")
    if SGE_NAME in prices:
        lines.append(f"{SGE_NAME}：{prices[SGE_NAME]:.2f} 元/克")
    return title, "\n".join(lines)


# ============================ 通知 ============================
class Notifier:
    def send(self, title, body):
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    def send(self, title, body):
        print(f"\n[{title}]\n{body}\n")


class BarkNotifier(Notifier):
    """iOS 推送，需先在 App Store 装 Bark 并复制钥匙。环境变量 BARK_KEY。"""
    def __init__(self):
        self.key = os.getenv("BARK_KEY")
        # GitHub Actions 会把未配置的 Secret 注入为空字符串，需显式回退默认值
        self.server = (os.getenv("BARK_SERVER") or "https://api.day.app").rstrip("/")

    def send(self, title, body):
        if not self.key:
            return
        payload = json.dumps({"title": title, "body": body, "device_key": self.key,
                              "level": "timeSensitive", "sound": "alarm"}).encode("utf-8")
        req = urllib.request.Request(self.server + "/push", data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)


class WebhookNotifier(Notifier):
    """企业微信机器人(POST JSON) 或 Server酱(GET title/desp)。
    环境变量 WEBHOOK_URL：
      - 企业微信：https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
      - Server酱：https://sctapi.ftqq.com/<你的KEY>.send
    """
    def __init__(self):
        self.url = os.getenv("WEBHOOK_URL")

    def send(self, title, body):
        if not self.url:
            return
        if "ftqq.com" in self.url:
            # Server酱：GET，desp 支持换行(\n)与简单 markdown
            q = urllib.parse.urlencode({"title": title, "desp": body})
            urllib.request.urlopen(self.url + "?" + q, timeout=15)
        else:
            # 企业微信机器人：POST JSON（text 类型）
            payload = json.dumps({"msgtype": "text",
                                  "text": {"content": f"{title}\n{body}"}}).encode("utf-8")
            req = urllib.request.Request(self.url, data=payload,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15)


class TelegramNotifier(Notifier):
    """Telegram Bot。环境变量 TG_TOKEN、TG_CHAT_ID。"""
    def __init__(self):
        self.token = os.getenv("TG_TOKEN")
        self.chat_id = os.getenv("TG_CHAT_ID")

    def send(self, title, body):
        if not (self.token and self.chat_id):
            return
        text = f"{title}\n{body}"
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        q = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text})
        urllib.request.urlopen(url + "?" + q, timeout=15)


class EmailNotifier(Notifier):
    """邮件。环境变量 SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/EMAIL_TO。"""
    def __init__(self):
        self.host = os.getenv("SMTP_HOST")
        self.port = int(os.getenv("SMTP_PORT", "465"))
        self.user = os.getenv("SMTP_USER")
        self.pw = os.getenv("SMTP_PASS")
        self.to = os.getenv("EMAIL_TO")

    def send(self, title, body):
        if not (self.host and self.user and self.to):
            return
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = self.user
        msg["To"] = self.to
        msg.set_content(body)
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(self.host, self.port, context=ctx) as s:
            s.login(self.user, self.pw)
            s.send_message(msg)


def build_notifiers():
    notifiers = [ConsoleNotifier()]
    for cls in (BarkNotifier, WebhookNotifier, TelegramNotifier, EmailNotifier):
        n = cls()
        # 只有真正配置了的才加入（用简单探测：实例化后是否有有效凭据）
        # 这里统一加入，各 send 内部会自行判断凭据是否存在
        notifiers.append(n)
    return notifiers


def notify(notifiers, title, body):
    for n in notifiers:
        try:
            n.send(title, body)
        except Exception as e:  # noqa: BLE001
            print(f"[通知失败 {type(n).__name__}] {e}", file=sys.stderr)


# ============================ 主流程 ============================
def run_check(dry_run=False):
    try:
        prices = get_prices()
    except Exception as e:  # noqa: BLE001
        print(f"[抓取异常] {e}", file=sys.stderr)
        return
    mp = monitor_price(prices)
    state = load_state()
    events = check_thresholds(mp, state) if mp is not None else []
    if not dry_run:
        save_state(state)
    if events:
        title, body = format_alert(events, prices)
        if dry_run:
            print(f"[dry-run] 将触发预警：\n{body}\n")
        else:
            notify(build_notifiers(), title, body)
    else:
        # 无事件也打印一行，便于日志确认运行正常
        print(f"[{now_str()}] 检查完成，基准价 {mp:.2f} 元/克，无新预警。")


def run_report(dry_run=False):
    try:
        prices = get_prices()
    except Exception as e:  # noqa: BLE001
        print(f"[抓取异常] {e}", file=sys.stderr)
        return
    title, body = format_report(prices)
    if dry_run:
        print(f"[dry-run] 每日播报：\n{body}\n")
    else:
        notify(build_notifiers(), title, body)


def run_watch(interval):
    print(f"常驻监控已启动，每 {interval} 秒检查一次（Ctrl+C 退出）")
    while True:
        run_check()
        time.sleep(interval)


def main_handler(event=None, context=None):
    """腾讯云 SCF / 阿里云 FC 等 Serverless 入口。
    通过 event={"mode":"check|report"} 或环境变量 GM_MODE 指定模式。"""
    mode = (event or {}).get("mode") or os.getenv("GM_MODE", "check")
    if mode == "report":
        run_report()
    else:
        run_check()
    return {"status": "ok"}


def main():
    p = argparse.ArgumentParser(description="四大行黄金价格监控")
    p.add_argument("--mode", choices=["check", "report", "watch"], default="check")
    p.add_argument("--interval", type=int, default=300, help="watch 模式轮询间隔(秒)")
    p.add_argument("--dry-run", action="store_true", help="只打印不发送、不写状态")
    args = p.parse_args()

    if args.mode == "check":
        run_check(args.dry_run)
    elif args.mode == "report":
        run_report(args.dry_run)
    elif args.mode == "watch":
        run_watch(args.interval)


if __name__ == "__main__":
    main()
