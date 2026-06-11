#!/usr/bin/env python3
"""
金融SNSダイジェスト (StockTwits版)

StockTwits の無料 API でトレンドメッセージを取得して表示する。
認証不要・無料。GitHub Actions の IP ブロック対策に curl_cffi を使用。
取得失敗時はニュースに逃がさずエラーを明示する。

  📈 注目の投資商品 TOP5
  🗾 日本関連 TOP5 (EWJ/NKY メッセージ)
  🌍 海外で話題 TOP5
  🔥 総合ランキング TOP10
"""

import os
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytz
import requests

# ── 設定 ─────────────────────────────────────────────────
GMAIL_USER         = os.environ.get("GMAIL_USER", "alx99281@gmail.com")
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; market-digest-bot/1.0)"}
ST_BASE  = "https://api.stocktwits.com/api/2"

# 投資商品キーワード（本文マッチ用）
PRODUCT_KEYWORDS = [
    # 個別株
    "nvda", "nvidia", "tsla", "tesla", "aapl", "apple", "msft", "microsoft",
    "amzn", "amazon", "meta", "googl", "google",
    "amd", "arm", "smci", "intc", "intel",
    # 半導体・テーマ
    "dram", "hbm", "semiconductor", "chip", "ai stock",
    # 指数・ETF
    "spy", "qqq", "nasdaq", "dow", "s&p", "s&p500", "etf", "nikkei", "topix",
    # 債券・金利
    "treasury", "bond", "yield", "fed", "fomc",
    # 為替
    "usdjpy", "eurusd", "forex", "yen",
    # コモディティ
    "gold", "silver", "oil", "wti", "crude", "copper",
    # 仮想通貨
    "bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol",
    # 不動産
    "reit", "real estate",
]

# 日本関連シンボル（StockTwits）
JP_SYMBOLS = ["EWJ", "DXJ", "DBJP", "HEWJ"]

# 人気シンボル（グローバル）
POPULAR_SYMBOLS = [
    "NVDA", "TSLA", "AAPL", "META", "MSFT", "GOOGL", "AMZN",
    "SPY", "QQQ", "BTC.X", "ETH.X", "GLD", "TLT", "AMD",
]



# ── StockTwits 取得 ───────────────────────────────────────

def _get(url: str, params: dict = None) -> dict | None:
    """HTTP GET ヘルパー。失敗時は None。

    GitHub Actions のデータセンターIPは StockTwits の Cloudflare に
    403 でブロックされるため、curl_cffi の Chrome 偽装を優先使用する。
    """
    # ① curl_cffi（Chrome TLSフィンガープリント偽装）
    try:
        from curl_cffi import requests as cf_requests
        r = cf_requests.get(url, params=params, impersonate="chrome", timeout=15)
        if r.status_code == 200:
            return r.json()
        print(f"[WARN] curl_cffi {url} → HTTP {r.status_code}")
    except ImportError:
        pass
    except Exception as e:
        print(f"[WARN] curl_cffi GET {url}: {e}")

    # ② 通常の requests（ローカル実行などIPがブロックされていない環境用）
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=12)
        if r.status_code == 200:
            return r.json()
        print(f"[WARN] requests {url} → HTTP {r.status_code}")
    except Exception as e:
        print(f"[WARN] requests GET {url}: {e}")
    return None


def _parse_msg(raw: dict, category: str = "global") -> dict:
    """StockTwits メッセージを内部形式に変換する。"""
    u        = raw.get("user", {})
    likes    = raw.get("likes", {}).get("total", 0)
    reshares = raw.get("reshares", {}).get("reshared_count", 0)
    eng      = likes + reshares * 2
    symbols  = [s["symbol"] for s in raw.get("entities", {}).get("symbols", [])]
    sent_raw = raw.get("entities", {}).get("sentiment") or {}
    return {
        "id":         raw["id"],
        "text":       raw.get("body", ""),
        "username":   u.get("username", ""),
        "name":       u.get("name", u.get("username", "")),
        "followers":  u.get("followers_count", 0),
        "likes":      likes,
        "reshares":   reshares,
        "eng":        eng,
        "symbols":    symbols,
        "sentiment":  sent_raw.get("basic", ""),
        "created_at": raw.get("created_at", ""),
        "category":   category,
    }


def fetch_stocktwits() -> tuple[list[dict], list[dict]]:
    """
    StockTwits からメッセージを取得し (global_msgs, jp_msgs) を返す。
    global_msgs : トレンド + 人気シンボル合算
    jp_msgs     : 日本関連シンボルのメッセージ
    """
    seen_ids: set[int] = set()
    global_msgs: list[dict] = []
    jp_msgs:     list[dict] = []

    # ① トレンドストリーム（認証不要）
    data = _get(f"{ST_BASE}/streams/trending.json")
    if data:
        for raw in data.get("messages", []):
            m = _parse_msg(raw, "global")
            if m["id"] not in seen_ids:
                seen_ids.add(m["id"])
                global_msgs.append(m)
        print(f"  Trending: {len(global_msgs)} 件")
    time.sleep(0.5)

    # ② 人気シンボルのストリーム
    for sym in POPULAR_SYMBOLS:
        data = _get(f"{ST_BASE}/streams/symbol/{sym}.json")
        if data:
            for raw in data.get("messages", []):
                m = _parse_msg(raw, "global")
                if m["id"] not in seen_ids:
                    seen_ids.add(m["id"])
                    global_msgs.append(m)
        time.sleep(0.3)

    # ③ 日本関連シンボル
    for sym in JP_SYMBOLS:
        data = _get(f"{ST_BASE}/streams/symbol/{sym}.json")
        if data:
            for raw in data.get("messages", []):
                m = _parse_msg(raw, "jp")
                if m["id"] not in seen_ids:
                    seen_ids.add(m["id"])
                    jp_msgs.append(m)
        time.sleep(0.3)

    # エンゲージメント順ソート
    global_msgs.sort(key=lambda x: x["eng"], reverse=True)
    jp_msgs.sort(key=lambda x: x["eng"], reverse=True)
    print(f"  StockTwits Global: {len(global_msgs)} / JP: {len(jp_msgs)} 件")
    return global_msgs, jp_msgs


# ── HTML パーツ ───────────────────────────────────────────

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def st_card(m: dict, rank: int) -> str:
    """StockTwits メッセージカード HTML"""
    msg_url     = (f"https://stocktwits.com/{m['username']}/message/{m['id']}"
                   if m.get("username") else "https://stocktwits.com")
    profile_url = f"https://stocktwits.com/{m['username']}"

    # センチメントバッジ
    sent = m.get("sentiment", "")
    if sent == "Bullish":
        sent_html = ("<span style='background:#e8f5e9;color:#2e7d32;border-radius:3px;"
                     "padding:1px 6px;font-size:10px;margin-right:5px;'>📈 Bullish</span>")
    elif sent == "Bearish":
        sent_html = ("<span style='background:#fce4ec;color:#c62828;border-radius:3px;"
                     "padding:1px 6px;font-size:10px;margin-right:5px;'>📉 Bearish</span>")
    else:
        sent_html = ""

    # シンボルタグ（最大4個）
    sym_html = "".join(
        f"<span style='background:#e3f2fd;color:#1565c0;border-radius:3px;"
        f"padding:1px 5px;font-size:10px;margin-right:3px;'>${_esc(s)}</span>"
        for s in m.get("symbols", [])[:4]
    )

    author_html = ""
    if m.get("username"):
        author_html = (
            f"<a href='{profile_url}' target='_blank' "
            f"style='color:#555;text-decoration:none;font-weight:bold;'>"
            f"{_esc(m['name'])}</a>"
            f"<span style='color:#aaa;margin-left:4px;'>@{_esc(m['username'])}</span>"
        )

    return f"""
<div style="border-bottom:1px solid #f0f0f0;padding:10px 0;">
  <div style="font-size:11px;color:#aaa;margin-bottom:4px;">
    {sent_html}{sym_html}#{rank}&nbsp;&nbsp;♥{m['likes']:,}&nbsp;&nbsp;🔁{m['reshares']:,}
  </div>
  {"<div style='font-size:12px;margin-bottom:4px;'>" + author_html + "</div>" if author_html else ""}
  <div style="font-size:13px;line-height:1.65;color:#222;">{_esc(m['text'])}</div>
  <a href="{msg_url}" target="_blank"
     style="font-size:11px;color:#1976d2;text-decoration:none;display:inline-block;margin-top:3px;">
    → StockTwits で見る ↗
  </a>
</div>"""


def section(icon: str, title: str, subtitle: str, cards_html: str,
            title_color: str = "#b71c1c") -> str:
    return (f"<h3 style='color:{title_color};margin:22px 0 2px;font-size:15px;'>"
            f"{icon} {title}</h3>"
            f"<div style='font-size:11px;color:#aaa;margin-bottom:4px;'>{subtitle}</div>"
            f"{cards_html}")


def filter_product_msgs(msgs: list[dict], top_n: int = 5) -> list[dict]:
    """投資商品・ティッカー言及メッセージをエンゲージメント順で返す"""
    matched = [
        m for m in msgs
        if m.get("symbols")                                       # ティッカータグ付き
        or any(kw in m["text"].lower() for kw in PRODUCT_KEYWORDS)
    ]
    matched.sort(key=lambda x: x["eng"], reverse=True)
    return matched[:top_n]


def build_html(global_msgs: list, jp_msgs: list,
               date_str: str, time_label: str,
               since_str: str, now_str: str) -> str:

    icon_hdr = "🌅" if "朝" in time_label else "🌆"
    has_data = bool(global_msgs or jp_msgs)
    sections_html = []

    if has_data:
        all_msgs = sorted(global_msgs + jp_msgs, key=lambda x: x["eng"], reverse=True)

        # 📈 注目の投資商品 TOP5
        product_msgs = filter_product_msgs(all_msgs)
        if product_msgs:
            cards = "".join(st_card(m, i) for i, m in enumerate(product_msgs, 1))
            sections_html.append(section(
                "📈", "注目の投資商品",
                "商品・ティッカー言及メッセージ · エンゲージメント順",
                cards, "#2e7d32"))

        # 🗾 日本関連 TOP5（取得できた場合のみ表示）
        if jp_msgs:
            cards = "".join(st_card(m, i) for i, m in enumerate(jp_msgs[:5], 1))
            sections_html.append(section(
                "🗾", "日本関連",
                "EWJ/NKY 関連メッセージ · エンゲージメント順",
                cards, "#c62828"))

        # 🌍 海外で話題 TOP5
        if global_msgs:
            cards = "".join(st_card(m, i) for i, m in enumerate(global_msgs[:5], 1))
            sections_html.append(section(
                "🌍", "海外で話題",
                "StockTwits トレンド · エンゲージメント順",
                cards, "#1565c0"))

        # 🔥 総合ランキング TOP10
        if all_msgs:
            cards = "".join(st_card(m, i) for i, m in enumerate(all_msgs[:10], 1))
            sections_html.append(section(
                "🔥", "総合ランキング TOP10",
                "全メッセージ合算 · エンゲージメント順",
                cards, "#b71c1c"))

        body_html = "\n".join(sections_html)
        src_note  = "StockTwits · 直近メッセージ"
    else:
        body_html = """
        <div style="background:#fff3e0;border-left:4px solid #ff9800;
                    padding:16px;border-radius:4px;margin-top:16px;">
          <div style="font-size:14px;font-weight:bold;color:#e65100;margin-bottom:8px;">
            ⚠️ データを取得できませんでした
          </div>
          <div style="font-size:13px;color:#555;line-height:1.7;">
            StockTwits API への接続に失敗しました。<br>
            次回の配信まで自動的に再試行します。
          </div>
        </div>"""
        src_note = "StockTwits · データ取得失敗"

    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:640px;
             margin:0 auto;color:#333;font-size:14px;">
  <div style="background:#1a237e;color:white;padding:16px 24px;border-radius:8px 8px 0 0;">
    <div style="font-size:20px;font-weight:bold;">{icon_hdr} 金融SNSダイジェスト</div>
    <div style="font-size:12px;opacity:.85;margin-top:4px;">{date_str}　{time_label}</div>
    <div style="font-size:11px;opacity:.7;margin-top:3px;">
      集計期間: {since_str} 〜 {now_str} ／ {src_note}
    </div>
  </div>
  <div style="background:#fff;padding:20px 24px;border:1px solid #e8e8e8;
              border-top:none;line-height:1.85;">
    {body_html}
  </div>
  <div style="background:#f5f5f5;padding:10px 24px;border:1px solid #e8e8e8;
              border-top:none;border-radius:0 0 8px 8px;text-align:center;">
    <p style="font-size:10px;color:#bbb;margin:0;">ソース: {src_note}</p>
  </div>
</body></html>"""


# ── メール送信 ─────────────────────────────────────────────

def send_email(subject: str, html_body: str, plain_body: str) -> None:
    msg             = MIMEMultipart("alternative")
    msg["Subject"]  = subject
    msg["From"]     = GMAIL_USER
    msg["To"]       = GMAIL_USER
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, GMAIL_USER, msg.as_string())
    print(f"メール送信完了: {subject}")


# ── メイン ────────────────────────────────────────────────

def main():
    sgt      = pytz.timezone("Asia/Singapore")
    now_sgt  = datetime.now(sgt)
    days_ja  = ["月", "火", "水", "木", "金", "土", "日"]
    date_str = now_sgt.strftime(
        f"%Y年%m月%d日（{days_ja[now_sgt.weekday()]}）%H:%M SGT")
    hour       = now_sgt.hour
    time_label = "朝版（6:00 SGT）" if 4 <= hour < 14 else "夕版（18:00 SGT）"
    hours_back = 24
    since_str  = (now_sgt - timedelta(hours=hours_back)).strftime("%m/%d %H:%M SGT")
    now_str    = now_sgt.strftime("%m/%d %H:%M SGT")
    subject    = (f"{'🌅' if '朝' in time_label else '🌆'} 金融ダイジェスト"
                  f" {now_sgt.strftime('%m/%d(%a)')} {time_label}")

    print(f"=== {subject} ===")

    try:
        print("[1/3] StockTwits 取得中...")
        global_msgs, jp_msgs = fetch_stocktwits()

        print("[2/3] HTML 生成中...")
        html = build_html(global_msgs, jp_msgs,
                          date_str, time_label, since_str, now_str)

        # プレーンテキスト（スパムフィルタ対策）
        all_msgs = sorted(global_msgs + jp_msgs, key=lambda x: x["eng"], reverse=True)
        plain_lines = [subject, ""]
        if all_msgs:
            for m in all_msgs[:10]:
                syms = " ".join(f"${s}" for s in m.get("symbols", []))
                plain_lines.append(
                    f"♥{m['likes']} 🔁{m['reshares']}  @{m['username']}  {syms}")
                plain_lines.append(m["text"][:150])
                plain_lines.append(
                    f"https://stocktwits.com/{m['username']}/message/{m['id']}")
                plain_lines.append("")
        else:
            plain_lines.append("データを取得できませんでした。")
        plain = "\n".join(plain_lines)

        print("[3/3] メール送信中...")
        send_email(subject, html, plain)

    except Exception as e:
        import traceback
        print(f"[ERROR] {e}")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
