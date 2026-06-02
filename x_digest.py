#!/usr/bin/env python3
"""
X（Twitter）金融ソーシャルダイジェスト

X/Twitter・Reddit・StockTwits から金融関連の注目投稿をピックアップし、
  - 注目の投資商品・投資手法（最大5件、具体的なティッカー付き）
  - 注目ツイート TOP10（エンゲージメント順、リンク付き）
を毎朝6時・夕方6時に送信する。

TWITTER_BEARER_TOKEN が設定されていれば X の実データを取得。
未設定の場合は Reddit + StockTwits のみで動作。
"""

import os
import re
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytz
import requests
from groq import Groq

# ── 設定 ─────────────────────────────────────────────────
GROQ_API_KEY         = os.environ["GROQ_API_KEY"]
GMAIL_USER           = os.environ.get("GMAIL_USER", "alx99281@gmail.com")
GMAIL_APP_PASSWORD   = os.environ["GMAIL_APP_PASSWORD"]
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN", "")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; market-digest-bot/1.0)"}


# ── データ取得 ────────────────────────────────────────────

def fetch_twitter(hours: int = 13) -> list[dict]:
    """X/Twitter API v2 で金融関連ツイートを取得（Bearer Token 必須）"""
    if not TWITTER_BEARER_TOKEN:
        return []

    auth  = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")

    queries = [
        "(株 OR 日経 OR 為替 OR 金利 OR ETF OR 投資信託 OR 債券 OR 仕組み債"
        " OR レバレッジ OR 空売り OR 不動産 OR マンション) lang:ja -is:retweet -is:reply",
        "(stocks OR investing OR markets OR Fed OR SPX OR Nikkei OR ETF"
        " OR leverage OR hedge OR bond OR forex OR realestate) lang:en -is:retweet -is:reply",
    ]

    tweets = []
    for query in queries:
        try:
            resp = requests.get(
                "https://api.twitter.com/2/tweets/search/recent",
                headers=auth,
                params={
                    "query":        query,
                    "max_results":  20,
                    "start_time":   since,
                    "tweet.fields": "public_metrics,created_at,lang,author_id",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                for t in resp.json().get("data", []):
                    m = t.get("public_metrics", {})
                    eng = (m.get("like_count", 0)
                           + m.get("retweet_count", 0) * 2
                           + m.get("reply_count", 0))
                    tweets.append({
                        "id":         t["id"],
                        "text":       t["text"],
                        "engagement": eng,
                        "lang":       t.get("lang", ""),
                        "likes":      m.get("like_count", 0),
                        "retweets":   m.get("retweet_count", 0),
                    })
            elif resp.status_code == 429:
                print("[WARN] Twitter rate limit")
                break
            else:
                print(f"[WARN] Twitter API {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"[WARN] Twitter fetch error: {e}")
        time.sleep(1)

    tweets.sort(key=lambda x: x["engagement"], reverse=True)
    return tweets[:30]


def fetch_reddit(hours: int = 13) -> list[dict]:
    """Reddit 金融コミュニティのホット投稿を取得（認証不要）"""
    subreddits = [
        "investing", "stocks", "wallstreetbets", "finance",
        "StockMarket", "Economics", "SecurityAnalysis",
        "JapanFinance", "japan_investing",
    ]
    posts = []
    # www.reddit.com が bot ブロックする場合は old.reddit.com を試みる
    for base_url in ["https://www.reddit.com", "https://old.reddit.com"]:
        if posts:
            break
        for sub in subreddits:
            try:
                resp = requests.get(
                    f"{base_url}/r/{sub}/hot.json?limit=10",
                    headers=HEADERS, timeout=10,
                )
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        for item in data["data"]["children"]:
                            d = item["data"]
                            posts.append({
                                "title":    d["title"],
                                "score":    d.get("score", 0),
                                "comments": d.get("num_comments", 0),
                                "sub":      sub,
                                "url":      f"https://www.reddit.com{d.get('permalink', '')}",
                            })
                    except Exception:
                        pass  # HTML が返った場合などはスキップ
                elif resp.status_code == 429:
                    print(f"[WARN] Reddit rate limit ({base_url})")
                    break
            except Exception as e:
                print(f"[WARN] Reddit r/{sub} ({base_url}): {e}")
            time.sleep(0.3)

    posts.sort(key=lambda x: x["score"] + x["comments"] * 2, reverse=True)
    return posts[:30]


def fetch_stocktwits() -> list[dict]:
    """StockTwits トレンドシンボルを取得（認証不要）"""
    try:
        resp = requests.get(
            "https://api.stocktwits.com/api/2/trending/symbols.json",
            headers=HEADERS, timeout=10,
        )
        if resp.status_code == 200:
            return [
                {"symbol": s["symbol"], "title": s.get("title", "")}
                for s in resp.json().get("symbols", [])[:15]
            ]
    except Exception as e:
        print(f"[WARN] StockTwits: {e}")
    return []


# ── Groq 要約（投資商品セクションのみ） ──────────────────────

def summarize_products(tweets: list, reddit: list, stocktwits: list,
                       time_label: str) -> str:
    """
    注目の投資商品・投資手法（最大5件）のみをAIで生成。
    トピックセクションは廃止し、代わりにTOP10ツイートを直接表示する。
    """
    client = Groq(api_key=GROQ_API_KEY)

    body = ""
    if tweets:
        body += "【X (Twitter) 高エンゲージメント投稿（上位20件）】\n"
        for i, t in enumerate(tweets[:20], 1):
            body += f"[{i}] ({t['lang']}) {t['text'][:200]} (eng:{t['engagement']})\n"

    if reddit:
        body += "\n【Reddit 金融コミュニティ 注目投稿（上位20件）】\n"
        for i, p in enumerate(reddit[:20], 1):
            body += f"[{i}] r/{p['sub']}: {p['title']} (score:{p['score']})\n"

    if stocktwits:
        body += "\n【StockTwits トレンドシンボル】\n"
        body += "  ".join(f"{s['symbol']}({s['title']})" for s in stocktwits) + "\n"

    if not body:
        return ""

    sources_label = "X/Twitter・Reddit・StockTwits" if tweets else "Reddit・StockTwits"

    prompt = f"""あなたは金融ソーシャルメディアのアナリストです。
以下は{sources_label}から収集した金融関連の投稿データです。
日本語で「注目の投資商品・投資手法」セクションのみを作成してください。

【ルール】
- 日本・米国・欧州など世界中の投稿を対象とする
- 英語投稿は日本語に翻訳・要約する
- 注目度・エンゲージメントの高いものを優先する
- 必ず具体的な商品名・ティッカーシンボルで記載すること
  （例: NVIDIA(NVDA)、DRAM ETF(SOXQ)、S&P500(SPY)、ドル円(USDJPY)、米10年債、
       東京都内マンション、米国REIT(VNQ)など）
- 対象: 個別株・ETF・投資信託・債券・為替・仕組み商品・仮想通貨・
       コモディティ・不動産（マンション・商業施設・REIT等）・
       レバレッジ・空売りなどの投資手法
- 言及が少ない場合は件数を減らしてよい（最大5件）
- 引用番号などは不要。商品名と理由だけ簡潔に

【出力形式（このセクションのみ出力）】

## 📈 注目の投資商品・投資手法（最大5件）
1. **[具体的な商品名・ティッカー]** — 注目理由を1〜2文

---
{body}
"""
    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
        )
        return resp.choices[0].message.content
    except Exception as e:
        print(f"[WARN] Groq error: {e}")
        return ""


# ── HTML 生成 ──────────────────────────────────────────────

def _escape(text: str) -> str:
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))


def build_top_tweets_html(tweets: list, reddit: list,
                          stocktwits: list | None = None) -> str:
    """TOP10ツイート（またはReddit投稿）を直接HTML描画。リンク付き。"""
    items_html = ""

    if tweets:
        section_title = "🔥 注目ツイート TOP10"
        items = tweets[:10]
        for i, t in enumerate(items, 1):
            url      = f"https://x.com/i/web/status/{t['id']}"
            text_esc = _escape(t["text"])
            eng_str  = f"♥{t['likes']:,} RT{t['retweets']:,}"
            lang_tag = (f"<span style='background:#e3f2fd;color:#1565c0;"
                        f"border-radius:3px;padding:1px 5px;font-size:10px;"
                        f"margin-right:6px;'>{t['lang'].upper()}</span>"
                        if t.get("lang") else "")
            items_html += f"""
    <div style="border-bottom:1px solid #f0f0f0;padding:10px 0;">
      <div style="font-size:12px;color:#888;margin-bottom:4px;">
        {lang_tag}
        <span style="color:#aaa;">#{i} &nbsp; {eng_str}</span>
      </div>
      <div style="font-size:13px;line-height:1.6;color:#222;">{text_esc}</div>
      <div style="margin-top:5px;">
        <a href="{url}" target="_blank"
           style="font-size:11px;color:#1976d2;text-decoration:none;">
          → X (Twitter) で見る ↗
        </a>
      </div>
    </div>"""

    elif reddit:
        section_title = "🔥 注目 Reddit投稿 TOP10"
        items = reddit[:10]
        for i, p in enumerate(items, 1):
            title_esc = _escape(p["title"])
            url       = p.get("url", "")
            score_str = f"▲{p['score']:,} 💬{p['comments']:,}"
            items_html += f"""
    <div style="border-bottom:1px solid #f0f0f0;padding:10px 0;">
      <div style="font-size:12px;color:#888;margin-bottom:4px;">
        <span style="background:#fff3e0;color:#e65100;border-radius:3px;
                     padding:1px 5px;font-size:10px;margin-right:6px;">
          r/{p['sub']}
        </span>
        <span style="color:#aaa;">#{i} &nbsp; {score_str}</span>
      </div>
      <div style="font-size:13px;line-height:1.6;color:#222;">{title_esc}</div>
      {"<div style='margin-top:5px;'><a href='" + url + "' target='_blank' style='font-size:11px;color:#1976d2;text-decoration:none;'>→ Reddit で見る ↗</a></div>" if url else ""}
    </div>"""
    elif stocktwits:
        # Twitter・Reddit 両方取得不可の場合は StockTwits トレンドシンボルを表示
        section_title = "📊 StockTwits トレンドシンボル"
        for i, s in enumerate(stocktwits[:15], 1):
            sym   = _escape(s["symbol"])
            title = _escape(s.get("title", ""))
            url   = f"https://stocktwits.com/symbol/{s['symbol']}"
            items_html += f"""
    <div style="display:inline-block;margin:4px;">
      <a href="{url}" target="_blank"
         style="background:#e8f5e9;color:#2e7d32;padding:5px 10px;
                border-radius:4px;text-decoration:none;font-size:12px;
                font-weight:bold;">
        {sym}
        {"<span style='font-weight:normal;font-size:11px;color:#555;'>&nbsp;{}</span>".format(title) if title else ""}
      </a>
    </div>"""
        return f"""
  <h3 style="color:#b71c1c;margin:20px 0 8px;font-size:15px;">{section_title}</h3>
  <div style="line-height:2.2;">{items_html}</div>"""

    else:
        return (
            "<p style='color:#aaa;font-size:12px;margin-top:16px;'>"
            "⚠️ データソースへの接続に失敗しました。次回の配信をお待ちください。"
            "</p>"
        )

    return f"""
  <h3 style="color:#b71c1c;margin:20px 0 4px;font-size:15px;">{section_title}</h3>
  <div style="font-size:12px;color:#aaa;margin-bottom:8px;">エンゲージメント順</div>
  {items_html}"""


def build_html(products_md: str, tweets: list, reddit: list, stocktwits: list,
               date_str: str, time_label: str,
               since_str: str, now_str: str,
               has_twitter: bool) -> str:

    # ── 投資商品セクション（Markdown → HTML）
    products_html = ""
    if products_md:
        h = _escape(products_md)
        h = re.sub(
            r"## (📈[^\n]*)",
            r"<h3 style='color:#2e7d32;margin:16px 0 4px;font-size:15px;'>\1</h3>",
            h)
        h = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", h)
        h = re.sub(r"^(\d+)\. ", r"<br>&nbsp;\1. ", h, flags=re.MULTILINE)
        h = h.replace("\n---\n", "").replace("\n", "<br>")
        products_html = h

    # ── TOP10ツイート（直接HTML描画）
    top_tweets_html = build_top_tweets_html(tweets, reddit, stocktwits)

    source_str = ("X (Twitter) / Reddit / StockTwits" if has_twitter
                  else "Reddit / StockTwits")
    icon = "🌅" if "朝" in time_label else "🌆"

    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:640px;
             margin:0 auto;color:#333;font-size:14px;">
  <div style="background:#1a237e;color:white;padding:16px 24px;
              border-radius:8px 8px 0 0;">
    <div style="font-size:20px;font-weight:bold;">{icon} 金融ソーシャルダイジェスト</div>
    <div style="font-size:12px;opacity:.85;margin-top:4px;">
      {date_str}　{time_label}
    </div>
    <div style="font-size:11px;opacity:.7;margin-top:3px;">
      集計期間: {since_str} 〜 {now_str}
    </div>
  </div>
  <div style="background:#fff;padding:20px 24px;border:1px solid #e8e8e8;
              border-top:none;line-height:1.85;">
    {products_html}
    {top_tweets_html}
  </div>
  <div style="background:#f5f5f5;padding:10px 24px;border:1px solid #e8e8e8;
              border-top:none;border-radius:0 0 8px 8px;text-align:center;">
    <p style="font-size:10px;color:#bbb;margin:0;">
      Powered by Groq (Llama 3.3 70B) · {source_str}
    </p>
  </div>
</body></html>"""


# ── メール送信 ─────────────────────────────────────────────

def send_email(subject: str, html_body: str) -> None:
    msg = MIMEMultipart("alternative")
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
    sgt     = pytz.timezone("Asia/Singapore")
    now_sgt = datetime.now(sgt)
    days_ja = ["月", "火", "水", "木", "金", "土", "日"]
    date_str = now_sgt.strftime(
        f"%Y年%m月%d日（{days_ja[now_sgt.weekday()]}）%H:%M SGT")

    hour       = now_sgt.hour
    time_label = "朝版（6:00 SGT）" if 4 <= hour < 14 else "夕版（18:00 SGT）"

    hours_back = 13
    since_sgt  = now_sgt - timedelta(hours=hours_back)
    since_str  = since_sgt.strftime("%m/%d %H:%M SGT")
    now_str    = now_sgt.strftime("%m/%d %H:%M SGT")

    print(f"=== 金融ソーシャルダイジェスト {date_str} {time_label} ===")

    print("[1/4] X/Twitter 取得中...")
    tweets = fetch_twitter(hours=hours_back)
    print(f"  → {len(tweets)} 件"
          + (" (Twitter API なし)" if not tweets and not TWITTER_BEARER_TOKEN else ""))

    print("[2/4] Reddit 取得中...")
    reddit = fetch_reddit(hours=hours_back)
    print(f"  → {len(reddit)} 件")

    print("[3/4] StockTwits 取得中...")
    st = fetch_stocktwits()
    print(f"  → {len(st)} 件")

    print("[4/4] Groq 要約生成 & メール送信...")
    products_md = summarize_products(tweets, reddit, st, time_label)
    html = build_html(
        products_md, tweets, reddit, st,
        date_str, time_label, since_str, now_str,
        bool(TWITTER_BEARER_TOKEN),
    )
    subject = (f"{'🌅' if '朝' in time_label else '🌆'} 金融ソーシャル"
               f" {now_sgt.strftime('%m/%d(%a)')} {time_label} | 注目商品・TOP10")
    send_email(subject, html)


if __name__ == "__main__":
    main()
