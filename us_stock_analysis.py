#!/usr/bin/env python3
"""
米国株分析メール
・主要指数・セクター・VIX のスナップショット
・S&P 500 チャートパターン分析（SVG）
・市場レジーム分析（過去類似局面との比較）
・Groq LLM による AI コメント
毎朝 6:15 SGT にメール送信する
"""

import math
import os
import re
import smtplib
import sys
import traceback
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytz
from groq import Groq

# ── 設定 ─────────────────────────────────────────────────
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
GMAIL_USER         = os.environ.get("GMAIL_USER", "alx99281@gmail.com")
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

# 主要指数・ETF
US_INDICES = {
    "^GSPC":  "S&P 500",
    "^NDX":   "NASDAQ 100",
    "^DJI":   "Dow Jones",
    "^RUT":   "Russell 2000",
    "^VIX":   "VIX",
    "^TNX":   "米10年金利",
    "DXY=X":  "ドル指数",
    "EURUSD=X": "EUR/USD",
    "USDJPY=X": "USD/JPY",
    "GC=F":   "金 (Gold)",
    "CL=F":   "原油 (WTI)",
}

# セクター ETF
SECTOR_ETFS = {
    "XLK":  "テクノロジー",
    "XLF":  "金融",
    "XLE":  "エネルギー",
    "XLV":  "ヘルスケア",
    "XLI":  "資本財",
    "XLY":  "一般消費財",
    "XLP":  "生活必需品",
    "XLU":  "公益",
    "XLRE": "不動産",
    "XLB":  "素材",
    "XLC":  "通信",
    "SMH":  "半導体",
}

# 個別注目銘柄
WATCHLIST = {
    "NVDA": "NVIDIA",
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "AMZN": "Amazon",
    "META": "Meta",
    "GOOGL": "Alphabet",
    "TSLA": "Tesla",
    "AMD":  "AMD",
    "AVGO": "Broadcom",
    "TSM":  "TSMC",
}


# ── データ取得 ────────────────────────────────────────────

def _isnan(v) -> bool:
    try:
        return math.isnan(float(v))
    except Exception:
        return True


def fetch_quote(tickers: list[str], period: str = "5d") -> dict:
    """yfinance で終値・変化率を取得する。"""
    import yfinance as yf
    result = {}
    try:
        data = yf.download(
            tickers, period=period, interval="1d",
            auto_adjust=True, progress=False, threads=True,
        )
        close = data.get("Close", data) if hasattr(data, "get") else data
        if hasattr(close, "columns"):
            for t in tickers:
                if t in close.columns:
                    series = close[t].dropna()
                    if len(series) >= 2:
                        cur  = float(series.iloc[-1])
                        prev = float(series.iloc[-2])
                        pct  = (cur - prev) / prev * 100 if prev else 0
                        result[t] = {"price": round(cur, 4), "pct": round(pct, 2)}
                    elif len(series) == 1:
                        result[t] = {"price": round(float(series.iloc[-1]), 4), "pct": None}
        else:
            series = close.dropna()
            t = tickers[0]
            if len(series) >= 2:
                cur  = float(series.iloc[-1])
                prev = float(series.iloc[-2])
                pct  = (cur - prev) / prev * 100 if prev else 0
                result[t] = {"price": round(cur, 4), "pct": round(pct, 2)}
    except Exception as e:
        print(f"[WARN] fetch_quote({tickers[:3]}...): {e}")
    return result


def fetch_all_quotes() -> dict:
    """指数・セクター・銘柄をまとめて取得する。"""
    all_tickers = list(US_INDICES) + list(SECTOR_ETFS) + list(WATCHLIST)
    return fetch_quote(all_tickers)


# ── S&P 500 チャートパターン分析 ─────────────────────────

def run_spx_pattern(window_days: int = 63, forward_days: int = 21, top_n: int = 5) -> dict | None:
    try:
        import numpy as np
        import pandas as pd
        import yfinance as yf
        from numpy.lib.stride_tricks import sliding_window_view

        print("  [SPX パターン] データ取得中...")
        raw = yf.download("^GSPC", period="25y", interval="1d",
                          auto_adjust=True, progress=False)
        if raw.empty:
            return None
        close = raw["Close"].dropna()
        dates = pd.to_datetime(close.index)
        vals  = close.values.astype(float)

        if len(vals) < window_days + forward_days + 10:
            return None

        def znorm(a):
            s = a.std()
            return (a - a.mean()) / (s if s > 1e-9 else 1.0)

        cur_norm = znorm(vals[-window_days:])
        all_wins = sliding_window_view(vals, window_days)
        means    = all_wins.mean(axis=1, keepdims=True)
        stds     = np.where(all_wins.std(axis=1, keepdims=True) < 1e-9, 1.0,
                            all_wins.std(axis=1, keepdims=True))
        all_norm = (all_wins - means) / stds
        corrs    = (all_norm @ cur_norm) / window_days

        cutoff   = len(corrs) - forward_days - 5
        corrs    = corrs[:cutoff]
        top_idx  = np.argsort(corrs)[::-1]

        results, used = [], []
        for idx in top_idx:
            if len(results) >= top_n:
                break
            if any(abs(idx - u) < window_days // 2 for u in used):
                continue
            used.append(idx)
            end_idx  = idx + window_days - 1
            fwd_vals = vals[idx + window_days: idx + window_days + forward_days]
            base     = vals[end_idx]
            fwd_rets = ((fwd_vals - base) / base * 100).tolist() if base else []

            base_s       = vals[idx] if vals[idx] else 1.0
            hist_indexed = (vals[idx: idx + window_days] / base_s * 100).tolist()
            fwd_indexed  = (vals[idx + window_days: idx + window_days + forward_days] / base_s * 100).tolist()
            results.append({
                "start_date":      dates[idx].strftime("%Y-%m-%d"),
                "end_date":        dates[end_idx].strftime("%Y-%m-%d"),
                "corr":            round(float(corrs[idx]), 4),
                "forward_returns": fwd_rets,
                "forward_final":   round(float(fwd_rets[-1]), 2) if fwd_rets else None,
                "pattern_indexed": hist_indexed,
                "forward_indexed": fwd_indexed,
            })

        if not results:
            return None

        cur_base = vals[-window_days] if vals[-window_days] else 1.0
        finals   = [r["forward_final"] for r in results if r["forward_final"] is not None]
        print(f"  [SPX パターン] 完了 — {len(results)} 件")
        return {
            "current_indexed":       (vals[-window_days:] / cur_base * 100).tolist(),
            "current_start":         dates[-window_days].strftime("%Y-%m-%d"),
            "current_end":           dates[-1].strftime("%Y-%m-%d"),
            "top_similar":           results,
            "avg_forward_return":    round(float(__import__("numpy").mean(finals)), 2) if finals else None,
            "median_forward_return": round(float(__import__("numpy").median(finals)), 2) if finals else None,
            "window_days":           window_days,
            "forward_days":          forward_days,
        }
    except Exception as e:
        print(f"[WARN] SPX パターン分析失敗: {e}")
        traceback.print_exc()
        return None


# ── SVG チャート ──────────────────────────────────────────

def build_pattern_svg(data: dict) -> str:
    import numpy as np

    W, H = 560, 210
    ML, MR, MT, MB = 50, 12, 28, 40
    pw = W - ML - MR
    ph = H - MT - MB

    cur  = np.array(data.get("current_indexed", []), dtype=float)
    top1 = data["top_similar"][0] if data["top_similar"] else None
    if cur.size == 0:
        return ""

    fwd_days = data.get("forward_days", 21)
    win_days = data.get("window_days", 63)
    all_y    = list(cur)
    if top1:
        all_y.extend(top1.get("pattern_indexed", []))
        all_y.extend(top1.get("forward_indexed", []))
    all_y = [y for y in all_y if not np.isnan(float(y))]
    if not all_y:
        return ""

    y_min    = min(all_y) * 0.997
    y_max    = max(all_y) * 1.003
    total_x  = win_days + fwd_days

    def sx(i):  return ML + (i / max(total_x - 1, 1)) * pw
    def sy(v):  return MT + (1 - (v - y_min) / max(y_max - y_min, 1e-6)) * ph

    def to_path(pts, x_offset=0):
        valid = [(i, float(v)) for i, v in enumerate(pts) if not np.isnan(float(v))]
        if not valid:
            return ""
        parts = [f"M {sx(valid[0][0]+x_offset):.1f},{sy(valid[0][1]):.1f}"]
        for i, v in valid[1:]:
            parts.append(f"L {sx(i+x_offset):.1f},{sy(v):.1f}")
        return " ".join(parts)

    elems = []
    for i in range(5):
        yv  = y_min + (y_max - y_min) * i / 4
        yp  = sy(yv)
        elems.append(f'<line x1="{ML}" y1="{yp:.1f}" x2="{W-MR}" y2="{yp:.1f}" '
                     f'stroke="#f0f0f0" stroke-width="1"/>')
        elems.append(f'<text x="{ML-4}" y="{yp+4:.1f}" text-anchor="end" '
                     f'font-size="9" fill="#aaa">{yv:.0f}</text>')

    if y_min < 100 < y_max:
        y100 = sy(100)
        elems.append(f'<line x1="{ML}" y1="{y100:.1f}" x2="{W-MR}" y2="{y100:.1f}" '
                     f'stroke="#ddd" stroke-width="1" stroke-dasharray="3,2"/>')

    tx = sx(win_days - 1)
    elems.append(f'<line x1="{tx:.1f}" y1="{MT}" x2="{tx:.1f}" y2="{H-MB}" '
                 f'stroke="#bbb" stroke-width="1" stroke-dasharray="3,2"/>')
    elems.append(f'<text x="{tx:.1f}" y="{H-MB+11}" text-anchor="middle" '
                 f'font-size="8" fill="#999">今日</text>')

    if top1:
        patt = top1.get("pattern_indexed", [])
        fwdv = top1.get("forward_indexed", [])
        if patt:
            d = to_path(patt)
            if d:
                elems.append(f'<path d="{d}" stroke="#E53935" stroke-width="2.2" '
                             f'fill="none" opacity="0.75"/>')
        if fwdv and patt:
            join = [patt[-1]] + list(fwdv)
            d = to_path(join, x_offset=len(patt) - 1)
            if d:
                elems.append(f'<path d="{d}" stroke="#E53935" stroke-width="1.8" '
                             f'fill="none" stroke-dasharray="5,3" opacity="0.7"/>')

    d = to_path(cur.tolist())
    if d:
        elems.append(f'<path d="{d}" stroke="#1565C0" stroke-width="2.5" fill="none"/>')

    avg_r = data.get("avg_forward_return")
    title = "S&P 500 現在 vs 類似局面TOP1（インデックス 期初=100）"
    if avg_r is not None:
        n = len(data["top_similar"])
        title += f"  上位{n}局面 翌{fwd_days}日平均 {avg_r:+.1f}%"
    elems.append(f'<text x="{ML+pw//2}" y="{MT-10}" text-anchor="middle" '
                 f'font-size="9" font-weight="bold" fill="#444">{title}</text>')

    legy = H - 8
    elems.append(f'<line x1="{ML}" y1="{legy}" x2="{ML+16}" y2="{legy}" '
                 f'stroke="#1565C0" stroke-width="2.5"/>')
    cs = data.get("current_start", "")
    ce = data.get("current_end", "")
    elems.append(f'<text x="{ML+20}" y="{legy+4}" font-size="9" fill="#555">'
                 f'現在 {cs}〜{ce}</text>')

    if top1:
        lx2 = ML + 220
        ff  = top1.get("forward_final")
        ffs = f" 翌{fwd_days}日:{ff:+.1f}%" if ff is not None else ""
        elems.append(f'<line x1="{lx2}" y1="{legy}" x2="{lx2+16}" y2="{legy}" '
                     f'stroke="#E53935" stroke-width="2"/>')
        elems.append(f'<text x="{lx2+20}" y="{legy+4}" font-size="9" fill="#555">'
                     f'類似① {top1["start_date"][:7]}{ffs}</text>')

    svg_inner = "\n  ".join(elems)
    total_h   = H + 4
    return (f'<svg viewBox="0 0 {W} {total_h}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;max-width:{W}px;height:auto;display:block;">\n'
            f'  <rect width="{W}" height="{total_h}" fill="white" rx="4"/>\n'
            f'  {svg_inner}\n'
            f'</svg>')


# ── AI コメント ───────────────────────────────────────────

def generate_ai_comment(quotes: dict, pattern: dict | None) -> str:
    client = Groq(api_key=GROQ_API_KEY)

    def fmt(ticker, label):
        q = quotes.get(ticker)
        if not q:
            return f"  {label}: データなし"
        pct = q["pct"]
        pct_s = f"{pct:+.2f}%" if pct is not None else "N/A"
        return f"  {label}: {q['price']:,.2f}  ({pct_s})"

    index_lines  = "\n".join(fmt(t, US_INDICES[t]) for t in US_INDICES)
    sector_lines = "\n".join(fmt(t, SECTOR_ETFS[t]) for t in SECTOR_ETFS)

    pattern_text = "パターン分析なし"
    if pattern:
        avg = pattern.get("avg_forward_return")
        med = pattern.get("median_forward_return")
        top1 = pattern["top_similar"][0] if pattern["top_similar"] else None
        lines = []
        if top1:
            lines.append(f"最類似局面: {top1['start_date']}〜{top1['end_date']}（相関:{top1['corr']:.3f}）")
            if top1.get("forward_final") is not None:
                lines.append(f"  → その後{pattern['forward_days']}日: {top1['forward_final']:+.1f}%")
        if avg is not None:
            lines.append(f"上位{len(pattern['top_similar'])}局面平均: {avg:+.1f}%  中央値: {med:+.1f}%")
        pattern_text = "\n".join(lines)

    prompt = f"""あなたは米国株市場のプロアナリストです。以下のデータを踏まえ、日本語で簡潔に分析してください。

【主要指数・資産（前日比）】
{index_lines}

【セクター ETF（前日比）】
{sector_lines}

【S&P 500 チャートパターン分析】
{pattern_text}

以下のフォーマットで出力してください（各項目2〜4行）：

## 📊 本日の市場概況
（主要指数の動きと特徴）

## 🔥 注目セクター・テーマ
（強いセクター・弱いセクターとその背景）

## 💡 今後1〜2週間の注目ポイント
（パターン分析も踏まえた示唆、リスク要因）
"""
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
    )
    return response.choices[0].message.content


# ── HTML 生成 ─────────────────────────────────────────────

def _color(pct) -> str:
    if pct is None:
        return "#888"
    return "#2e7d32" if float(pct) >= 0 else "#c62828"


def _pct_html(pct) -> str:
    if pct is None:
        return "<span style='color:#ccc;'>—</span>"
    v = float(pct)
    color = _color(v)
    return f"<span style='color:{color};font-weight:bold;'>{v:+.2f}%</span>"


def build_index_table(quotes: dict) -> str:
    rows = ""
    for i, (t, label) in enumerate(US_INDICES.items()):
        q   = quotes.get(t, {})
        bg  = "#fff" if i % 2 == 0 else "#fafafa"
        pr  = f"{q['price']:,.4f}" if q.get("price") else "—"
        rows += (f"<tr style='background:{bg};'>"
                 f"<td style='padding:5px 10px;font-size:12px;font-weight:bold;'>{label}</td>"
                 f"<td style='padding:5px 10px;font-size:12px;color:#555;'>{t}</td>"
                 f"<td style='padding:5px 10px;font-size:12px;text-align:right;'>{pr}</td>"
                 f"<td style='padding:5px 10px;font-size:12px;text-align:right;'>"
                 f"{_pct_html(q.get('pct'))}</td></tr>")
    return f"""<table style='width:100%;border-collapse:collapse;'>
  <thead>
    <tr style='background:#eceff1;'>
      <th style='padding:6px 10px;font-size:11px;text-align:left;'>名称</th>
      <th style='padding:6px 10px;font-size:11px;text-align:left;'>Ticker</th>
      <th style='padding:6px 10px;font-size:11px;text-align:right;'>価格</th>
      <th style='padding:6px 10px;font-size:11px;text-align:right;'>前日比</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>"""


def build_sector_table(quotes: dict) -> str:
    items = [(t, SECTOR_ETFS[t], quotes.get(t, {})) for t in SECTOR_ETFS]
    items_sorted = sorted(items, key=lambda x: (x[2].get("pct") or -999), reverse=True)
    rows = ""
    for i, (t, label, q) in enumerate(items_sorted):
        bg = "#fff" if i % 2 == 0 else "#fafafa"
        rows += (f"<tr style='background:{bg};'>"
                 f"<td style='padding:5px 10px;font-size:12px;font-weight:bold;'>{label}</td>"
                 f"<td style='padding:5px 10px;font-size:12px;color:#555;'>{t}</td>"
                 f"<td style='padding:5px 10px;font-size:12px;text-align:right;'>"
                 f"{_pct_html(q.get('pct'))}</td></tr>")
    return f"""<table style='width:100%;border-collapse:collapse;'>
  <thead>
    <tr style='background:#eceff1;'>
      <th style='padding:6px 10px;font-size:11px;text-align:left;'>セクター</th>
      <th style='padding:6px 10px;font-size:11px;text-align:left;'>ETF</th>
      <th style='padding:6px 10px;font-size:11px;text-align:right;'>前日比</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>"""


def build_watchlist_table(quotes: dict) -> str:
    items = [(t, WATCHLIST[t], quotes.get(t, {})) for t in WATCHLIST]
    rows  = ""
    for i, (t, label, q) in enumerate(items):
        bg  = "#fff" if i % 2 == 0 else "#fafafa"
        pr  = f"${q['price']:,.2f}" if q.get("price") else "—"
        rows += (f"<tr style='background:{bg};'>"
                 f"<td style='padding:5px 10px;font-size:12px;font-weight:bold;'>{label}</td>"
                 f"<td style='padding:5px 10px;font-size:12px;color:#555;'>{t}</td>"
                 f"<td style='padding:5px 10px;font-size:12px;text-align:right;'>{pr}</td>"
                 f"<td style='padding:5px 10px;font-size:12px;text-align:right;'>"
                 f"{_pct_html(q.get('pct'))}</td></tr>")
    return f"""<table style='width:100%;border-collapse:collapse;'>
  <thead>
    <tr style='background:#eceff1;'>
      <th style='padding:6px 10px;font-size:11px;text-align:left;'>銘柄</th>
      <th style='padding:6px 10px;font-size:11px;text-align:left;'>Ticker</th>
      <th style='padding:6px 10px;font-size:11px;text-align:right;'>価格</th>
      <th style='padding:6px 10px;font-size:11px;text-align:right;'>前日比</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>"""


def build_html(quotes: dict, pattern: dict | None,
               ai_comment: str, date_str: str) -> str:

    ai_html = (ai_comment
               .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    ai_html = re.sub(r"## (📊.*?)(\n|$)",
                     r"<h4 style='color:#37474f;font-size:13px;margin:10px 0 2px;'>\1</h4>",
                     ai_html)
    ai_html = re.sub(r"## (🔥.*?)(\n|$)",
                     r"<h4 style='color:#b71c1c;font-size:13px;margin:10px 0 2px;'>\1</h4>",
                     ai_html)
    ai_html = re.sub(r"## (💡.*?)(\n|$)",
                     r"<h4 style='color:#1565c0;font-size:13px;margin:10px 0 2px;'>\1</h4>",
                     ai_html)
    ai_html = ai_html.replace("\n- ", "<br>&nbsp;• ").replace("\n", "<br>")

    pattern_section = ""
    if pattern:
        svg = build_pattern_svg(pattern)
        rows = ""
        for i, sim in enumerate(pattern["top_similar"], 1):
            ff      = sim["forward_final"]
            ret_str = f"{ff:+.1f}%" if ff is not None else "N/A"
            ret_col = "#2e7d32" if (ff or 0) >= 0 else "#c62828"
            rows += (f"<tr style='border-bottom:1px solid #f0f0f0;'>"
                     f"<td style='padding:5px 8px;font-size:11px;color:#888;'>#{i}</td>"
                     f"<td style='padding:5px 8px;font-size:11px;'>"
                     f"{sim['start_date']} 〜 {sim['end_date']}</td>"
                     f"<td style='padding:5px 8px;font-size:11px;text-align:center;'>"
                     f"{sim['corr']:.3f}</td>"
                     f"<td style='padding:5px 8px;font-size:11px;text-align:center;"
                     f"font-weight:bold;color:{ret_col};'>{ret_str}</td></tr>")
        avg     = pattern.get("avg_forward_return")
        med     = pattern.get("median_forward_return")
        avg_str = f"{avg:+.1f}%" if avg is not None else "N/A"
        med_str = f"{med:+.1f}%" if med is not None else "N/A"
        avg_col = "#2e7d32" if (avg or 0) >= 0 else "#c62828"
        fwd     = pattern["forward_days"]
        n       = len(pattern["top_similar"])
        pattern_section = f"""
  <div style="background:#f0f4ff;padding:16px 24px;border:1px solid #e8e8e8;border-top:none;">
    <div style="font-size:15px;font-weight:bold;color:#1565C0;margin-bottom:12px;">
      📉 S&amp;P 500 チャートパターン分析
      （{pattern['current_start']} 〜 {pattern['current_end']}）
    </div>
    {svg}
    <div style="margin-top:14px;">
      <span style="font-size:12px;font-weight:bold;color:#555;">
        形状類似局面 TOP{n}（翌{fwd}営業日リターン）
      </span>
      <table style="width:100%;border-collapse:collapse;margin-top:6px;">
        <tr style="background:#e8eaf6;">
          <th style="padding:5px 8px;font-size:11px;text-align:left;">#</th>
          <th style="padding:5px 8px;font-size:11px;text-align:left;">期間</th>
          <th style="padding:5px 8px;font-size:11px;text-align:center;">相関係数</th>
          <th style="padding:5px 8px;font-size:11px;text-align:center;">翌月リターン</th>
        </tr>
        {rows}
      </table>
      <p style="font-size:12px;margin:10px 0 0;">
        平均翌月リターン：<strong style="color:{avg_col};">{avg_str}</strong>
        　中央値：<strong style="color:{avg_col};">{med_str}</strong>
      </p>
    </div>
    <p style="font-size:10px;color:#bbb;margin:8px 0 0;">
      ※ Pearson r でチャート形状類似度を計算。参考指標です。
    </p>
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:640px;
             margin:0 auto;color:#333;font-size:14px;">
  <div style="background:#0d47a1;color:white;padding:16px 24px;border-radius:8px 8px 0 0;">
    <div style="font-size:20px;font-weight:bold;">🇺🇸 米国株分析レポート</div>
    <div style="font-size:12px;opacity:.8;margin-top:4px;">{date_str}</div>
  </div>

  <!-- AI コメント -->
  <div style="background:#fff;padding:16px 24px;border:1px solid #e8e8e8;border-top:none;">
    <div style="font-size:12px;line-height:1.8;border-left:3px solid #90caf9;padding-left:10px;">
      {ai_html}
    </div>
    <p style="font-size:10px;color:#ccc;margin:10px 0 0;">
      ※ AI コメントは参考情報です。投資判断の根拠となるものではありません。
    </p>
  </div>

  <!-- 主要指数 -->
  <div style="background:#fff8f8;padding:16px 24px;border:1px solid #e8e8e8;border-top:none;">
    <div style="font-size:15px;font-weight:bold;color:#b71c1c;margin-bottom:10px;">
      📊 主要指数・資産
    </div>
    {build_index_table(quotes)}
  </div>

  <!-- セクター -->
  <div style="background:#f9fbe7;padding:16px 24px;border:1px solid #e8e8e8;border-top:none;">
    <div style="font-size:15px;font-weight:bold;color:#33691e;margin-bottom:10px;">
      🏭 セクター ETF（強弱順）
    </div>
    {build_sector_table(quotes)}
  </div>

  <!-- ウォッチリスト -->
  <div style="background:#e8f5e9;padding:16px 24px;border:1px solid #e8e8e8;border-top:none;">
    <div style="font-size:15px;font-weight:bold;color:#1b5e20;margin-bottom:10px;">
      🔭 注目銘柄
    </div>
    {build_watchlist_table(quotes)}
  </div>

  {pattern_section}

  <div style="background:#f5f5f5;padding:10px 24px;border:1px solid #e8e8e8;
              border-top:none;border-radius:0 0 8px 8px;text-align:center;">
    <p style="font-size:10px;color:#bbb;margin:0;">
      Powered by Groq (Llama 3.3 70B) · yfinance
    </p>
  </div>
</body></html>"""


# ── メール送信 ────────────────────────────────────────────

def send_email(subject: str, html_body: str) -> None:
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = GMAIL_USER
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, GMAIL_USER, msg.as_string())
    print("メール送信完了")


# ── メイン ────────────────────────────────────────────────

def main():
    sgt      = pytz.timezone("Asia/Singapore")
    now_sgt  = datetime.now(sgt)
    days_ja  = ["月", "火", "水", "木", "金", "土", "日"]
    date_str = now_sgt.strftime(
        f"%Y年%m月%d日（{days_ja[now_sgt.weekday()]}）%H:%M SGT")

    print(f"=== 米国株分析 {date_str} ===")

    print("\n[1/4] 価格データ取得中...")
    quotes = fetch_all_quotes()
    print(f"  {len(quotes)} 銘柄取得完了")

    print("[2/4] S&P 500 パターン分析中...")
    pattern = run_spx_pattern(window_days=63, forward_days=21, top_n=5)

    print("[3/4] AI コメント生成中 (Groq)...")
    ai_comment = generate_ai_comment(quotes, pattern)

    print("[4/4] メール送信中...")
    html    = build_html(quotes, pattern, ai_comment, date_str)
    subject = f"🇺🇸 米国株分析 {now_sgt.strftime('%m/%d(%a)')} | S&P·NASDAQ·セクター·AI分析"
    send_email(subject, html)


if __name__ == "__main__":
    main()
