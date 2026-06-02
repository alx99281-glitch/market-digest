#!/usr/bin/env python3
"""
金融ソーシャル＆ニュースダイジェスト

データソース:
  - X/Twitter API v2（Bearer Token 設定時）: 金融ツイート上位10件
  - RSS フィード（常時）: Reuters・Yahoo Finance・CNBC・MarketWatch 等
を毎朝6時・夕方6時に配信。

RSS は認証不要で安定取得。Twitter はエンゲージメント順TOP10を表示。
"""

import os
import re
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import feedparser
import pytz
import requests
from groq import Groq

# ── 設定 ─────────────────────────────────────────────────
GROQ_API_KEY         = os.environ["GROQ_API_KEY"]
GMAIL_USER           = os.environ.get("GMAIL_USER", "alx99281@gmail.com")
GMAIL_APP_PASSWORD   = os.environ["GMAIL_APP_PASSWORD"]
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN", "")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; market-digest-bot/1.0)"}

# ── RSS フィード定義 ───────────────────────────────────────
RSS_FEEDS = {
    "Reuters(EN)":     "https://feeds.reuters.com/reuters/businessNews",
    "CNBC":            "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "WSJ Markets":     "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "MarketWatch":     "https://feeds.marketwatch.com/marketwatch/topstories/",
    "Yahoo Finance":   "https://finance.yahoo.com/news/rssindex",
    "Nikkei(EN)":      "https://www.nikkei.com/rss/rss.aspx?ce=MH",
    "ロイター(日本語)": "https://jp.reuters.com/rssFeed/businessNews",
    "NHK経済":         "https://www.nhk.or.jp/rss/news/cat6.xml",
    "株探":             "https://kabutan.jp/rss/news.xml",
    "Yahoo Finance JP": "https://finance.yahoo.co.jp/rss/category/market",
}

FINANCE_KEYWORDS = [
    "株", "相場", "市場", "円", "ドル", "金利", "債券", "為替", "日銀", "FRB",
    "経済", "GDP", "インフレ", "物価", "日経", "TOPIX", "指数", "原油", "金価格",
    "利上げ", "利下げ", "政策金利", "貿易", "関税", "景気", "不動産", "マンション",
    "REIT", "ETF", "投資", "ファンド", "仮想通貨", "ビットコイン",
    "stock", "market", "bond", "forex", "currency", "rate", "economy",
    "inflation", "Fed", "central bank", "equity", "yield", "treasury",
    "finance", "trade", "tariff", "recession", "oil", "gold", "yen", "dollar",
    "interest rate", "nasdaq", "s&p", "dow", "bitcoin", "crypto", "real estate",
    "NVDA", "TSLA", "AAPL", "SPY", "QQQ",
]


# ── データ取得 ────────────────────────────────────────────

def fetch_rss(hours: int = 13) -> list[dict]:
    """RSS フィードから金融ニュースを取得（認証不要・安定）"""
    cutoff   = datetime.now(timezone.utc) - timedelta(hours=hours)
    articles = []

    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            for entry in feed.entries[:25]:
                pub    = entry.get("published_parsed") or entry.get("updated_parsed")
                pub_dt = datetime(*pub[:6], tzinfo=timezone.utc) if pub else None
                if pub_dt and pub_dt < cutoff:
                    continue

                title   = entry.get("title", "").strip()
                summary = re.sub(r"<[^>]+>", "", entry.get("summary",
                          entry.get("description", ""))).strip()
                link    = entry.get("link", "")

                text     = (title + " " + summary).lower()
                fin_src  = source in ("Yahoo Finance", "Yahoo Finance JP", "株探",
                                      "ロイター(日本語)", "WSJ Markets", "Reuters(EN)")
                has_kw   = any(kw.lower() in text for kw in FINANCE_KEYWORDS)

                if fin_src or has_kw:
                    articles.append({
                        "source":  source,
                        "title":   title,
                        "summary": summary[:300],
                        "link":    link,
                        "date":    pub_dt,
                    })
        except Exception as e:
            print(f"[WARN] RSS {source}: {e}")
        time.sleep(0.2)

    articles.sort(
        key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True)
    print(f"  → RSS {len(articles)} 件")
    return articles[:40]


def fetch_twitter(hours: int = 13) -> list[dict]:
    """X/Twitter API v2 で金融関連ツイートを取得（Bearer Token 必須）"""
    if not TWITTER_BEARER_TOKEN:
        return []

    auth  = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")

    queries = [
        "(株 OR 日経 OR 為替 OR 金利 OR ETF OR 投資信託 OR 債券"
        " OR 不動産 OR マンション OR レバレッジ) lang:ja -is:retweet -is:reply",
        "(stocks OR investing OR markets OR Fed OR SPX OR Nikkei OR ETF"
        " OR leverage OR bond OR forex OR realestate) lang:en -is:retweet -is:reply",
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
                    "tweet.fields": "public_metrics,created_at,lang",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                for t in resp.json().get("data", []):
                    m   = t.get("public_metrics", {})
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
    print(f"  → Twitter {len(tweets)} 件")
    return tweets[:30]


# ── Groq 要約（投資商品セクション） ────────────────────────

def summarize_products(articles: list, tweets: list, time_label: str) -> str:
    """注目の投資商品・投資手法（最大5件）をAI生成"""
    client = Groq(api_key=GROQ_API_KEY)

    body = ""
    if tweets:
        body += "【X (Twitter) 高エンゲージメント投稿】\n"
        for i, t in enumerate(tweets[:15], 1):
            body += f"[tw{i}] ({t['lang']}) {t['text'][:200]}\n"

    if articles:
        body += "\n【最新金融ニュース（RSS）】\n"
        for i, a in enumerate(articles[:25], 1):
            body += f"[{a['source']}] {a['title']}  {a['summary'][:150]}\n"

    if not body:
        return ""

    prompt = f"""あなたは金融ソーシャルメディア・ニュースのアナリストです。
以下の投稿・ニュースデータを分析し、「注目の投資商品・投資手法」セクションのみ
日本語で作成してください。

【ルール】
- 必ず具体的な商品名・ティッカーシンボルで記載すること
  （例: NVIDIA(NVDA)、DRAM ETF(SOXQ)、S&P500(SPY)、ドル円(USDJPY)、
       米10年債、東京都内マンション、米国REIT(VNQ)など）
- 対象: 個別株・ETF・投資信託・債券・為替・仕組み商品・仮想通貨・
       コモディティ・不動産（マンション・商業施設・REIT等）・
       レバレッジ・空売りなどの投資手法
- 言及が少なければ件数を減らしてよい（最大5件）
- 引用符号は不要。商品名と注目理由だけ簡潔に

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


def build_top10_html(tweets: list, articles: list) -> str:
    """TOP10 を直接HTML描画。ツイートがあればツイート、なければRSSニュース。"""
    items_html = ""

    if tweets:
        # ── Twitter モード ──
        section_title = "🔥 注目ツイート TOP10"
        subtitle      = "エンゲージメント順"
        for i, t in enumerate(tweets[:10], 1):
            url      = f"https://x.com/i/web/status/{t['id']}"
            text_esc = _escape(t["text"])
            eng_str  = f"♥{t['likes']:,}&nbsp;&nbsp;RT{t['retweets']:,}"
            lang_tag = (
                f"<span style='background:#e3f2fd;color:#1565c0;"
                f"border-radius:3px;padding:1px 5px;font-size:10px;"
                f"margin-right:6px;'>{_escape(t['lang'].upper())}</span>"
            ) if t.get("lang") else ""
            items_html += f"""
    <div style="border-bottom:1px solid #f0f0f0;padding:10px 0;">
      <div style="font-size:12px;color:#aaa;margin-bottom:4px;">
        {lang_tag}#{i}&nbsp;&nbsp;{eng_str}
      </div>
      <div style="font-size:13px;line-height:1.6;color:#222;">{text_esc}</div>
      <a href="{url}" target="_blank"
         style="font-size:11px;color:#1976d2;text-decoration:none;">
        → X (Twitter) で見る ↗
      </a>
    </div>"""

    elif articles:
        # ── RSS ニュースモード ──
        section_title = "📰 注目金融ニュース TOP10"
        subtitle      = "最新順"
        for i, a in enumerate(articles[:10], 1):
            title_esc = _escape(a["title"])
            src_esc   = _escape(a["source"])
            url       = a.get("link", "")
            date_str  = (a["date"].strftime("%m/%d %H:%M")
                         if a.get("date") else "")
            summary_esc = _escape(a["summary"]) if a.get("summary") else ""
            items_html += f"""
    <div style="border-bottom:1px solid #f0f0f0;padding:10px 0;">
      <div style="font-size:11px;color:#aaa;margin-bottom:3px;">
        <span style="background:#fff3e0;color:#e65100;border-radius:3px;
                     padding:1px 5px;font-size:10px;margin-right:6px;">
          {src_esc}
        </span>#{i}&nbsp;&nbsp;{date_str}
      </div>
      <div style="font-size:13px;font-weight:bold;line-height:1.5;
                  color:#222;margin-bottom:3px;">{title_esc}</div>
      {"<div style='font-size:12px;color:#555;line-height:1.5;'>" + summary_esc + "</div>" if summary_esc else ""}
      {"<a href='" + _escape(url) + "' target='_blank' style='font-size:11px;color:#1976d2;text-decoration:none;'>→ 記事を読む ↗</a>" if url else ""}
    </div>"""

    else:
        return (
            "<p style='color:#aaa;font-size:12px;margin-top:16px;'>"
            "⚠️ データソースへの接続に失敗しました。次回の配信をお待ちください。"
            "</p>"
        )

    return f"""
  <h3 style="color:#b71c1c;margin:20px 0 2px;font-size:15px;">{section_title}</h3>
  <div style="font-size:11px;color:#aaa;margin-bottom:6px;">{subtitle}</div>
  {items_html}"""


def build_html(products_md: str, tweets: list, articles: list,
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

    top10_html   = build_top10_html(tweets, articles)
    source_str   = ("X (Twitter) + RSS" if has_twitter else "RSS（Reuters・CNBC・WSJ 等）")
    icon         = "🌅" if "朝" in time_label else "🌆"

    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:640px;
             margin:0 auto;color:#333;font-size:14px;">
  <div style="background:#1a237e;color:white;padding:16px 24px;
              border-radius:8px 8px 0 0;">
    <div style="font-size:20px;font-weight:bold;">{icon} 金融ダイジェスト</div>
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
    {top10_html}
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

    print(f"=== 金融ダイジェスト {date_str} {time_label} ===")

    print("[1/4] RSS フィード取得中...")
    articles = fetch_rss(hours=hours_back)

    print("[2/4] X/Twitter 取得中...")
    tweets = fetch_twitter(hours=hours_back)
    if not tweets and not TWITTER_BEARER_TOKEN:
        print("  → Twitter API なし（Bearer Token 未設定）")

    print("[3/4] Groq 投資商品サマリー生成中...")
    products_md = summarize_products(articles, tweets, time_label)

    print("[4/4] メール送信中...")
    html = build_html(
        products_md, tweets, articles,
        date_str, time_label, since_str, now_str,
        bool(TWITTER_BEARER_TOKEN),
    )
    subject = (f"{'🌅' if '朝' in time_label else '🌆'} 金融ダイジェスト"
               f" {now_sgt.strftime('%m/%d(%a)')} {time_label} | 注目商品・ニュース")
    send_email(subject, html)


if __name__ == "__main__":
    main()
