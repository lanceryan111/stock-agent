#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日股票行情 Agent v2 —— 带学习闭环
流程：
  1. 查股价 (yfinance)
  2. 读取你通过 Gmail 回信给出的反馈 (IMAP)
  3. 用真实市场价格给昨天的预测打分，更新战绩
  4. 把 [今日数据 + 历史战绩 + 错误案例 + 策略笔记 + 你的反馈] 喂给本地 Qwen3
     → 生成今日买/卖/持有观点，并让模型更新自己的策略笔记
  5. 发邮件：观点 + 累计战绩计分板

反馈方式：直接回复每天的行情邮件即可，下次运行会自动读取；
        也可以在终端运行: ./venv/bin/python3 stock_agent.py feedback "你的反馈"

⚠️ 模拟盘实验，所有观点仅供参考，不构成投资建议。
"""

import email
import email.header
import imaplib
import json
import re
import smtplib
import ssl
import sys
import traceback
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
HISTORY_PATH = BASE_DIR / "history.json"      # 所有预测及打分结果
LESSONS_PATH = BASE_DIR / "lessons.md"        # 模型自己维护的策略笔记
FEEDBACK_PATH = BASE_DIR / "feedback.json"    # 你的反馈
LOG_PATH = BASE_DIR / "stock_agent.log"

# 打分规则（可自行调整）：
CORRECT_BUY = 0.3    # buy 后涨幅 > +0.3% 算对
CORRECT_SELL = -0.3  # sell 后跌幅 < -0.3% 算对
CORRECT_HOLD = 1.0   # hold 后涨跌幅在 ±1.0% 内算对


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 行情

def get_groups(cfg: dict) -> dict:
    """tickers 支持两种写法：
    平铺列表:  "tickers": ["AAPL", "MSFT"]
    分组字典:  "tickers": {"科技巨头": ["AAPL","MSFT"], "芯片": ["NVDA","AMD"]}
    """
    t = cfg["tickers"]
    if isinstance(t, dict):
        return t
    return {"关注列表": t}


def fetch_quotes(tickers: list) -> list:
    quotes = []
    for symbol in tickers:
        try:
            t = yf.Ticker(symbol)
            info = t.fast_info
            price = info.last_price
            prev = info.previous_close
            change_pct = (price - prev) / prev * 100 if prev else 0.0
            hist = t.history(period="5d")["Close"]
            five_day = None
            if len(hist) >= 2:
                five_day = round((hist.iloc[-1] - hist.iloc[0]) / hist.iloc[0] * 100, 2)
            quotes.append({
                "symbol": symbol,
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
                "five_day_pct": five_day,
                "year_high": round(info.year_high, 2) if info.year_high else None,
                "year_low": round(info.year_low, 2) if info.year_low else None,
            })
            log(f"{symbol}: {price:.2f} ({change_pct:+.2f}%)")
        except Exception as e:
            log(f"获取 {symbol} 失败: {e}")
    return quotes


# ---------------------------------------------------------------- 反馈（学习信号1：你）

def strip_quoted_reply(text: str) -> str:
    """去掉邮件回复中引用的原文，只留你写的部分。"""
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", s) or re.match(r"^在.+写道：$", s):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def fetch_email_feedback(cfg: dict) -> list:
    """从 Gmail 收件箱读取你对行情邮件的未读回复，作为反馈。"""
    collected = []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(cfg["gmail_address"], cfg["gmail_app_password"])
        M.select("INBOX")
        typ, data = M.search(None, '(UNSEEN SUBJECT "Re:")')
        for num in (data[0].split() if data and data[0] else []):
            typ, msg_data = M.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            subject = str(email.header.make_header(
                email.header.decode_header(msg.get("Subject", ""))))
            if "股票" not in subject and "行情" not in subject:
                continue
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", errors="replace")
                        break
            else:
                body = msg.get_payload(decode=True).decode(
                    msg.get_content_charset() or "utf-8", errors="replace")
            body = strip_quoted_reply(body)[:1000]
            if body:
                collected.append(body)
                log(f"收到邮件反馈: {body[:60]}...")
        M.logout()
    except Exception as e:
        log(f"读取邮件反馈失败（跳过，不影响主流程）: {e}")
    if collected:
        fb = load_json(FEEDBACK_PATH, [])
        for text in collected:
            fb.append({"date": datetime.now().strftime("%Y-%m-%d"),
                       "source": "email", "text": text})
        save_json(FEEDBACK_PATH, fb)
    return collected


def add_cli_feedback(text: str):
    fb = load_json(FEEDBACK_PATH, [])
    fb.append({"date": datetime.now().strftime("%Y-%m-%d"),
               "source": "cli", "text": text[:1000]})
    save_json(FEEDBACK_PATH, fb)
    print("反馈已记录，下次运行时会喂给模型。")


# ---------------------------------------------------------------- 打分（学习信号2：市场）

def score_predictions(history: dict, quotes: list) -> int:
    """用今天的真实价格给之前未结算的预测打分。"""
    price_map = {q["symbol"]: q["price"] for q in quotes}
    today = datetime.now().date()
    resolved = 0
    for p in history["predictions"]:
        if p.get("resolved") or p["symbol"] not in price_map:
            continue
        pred_date = datetime.strptime(p["date"], "%Y-%m-%d").date()
        days = (today - pred_date).days
        if days < 1:
            continue
        now_price = price_map[p["symbol"]]
        ret = (now_price - p["price_at"]) / p["price_at"] * 100
        action = p["action"]
        if action == "buy":
            correct, pnl = ret > CORRECT_BUY, ret
        elif action == "sell":
            correct, pnl = ret < CORRECT_SELL, -ret
        else:  # hold
            correct, pnl = abs(ret) <= CORRECT_HOLD, 0.0
        p["resolved"] = True
        p["outcome"] = {
            "date": today.strftime("%Y-%m-%d"), "days": days,
            "price": now_price, "return_pct": round(ret, 2),
            "correct": correct, "pnl_pct": round(pnl, 2),
        }
        resolved += 1
        log(f"打分 {p['symbol']} {action}@{p['price_at']} → {now_price} "
            f"({ret:+.2f}%) {'✓对' if correct else '✗错'}")
    return resolved


def compute_stats(history: dict) -> dict:
    done = [p for p in history["predictions"] if p.get("resolved")]
    if not done:
        return {"total": 0, "accuracy": None, "cum_pnl": 0.0}
    correct = sum(1 for p in done if p["outcome"]["correct"])
    cum_pnl = sum(p["outcome"]["pnl_pct"] for p in done)
    return {"total": len(done),
            "accuracy": round(correct / len(done) * 100, 1),
            "cum_pnl": round(cum_pnl, 2)}


# ---------------------------------------------------------------- 模型

def read_lessons() -> str:
    try:
        return LESSONS_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "（暂无，第一次运行）"


def build_prompt(quotes, history, stats, feedback_new) -> str:
    recent_done = [p for p in history["predictions"] if p.get("resolved")][-12:]
    track = "\n".join(
        f"- {p['date']} {p['symbol']} {p['action']}(信心{p.get('confidence','?')}) "
        f"@{p['price_at']} → {p['outcome']['days']}天后 {p['outcome']['return_pct']:+.2f}% "
        f"{'✓' if p['outcome']['correct'] else '✗'} 理由曾是: {p.get('reason','')[:50]}"
        for p in recent_done) or "（暂无历史战绩）"
    all_fb = load_json(FEEDBACK_PATH, [])[-5:]
    fb_text = "\n".join(f"- [{f['date']}] {f['text'][:200]}" for f in all_fb) or "（暂无）"
    stats_text = (f"已结算 {stats['total']} 条，准确率 {stats['accuracy']}%，"
                  f"模拟累计盈亏 {stats['cum_pnl']:+.2f}%（每条观点等权）"
                  if stats["total"] else "尚无战绩")
    return f"""你是一个每天运行的股票模拟盘分析 agent。你的历史预测会被真实市场打分，请基于战绩持续改进。

## 今日行情数据（JSON）
{json.dumps(quotes, ensure_ascii=False)}

## 你的累计战绩
{stats_text}

## 最近已结算的预测（✓=判断正确 ✗=判断错误）
{track}

## 你此前总结的策略笔记
{read_lessons()}

## 用户最近给你的反馈（重要，优先采纳）
{fb_text}

## 任务
1. 反思最近的错误预测和用户反馈，先想清楚要调整什么。
2. 对每只股票给出今日观点：buy / hold / sell，信心 1-5，理由一句话（必须基于数据，不许编造消息面）。
3. 用不超过 250 字更新你的策略笔记（保留仍然有效的旧经验，写入新教训）。

## 输出格式：只输出一个 JSON 对象，不要输出任何其他文字
{{
  "market_summary": "不超过100字的今日总评",
  "calls": [
    {{"symbol": "AAPL", "action": "buy|hold|sell", "confidence": 3, "reason": "一句话理由"}}
  ],
  "updated_lessons": "更新后的完整策略笔记"
}}"""


def parse_model_json(text: str):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if m:
        text = m.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start:end + 1]
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(out.get("calls"), list):
        return None
    valid = []
    for c in out["calls"]:
        if (isinstance(c, dict) and c.get("symbol")
                and c.get("action") in ("buy", "hold", "sell")):
            c["confidence"] = min(5, max(1, int(c.get("confidence", 3))))
            valid.append(c)
    out["calls"] = valid
    return out if valid else None


def ask_model(cfg: dict, prompt: str):
    try:
        resp = requests.post(
            f"{cfg.get('ollama_url', 'http://localhost:11434')}/api/generate",
            json={"model": cfg.get("model", "qwen3:32b"),
                  "prompt": prompt, "stream": False, "think": False,
                  "options": {"temperature": 0.4, "num_predict": 1800}},
            timeout=900)
        resp.raise_for_status()
        return parse_model_json(resp.json().get("response", ""))
    except Exception as e:
        log(f"调用本地模型失败: {e}")
        return None


# ---------------------------------------------------------------- 邮件

ACTION_ZH = {"buy": ("买入", "#27ae60"), "sell": ("卖出", "#c0392b"),
             "hold": ("持有", "#7f8c8d")}


def build_html(quotes, result, stats, history, fb_count) -> str:
    price_map = {q["symbol"]: q for q in quotes}
    # 按分组排列观点行，每组一个小标题行
    grouped = {}
    for c in (result["calls"] if result else []):
        g = price_map.get(c["symbol"], {}).get("group", "")
        grouped.setdefault(g, []).append(c)
    call_rows = []
    for g, calls in grouped.items():
        if g and len(grouped) > 1:
            call_rows.append(
                f"<tr><td colspan='6' style='background:#ecf0f1;"
                f"font-weight:bold;padding:6px 8px'>📁 {g}</td></tr>")
        for c in calls:
            q = price_map.get(c["symbol"], {})
            zh, color = ACTION_ZH[c["action"]]
            stars = "★" * c["confidence"] + "☆" * (5 - c["confidence"])
            call_rows.append(
                f"<tr><td><b>{c['symbol']}</b></td>"
                f"<td>${q.get('price','—')}</td>"
                f"<td>{q.get('change_pct',0):+.2f}%</td>"
                f"<td style='color:{color};font-weight:bold'>{zh}</td>"
                f"<td>{stars}</td><td>{c.get('reason','')}</td></tr>")
    calls_html = (
        "<table cellpadding='7' cellspacing='0' "
        "style='border-collapse:collapse;width:100%;font-size:14px'>"
        "<tr style='background:#2c3e50;color:#fff;text-align:left'>"
        "<th>代码</th><th>现价</th><th>日涨跌</th><th>观点</th><th>信心</th><th>理由</th></tr>"
        + "".join(call_rows) + "</table>"
        if call_rows else
        "<p style='color:#999'>本地模型今日未产出有效观点（详见日志），以下仅为行情。</p>"
        + "".join(f"<p>{q['symbol']}: ${q['price']} ({q['change_pct']:+.2f}%)</p>"
                  for q in quotes))
    summary_html = (f"<div style='background:#f6f8fa;border-radius:8px;"
                    f"padding:12px 16px;margin:14px 0'>{result['market_summary']}</div>"
                    if result and result.get("market_summary") else "")
    if stats["total"]:
        recent = [p for p in history["predictions"] if p.get("resolved")][-5:]
        recent_html = "".join(
            f"<li>{p['date']} {p['symbol']} {ACTION_ZH[p['action']][0]} → "
            f"{p['outcome']['return_pct']:+.2f}% "
            f"{'✓' if p['outcome']['correct'] else '✗'}</li>" for p in recent)
        board = (f"<h3>📊 模型战绩</h3>"
                 f"<p>已结算 <b>{stats['total']}</b> 条 · 准确率 "
                 f"<b>{stats['accuracy']}%</b> · 模拟累计盈亏 "
                 f"<b>{stats['cum_pnl']:+.2f}%</b></p><ul>{recent_html}</ul>")
    else:
        board = "<p style='color:#999'>📊 战绩从明天开始结算（今天是第一批预测）。</p>"
    fb_note = (f"<p style='color:#2980b9'>✉️ 已读取你 {fb_count} 条新反馈并喂给模型。</p>"
               if fb_count else
               "<p style='color:#bbb;font-size:13px'>💡 直接回复本邮件即可给模型反馈，下次运行自动学习。</p>")
    return f"""
    <div style="font-family:-apple-system,'PingFang SC',sans-serif;max-width:680px">
      <h2>📈 每日股票观点 · {datetime.now().strftime('%Y-%m-%d')}</h2>
      {summary_html}
      {calls_html}
      {board}
      {fb_note}
      <p style="color:#999;font-size:12px;margin-top:16px">
        本地 Qwen3-32B 模拟盘实验 · 观点由 AI 生成且历史准确率有限，仅供参考，
        <b>不构成投资建议</b>，请勿据此实际交易。
      </p>
    </div>"""


def send_email(cfg: dict, html: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📈 每日股票观点 {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = cfg["gmail_address"]
    msg["To"] = cfg["email_to"]
    msg.attach(MIMEText(html, "html", "utf-8"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(cfg["gmail_address"], cfg["gmail_app_password"])
        server.send_message(msg)
    log(f"邮件已发送至 {cfg['email_to']}")


# ---------------------------------------------------------------- 主流程

def main():
    log("===== 开始运行 v2 =====")
    cfg = load_config()
    history = load_json(HISTORY_PATH, {"predictions": []})

    groups = get_groups(cfg)
    sym_group = {s: g for g, syms in groups.items() for s in syms}
    quotes = fetch_quotes(list(sym_group))
    for q in quotes:
        q["group"] = sym_group.get(q["symbol"], "")
    if not quotes:
        log("行情全部获取失败，退出")
        sys.exit(1)

    fb_new = fetch_email_feedback(cfg)
    n = score_predictions(history, quotes)
    if n:
        save_json(HISTORY_PATH, history)
    stats = compute_stats(history)

    result = ask_model(cfg, build_prompt(quotes, history, stats, fb_new))

    if result:
        today = datetime.now().strftime("%Y-%m-%d")
        price_map = {q["symbol"]: q["price"] for q in quotes}
        for c in result["calls"]:
            if c["symbol"] in price_map:
                history["predictions"].append({
                    "date": today, "symbol": c["symbol"],
                    "action": c["action"], "confidence": c["confidence"],
                    "reason": c.get("reason", ""),
                    "price_at": price_map[c["symbol"]], "resolved": False})
        save_json(HISTORY_PATH, history)
        if result.get("updated_lessons"):
            LESSONS_PATH.write_text(result["updated_lessons"], encoding="utf-8")
            log("策略笔记已更新")

    send_email(cfg, build_html(quotes, result, stats, history, len(fb_new)))
    log("===== 运行结束 =====")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "feedback":
        add_cli_feedback(" ".join(sys.argv[2:]))
        sys.exit(0)
    try:
        main()
    except Exception:
        log("运行出错:\n" + traceback.format_exc())
        sys.exit(1)
