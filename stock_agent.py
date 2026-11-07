#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日股票 Agent v3 —— 风险监控版

与 v2 的区别（基于 1152 条历史记录的统计检验结果）：
  1. 诚实记分牌：真实复利净值 + 交易成本 + 永远并排显示买入持有基准
  2. 冻结 lessons.md（模型自己写的事后归因），改由代码写入客观统计 scoreboard.md
  3. 取消每日方向预测（buy/sell/hold）——该尺度上模型判对率等于随机
  4. 改为风险监控：异动归因、波动率状态、临近财报、关键价位、集中度

模型的角色从「预测者」变成「解释者」：所有风险信号由代码确定性计算，
模型只负责把这些事实解释成人话，不允许预测方向。

反馈：直接回复邮件，或 ./venv/bin/python3 stock_agent.py feedback "你的话"
⚠️ 模拟实验，不构成投资建议。
"""

import email
import email.header
import imaplib
import json
import re
import smtplib
import ssl
import statistics
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
HISTORY_PATH = BASE_DIR / "history.json"          # 遗留预测记录（只结算，不再新增）
SCOREBOARD_PATH = BASE_DIR / "scoreboard.md"      # 代码写入的客观统计
LESSONS_PATH = BASE_DIR / "lessons.md"            # v2 遗留，启动时归档
LESSONS_ARCHIVE = BASE_DIR / "lessons.archive.md"
FEEDBACK_PATH = BASE_DIR / "feedback.json"
LOG_PATH = BASE_DIR / "stock_agent.log"

DEFAULT_COST_PCT = 0.05      # 每笔往返交易成本（百分比），可在 config 里改
Z_ALERT = 2.0                # |单日涨跌| 超过 N 倍日波动率 → 异动
VOL_SPIKE = 1.5              # 20日波动率 / 60日波动率 超过此值 → 波动率抬升
NEAR_EXTREME_PCT = 3.0       # 距 52 周高/低点 N% 以内 → 临近关键位
EARNINGS_WINDOW_DAYS = 7     # 财报日在 N 天内 → 提醒
VOLUME_SPIKE = 2.0           # 成交量 / 20日均量 超过此值 → 放量


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


def get_groups(cfg: dict) -> dict:
    t = cfg["tickers"]
    return t if isinstance(t, dict) else {"关注列表": t}


# ============================================================ 行情 + 客观指标

def fetch_quotes(tickers: list) -> list:
    """抓行情，并计算波动率、z-score、52周位置、量比、财报日等客观指标。"""
    quotes = []
    for symbol in tickers:
        try:
            t = yf.Ticker(symbol)
            info = t.fast_info
            price = float(info.last_price)
            prev = float(info.previous_close)
            change_pct = (price - prev) / prev * 100 if prev else 0.0

            q = {"symbol": symbol, "price": round(price, 2),
                 "change_pct": round(change_pct, 2)}

            # 近 3 个月日线 → 波动率、动量、量比
            hist = t.history(period="3mo")
            closes = hist["Close"].dropna()
            if len(closes) >= 21:
                rets = closes.pct_change().dropna() * 100
                vol20 = float(rets.tail(20).std())
                vol60 = float(rets.tail(60).std()) if len(rets) >= 30 else vol20
                q["vol20"] = round(vol20, 2)
                q["vol60"] = round(vol60, 2)
                q["vol_ratio"] = round(vol20 / vol60, 2) if vol60 else 1.0
                q["zscore"] = round(change_pct / vol60, 2) if vol60 else 0.0
                q["ret5_pct"] = round(
                    (closes.iloc[-1] - closes.iloc[-6]) / closes.iloc[-6] * 100, 2
                ) if len(closes) >= 6 else None
                q["ret20_pct"] = round(
                    (closes.iloc[-1] - closes.iloc[-21]) / closes.iloc[-21] * 100, 2)
            vols = hist["Volume"].dropna()
            if len(vols) >= 21 and float(vols.tail(20).mean()):
                q["volume_ratio"] = round(
                    float(vols.iloc[-1]) / float(vols.tail(20).mean()), 2)

            # 52 周位置
            hi = getattr(info, "year_high", None)
            lo = getattr(info, "year_low", None)
            if hi and lo:
                q["year_high"], q["year_low"] = round(hi, 2), round(lo, 2)
                q["pct_from_high"] = round((price - hi) / hi * 100, 2)
                q["pct_from_low"] = round((price - lo) / lo * 100, 2)

            # 下一个财报日
            q["days_to_earnings"] = next_earnings_days(t)

            quotes.append(q)
            log(f"{symbol}: {price:.2f} ({change_pct:+.2f}%) "
                f"z={q.get('zscore','?')} volr={q.get('vol_ratio','?')}")
        except Exception as e:
            log(f"获取 {symbol} 失败: {e}")
    return quotes


def next_earnings_days(ticker) -> int:
    """距下次财报的天数；取不到返回 None。"""
    try:
        cal = ticker.calendar
        dates = None
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date")
        elif cal is not None and hasattr(cal, "loc"):
            dates = cal.loc["Earnings Date"].tolist()
        if not dates:
            return None
        if not isinstance(dates, (list, tuple)):
            dates = [dates]
        today = datetime.now().date()
        future = []
        for d in dates:
            d = getattr(d, "date", lambda: d)()
            if isinstance(d, str):
                d = datetime.strptime(d[:10], "%Y-%m-%d").date()
            if (d - today).days >= 0:
                future.append((d - today).days)
        return min(future) if future else None
    except Exception:
        return None


# ============================================================ 确定性风险信号

def compute_alerts(quotes: list) -> list:
    """全部由代码判定，模型无权新增或修改。按严重度排序。"""
    alerts = []
    for q in quotes:
        s = q["symbol"]
        z = q.get("zscore")
        if z is not None and abs(z) >= Z_ALERT:
            direction = "暴涨" if z > 0 else "暴跌"
            alerts.append({
                "symbol": s, "type": "单日异动", "severity": "high",
                "fact": f"{direction} {q['change_pct']:+.2f}%，相当于 {abs(z):.1f} 倍日常波动"})
        d = q.get("days_to_earnings")
        if d is not None and d <= EARNINGS_WINDOW_DAYS:
            alerts.append({
                "symbol": s, "type": "临近财报", "severity": "high",
                "fact": f"{d} 天后发布财报" if d else "今日/明日发布财报"})
        vr = q.get("vol_ratio")
        if vr is not None and vr >= VOL_SPIKE:
            alerts.append({
                "symbol": s, "type": "波动率抬升", "severity": "medium",
                "fact": f"20日波动率 {q['vol20']}% 是 60日的 {vr} 倍"})
        ph = q.get("pct_from_high")
        pl = q.get("pct_from_low")
        if ph is not None and ph >= -NEAR_EXTREME_PCT:
            alerts.append({
                "symbol": s, "type": "临近52周高", "severity": "medium",
                "fact": f"距 52 周高点 {abs(ph):.1f}%（${q['year_high']}）"})
        elif pl is not None and pl <= NEAR_EXTREME_PCT:
            alerts.append({
                "symbol": s, "type": "临近52周低", "severity": "medium",
                "fact": f"距 52 周低点 {abs(pl):.1f}%（${q['year_low']}）"})
        volr = q.get("volume_ratio")
        if volr is not None and volr >= VOLUME_SPIKE:
            alerts.append({
                "symbol": s, "type": "放量", "severity": "low",
                "fact": f"成交量是 20 日均量的 {volr} 倍"})
    order = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: order[a["severity"]])
    return alerts


def compute_breadth(quotes: list, groups: dict) -> dict:
    """涨跌家数与板块表现——组合层面的客观事实。"""
    ups = [q for q in quotes if q["change_pct"] > 0]
    downs = [q for q in quotes if q["change_pct"] < 0]
    by_group = {}
    sym_group = {s: g for g, syms in groups.items() for s in syms}
    for q in quotes:
        by_group.setdefault(sym_group.get(q["symbol"], "其他"), []).append(q["change_pct"])
    return {
        "up": len(ups), "down": len(downs), "total": len(quotes),
        "avg_change": round(statistics.mean(
            [q["change_pct"] for q in quotes]), 2) if quotes else 0.0,
        "best": max(quotes, key=lambda q: q["change_pct"])["symbol"] if quotes else None,
        "worst": min(quotes, key=lambda q: q["change_pct"])["symbol"] if quotes else None,
        "groups": {g: round(statistics.mean(v), 2) for g, v in by_group.items()},
    }


# ============================================================ 诚实记分牌

def score_predictions(history: dict, quotes: list) -> int:
    """只结算 v2 遗留的未结算预测；不再产生新预测。"""
    price_map = {q["symbol"]: q["price"] for q in quotes}
    today = datetime.now().date()
    resolved = 0
    for p in history.get("predictions", []):
        if p.get("resolved") or p["symbol"] not in price_map:
            continue
        days = (today - datetime.strptime(p["date"], "%Y-%m-%d").date()).days
        if days < 1:
            continue
        now_price = price_map[p["symbol"]]
        ret = (now_price - p["price_at"]) / p["price_at"] * 100
        action = p["action"]
        if action == "buy":
            correct, pnl = ret > 0.3, ret
        elif action == "sell":
            correct, pnl = ret < -0.3, -ret
        else:
            correct, pnl = abs(ret) <= 1.0, 0.0
        p["resolved"] = True
        p["outcome"] = {"date": today.strftime("%Y-%m-%d"), "days": days,
                        "price": now_price, "return_pct": round(ret, 2),
                        "correct": correct, "pnl_pct": round(pnl, 2)}
        resolved += 1
        log(f"结算遗留预测 {p['symbol']} {action} → {ret:+.2f}% "
            f"{'✓' if correct else '✗'}")
    return resolved


def honest_scoreboard(history: dict, cost_pct: float) -> dict:
    """真实复利净值 + 交易成本 + 买入持有基准 + 随机基准对照。"""
    done = [p for p in history.get("predictions", []) if p.get("resolved")]
    if not done:
        return {"total": 0}
    done.sort(key=lambda p: p["date"])

    # 策略净值：跟随 buy 信号，每条持有到结算，扣往返成本
    equity = 1.0
    for p in done:
        if p["action"] == "buy":
            equity *= (1 + p["outcome"]["return_pct"] / 100 - cost_pct / 100)
    strat_ret = (equity - 1) * 100

    # 等权买入持有基准：每个标的首次记录价 → 最后一次结算价
    by_sym = {}
    for p in done:
        by_sym.setdefault(p["symbol"], []).append(p)
    bh = [ (v[-1]["outcome"]["price"] - v[0]["price_at"]) / v[0]["price_at"] * 100
           for v in by_sym.values() if v[0]["price_at"] ]
    bh_ret = statistics.mean(bh) if bh else 0.0

    # 判对率 vs 随机基准（用同一批观测的涨跌分布算）
    rets = [p["outcome"]["return_pct"] for p in done]
    n = len(rets)
    base = {"buy": sum(1 for r in rets if r > 0.3) / n * 100,
            "sell": sum(1 for r in rets if r < -0.3) / n * 100,
            "hold": sum(1 for r in rets if abs(r) <= 1.0) / n * 100}
    actions = {}
    for a in ("buy", "sell", "hold"):
        s = [p for p in done if p["action"] == a]
        if s:
            actions[a] = {
                "n": len(s),
                "hit": round(sum(1 for p in s if p["outcome"]["correct"]) / len(s) * 100, 1),
                "baseline": round(base[a], 1)}

    # 信心校准
    calib = {}
    for c in range(1, 6):
        s = [p for p in done if p.get("confidence") == c]
        if s:
            calib[c] = {"n": len(s),
                        "hit": round(sum(1 for p in s if p["outcome"]["correct"]) / len(s) * 100, 1)}

    return {"total": n, "days": len(set(p["date"] for p in done)),
            "start": done[0]["date"], "end": done[-1]["date"],
            "strategy_ret": round(strat_ret, 2),
            "buyhold_ret": round(bh_ret, 2),
            "n_buys": sum(1 for p in done if p["action"] == "buy"),
            "cost_pct": cost_pct,
            "overall_hit": round(sum(1 for p in done if p["outcome"]["correct"]) / n * 100, 1),
            "actions": actions, "calibration": calib}


def write_scoreboard_md(sb: dict):
    """用代码写客观统计，取代模型自己写的事后归因。"""
    if not sb.get("total"):
        SCOREBOARD_PATH.write_text("（暂无已结算记录）\n", encoding="utf-8")
        return
    lines = [
        "# 客观统计（由代码自动生成，不由模型撰写）",
        "",
        f"统计区间 {sb['start']} → {sb['end']}，{sb['days']} 个交易日，"
        f"{sb['total']} 条已结算预测。",
        "",
        "## 方向预测实验结果（已于 v3 停止产生新预测）",
        f"- 跟随 buy 信号实际交易（{sb['n_buys']} 笔，扣 {sb['cost_pct']}% 往返成本）："
        f"**{sb['strategy_ret']:+.2f}%**",
        f"- 同期等权买入持有基准：**{sb['buyhold_ret']:+.2f}%**",
        f"- 整体判对率：{sb['overall_hit']}%",
        "",
        "| 动作 | 条数 | 判对率 | 随机基准 | 结论 |",
        "|---|---|---|---|---|",
    ]
    for a, d in sb["actions"].items():
        diff = d["hit"] - d["baseline"]
        verdict = "等于随机" if abs(diff) < 3 else ("优于随机" if diff > 0 else "差于随机")
        lines.append(f"| {a} | {d['n']} | {d['hit']}% | {d['baseline']}% | {verdict} |")
    if sb["calibration"]:
        lines += ["", "## 信心校准", "", "| 信心 | 条数 | 判对率 |", "|---|---|---|"]
        for c, d in sorted(sb["calibration"].items()):
            lines.append(f"| {c}星 | {d['n']} | {d['hit']}% |")
    lines += ["", "> 结论：在 1 日尺度上判对率与随机无统计差别，",
              "> 因此 v3 不再产生方向预测，改为风险监控。"]
    SCOREBOARD_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def archive_lessons():
    """把 v2 模型自写的 lessons.md 归档——它是对噪声的事后归因，不再喂给模型。"""
    if LESSONS_PATH.exists() and not LESSONS_ARCHIVE.exists():
        LESSONS_ARCHIVE.write_text(
            "# 已归档：v2 时期模型自己写的策略笔记\n"
            "# 这些是对随机波动的事后归因，v3 起不再使用。\n\n"
            + LESSONS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        LESSONS_PATH.unlink()
        log("已归档 lessons.md → lessons.archive.md（不再喂给模型）")


# ============================================================ 反馈

def strip_quoted_reply(text: str) -> str:
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(">") or re.match(r"^On .+ wrote:$", s) or re.match(r"^在.+写道：$", s):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def fetch_email_feedback(cfg: dict) -> list:
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
            if "股票" not in subject and "风险" not in subject and "行情" not in subject:
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
        log(f"读取邮件反馈失败（跳过）: {e}")
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


# ============================================================ 模型（解释者，非预测者）

def build_prompt(quotes, alerts, breadth, feedback) -> str:
    fb = load_json(FEEDBACK_PATH, [])[-5:]
    fb_text = "\n".join(f"- [{f['date']}] {f['text'][:200]}" for f in fb) or "（暂无）"
    alert_text = "\n".join(
        f"- [{a['severity']}] {a['symbol']} · {a['type']}：{a['fact']}"
        for a in alerts) or "（今日无触发任何风险信号）"
    return f"""你是一个股票风险监控助手。你的职责是把下面这些【已经由程序算好的客观事实】解释成人话，
帮助用户快速了解今天他的关注列表发生了什么、有什么需要留意的日程和风险。

## 绝对禁止
- 禁止预测价格涨跌方向，禁止给出买入/卖出/持有建议。
  （历史统计已证明：在 1 日尺度上这类预测的准确率与随机抛硬币无差别。）
- 禁止编造数据中不存在的信息（新闻、传闻、财务数据、分析师观点）。
- 只能基于下面给出的事实说话。

## 今日涨跌全貌
上涨 {breadth['up']} 只 / 下跌 {breadth['down']} 只（共 {breadth['total']} 只），
平均涨跌 {breadth['avg_change']:+.2f}%，最强 {breadth['best']}，最弱 {breadth['worst']}。
各板块平均涨跌：{json.dumps(breadth['groups'], ensure_ascii=False)}

## 程序判定的风险信号（你不能增删，只能解释）
{alert_text}

## 完整行情与指标数据
{json.dumps(quotes, ensure_ascii=False)}

## 用户最近的反馈（优先采纳）
{fb_text}

## 输出格式：只输出一个 JSON 对象，不要输出任何其他文字
{{
  "market_summary": "不超过100字，描述今天整体发生了什么（只陈述事实，不预测）",
  "highlights": [
    {{"symbol": "AAPL", "note": "一句话说明这个信号对持仓者意味着什么风险或需要做什么准备"}}
  ],
  "watch": "不超过80字：接下来几天已知的日程与风险点（如财报日、波动率处于高位），不含方向判断"
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
    if not isinstance(out, dict) or not out.get("market_summary"):
        return None
    hl = out.get("highlights")
    out["highlights"] = [h for h in hl if isinstance(h, dict) and h.get("symbol")] \
        if isinstance(hl, list) else []
    # 安全网：模型若仍吐出方向性词汇，剔除该条
    banned = ("买入", "卖出", "加仓", "减仓", "抄底", "追涨", "看涨", "看跌", "目标价")
    out["highlights"] = [h for h in out["highlights"]
                         if not any(b in str(h.get("note", "")) for b in banned)]
    return out


def ask_model(cfg: dict, prompt: str):
    try:
        resp = requests.post(
            f"{cfg.get('ollama_url', 'http://localhost:11434')}/api/generate",
            json={"model": cfg.get("model", "qwen3.6:35b-a3b"),
                  "prompt": prompt, "stream": False, "think": False,
                  "options": {"temperature": 0.3, "num_predict": 1500}},
            timeout=900)
        resp.raise_for_status()
        return parse_model_json(resp.json().get("response", ""))
    except Exception as e:
        log(f"调用本地模型失败: {e}")
        return None


# ============================================================ 邮件

SEV = {"high": ("🔴", "#c0392b"), "medium": ("🟠", "#e67e22"), "low": ("🔵", "#2980b9")}


def build_html(quotes, alerts, breadth, result, sb, groups, fb_count) -> str:
    # 风险信号
    if alerts:
        rows = "".join(
            f"<tr><td>{SEV[a['severity']][0]}</td>"
            f"<td><b>{a['symbol']}</b></td>"
            f"<td style='color:{SEV[a['severity']][1]}'>{a['type']}</td>"
            f"<td>{a['fact']}</td></tr>" for a in alerts)
        alerts_html = (
            "<table cellpadding='7' cellspacing='0' style='border-collapse:collapse;"
            "width:100%;font-size:14px'>"
            "<tr style='background:#2c3e50;color:#fff;text-align:left'>"
            "<th></th><th>标的</th><th>信号</th><th>事实</th></tr>" + rows + "</table>")
    else:
        alerts_html = "<p style='color:#27ae60'>✓ 今日没有触发任何风险信号。</p>"

    # 模型解读
    if result:
        hl = "".join(f"<li><b>{h['symbol']}</b>：{h.get('note','')}</li>"
                     for h in result.get("highlights", []))
        interp = (f"<div style='background:#f6f8fa;border-radius:8px;padding:12px 16px;"
                  f"margin:14px 0;line-height:1.7'>{result['market_summary']}"
                  + (f"<ul style='margin:10px 0 0'>{hl}</ul>" if hl else "")
                  + (f"<p style='margin:10px 0 0'><b>留意：</b>{result['watch']}</p>"
                     if result.get("watch") else "") + "</div>")
    else:
        interp = "<p style='color:#999'>本地模型今日未产出有效解读，以上为程序计算的客观信号。</p>"

    # 行情总览（按组）
    sym_group = {s: g for g, syms in groups.items() for s in syms}
    grouped = {}
    for q in quotes:
        grouped.setdefault(sym_group.get(q["symbol"], "其他"), []).append(q)
    qrows = []
    for g, qs in grouped.items():
        qrows.append(f"<tr><td colspan='5' style='background:#ecf0f1;font-weight:bold;"
                     f"padding:6px 8px'>📁 {g}（平均 {breadth['groups'].get(g,0):+.2f}%）</td></tr>")
        for q in qs:
            color = "#c0392b" if q["change_pct"] < 0 else "#27ae60"
            qrows.append(
                f"<tr><td><b>{q['symbol']}</b></td><td>${q['price']}</td>"
                f"<td style='color:{color}'>{q['change_pct']:+.2f}%</td>"
                f"<td>{q.get('ret5_pct','—')}%</td>"
                f"<td>{q.get('vol20','—')}%</td></tr>")
    quotes_html = (
        "<table cellpadding='6' cellspacing='0' style='border-collapse:collapse;"
        "width:100%;font-size:13px'>"
        "<tr style='background:#34495e;color:#fff;text-align:left'>"
        "<th>代码</th><th>现价</th><th>今日</th><th>近5日</th><th>20日波动率</th></tr>"
        + "".join(qrows) + "</table>")

    # 诚实记分牌
    if sb.get("total"):
        diff = sb["strategy_ret"] - sb["buyhold_ret"]
        verdict = "跑输" if diff < 0 else "跑赢"
        act_rows = "".join(
            f"<tr><td>{a}</td><td>{d['n']}</td><td>{d['hit']}%</td>"
            f"<td>{d['baseline']}%</td></tr>" for a, d in sb["actions"].items())
        board = f"""
        <h3 style="margin-top:22px">📊 方向预测实验的诚实结算（已停止）</h3>
        <p style="font-size:14px">区间 {sb['start']} → {sb['end']}，{sb['days']} 个交易日，
        {sb['total']} 条预测。</p>
        <table cellpadding='7' cellspacing='0' style="border-collapse:collapse;
               width:100%;font-size:14px">
          <tr><td>跟随 buy 信号交易（{sb['n_buys']} 笔，扣 {sb['cost_pct']}% 成本）</td>
              <td style="color:{'#c0392b' if sb['strategy_ret']<0 else '#27ae60'};
                  font-weight:bold">{sb['strategy_ret']:+.2f}%</td></tr>
          <tr><td>什么都不做（等权买入持有）</td>
              <td style="color:{'#c0392b' if sb['buyhold_ret']<0 else '#27ae60'};
                  font-weight:bold">{sb['buyhold_ret']:+.2f}%</td></tr>
          <tr style="background:#fdf2f2"><td><b>差额</b></td>
              <td><b>{diff:+.2f}%（{verdict}）</b></td></tr>
        </table>
        <table cellpadding='6' cellspacing='0' style="border-collapse:collapse;
               width:100%;font-size:13px;margin-top:10px">
          <tr style="background:#7f8c8d;color:#fff;text-align:left">
            <th>动作</th><th>条数</th><th>判对率</th><th>随机基准</th></tr>
          {act_rows}
        </table>
        <p style="color:#777;font-size:12px">判对率与随机基准持平 → 无统计显著的预测能力，
        这是 v3 停止方向预测的依据。</p>"""
    else:
        board = ""

    fb_note = (f"<p style='color:#2980b9'>✉️ 已读取你 {fb_count} 条新反馈。</p>"
               if fb_count else
               "<p style='color:#bbb;font-size:13px'>💡 直接回复本邮件即可给它反馈。</p>")

    return f"""
    <div style="font-family:-apple-system,'PingFang SC',sans-serif;max-width:700px">
      <h2>🛡️ 每日风险监控 · {datetime.now().strftime('%Y-%m-%d')}</h2>
      <p style="color:#666;font-size:14px">上涨 {breadth['up']} / 下跌 {breadth['down']}
         （共 {breadth['total']} 只），平均 {breadth['avg_change']:+.2f}%</p>
      <h3>⚠️ 今日风险信号</h3>
      {alerts_html}
      {interp}
      <h3>📋 行情总览</h3>
      {quotes_html}
      {board}
      {fb_note}
      <p style="color:#999;font-size:12px;margin-top:16px">
        本地模型 · 风险信号由程序确定性计算，模型仅负责解释。
        <b>本邮件不含任何买卖建议，不构成投资建议。</b>
      </p>
    </div>"""


def send_email(cfg: dict, html: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🛡️ 每日股票风险监控 {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = cfg["gmail_address"]
    msg["To"] = cfg["email_to"]
    msg.attach(MIMEText(html, "html", "utf-8"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(cfg["gmail_address"], cfg["gmail_app_password"])
        server.send_message(msg)
    log(f"邮件已发送至 {cfg['email_to']}")


# ============================================================ 主流程

def main():
    log("===== 开始运行 v3（风险监控版）=====")
    cfg = load_config()
    cost_pct = float(cfg.get("cost_pct", DEFAULT_COST_PCT))
    archive_lessons()

    groups = get_groups(cfg)
    sym_group = {s: g for g, syms in groups.items() for s in syms}
    quotes = fetch_quotes(list(sym_group))
    if not quotes:
        log("行情全部获取失败，退出")
        sys.exit(1)

    fb_new = fetch_email_feedback(cfg)

    history = load_json(HISTORY_PATH, {"predictions": []})
    if score_predictions(history, quotes):
        save_json(HISTORY_PATH, history)
    sb = honest_scoreboard(history, cost_pct)
    write_scoreboard_md(sb)

    alerts = compute_alerts(quotes)
    breadth = compute_breadth(quotes, groups)
    log(f"风险信号 {len(alerts)} 条")

    result = ask_model(cfg, build_prompt(quotes, alerts, breadth, fb_new))
    send_email(cfg, build_html(quotes, alerts, breadth, result, sb, groups, len(fb_new)))
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
