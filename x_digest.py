#!/usr/bin/env python3
"""
金融ダイジェスト（RSS + Twitter）

データソース:
  - RSS フィード（常時）: Reuters・Yahoo Finance・CNBC・WSJ・NHK・株探 等
  - X/Twitter API v2（Bearer Token 設定時）

配信構成:
  1. 📈 注目の投資商品・投資手法（最大5件）
  2. 🗾 日本で話題のトピック（5件）
  3. 🌍 海外で話題のトピック（5件）
  4. 📰 注目ニュース / 🔥 注目ツイート TOP10
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

# ── RSS フィード定義（JP / OVERSEAS 分類）────────────────
RSS_FEEDS_JP = {
    "NHK経済":          "https://www.nhk.or.jp/rss/news/cat6.xml",
    "ロイター(日本語)":  "https://jp.reuters.com/rssFeed/businessNews",
    "株探":              "https://kabutan.jp/rss/news.xml",
    "Yahoo Finance JP":  "https://finance.yahoo.co.jp/rss/category/market",
    "Nikkei(EN)":        "https://www.nikkei.com/rss/rss.aspx?ce=MH",
}

RSS_FEEDS_OVERSEAS = {
    "Reuters(EN)":   "https://feeds.reuters.com/reuters/businessNews",
    "CNBC":          "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "WSJ Markets":   "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "MarketWatch":   "https://feeds.marketwatch.com/marketwatch/topstories/",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
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

def _fetch_from_feeds(feeds: dict, hours: int) -> list[dict]:
    """共通 RSS 取得ロジック"""
    cutoff   = datetime.now(timezone.utc) - timedelta(hours=hours)
    articles = []
    for source, url in feeds.items():
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            for entry in feed.entries[:25]:
                pub    = entry.get("published_parsed") or entry.get("updated_parsed")
                pub_dt = datetime(*pub[:6], tzinfo=timezone.utc) if pub else None
                if pub_dt and pub_dt < cutoff:
                    continue
                title   = entry.get("title", "").strip()
                summary = re.sub(r"<[^>]+>", "",
                          entry.get("summary", entry.get("description", ""))).strip()
                link    = entry.get("link", "")
                text    = (title + " " + summary).lower()
                fin_src = True  # 両辞書とも金融特化ソースのみ
                has_kw  = any(kw.lower() in text for kw in FINANCE_KEYWORDS)
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
    return articles[:30]


def fetch_rss(hours: int = 13) -> tuple[list[dict], list[dict]]:
    """日本・海外それぞれの RSS 記事を返す"""
    print("  [JP RSS]")
    jp = _fetch_from_feeds(RSS_FEEDS_JP, hours)
    print(f"    → {len(jp)} 件")
    print("  [OVERSEAS RSS]")
    ov = _fetch_from_feeds(RSS_FEEDS_OVERSEAS, hours)
    print(f"    → {len(ov)} 件")
    return jp, ov


def fetch_twitter(hours: int = 13) -> tuple[list[dict], list[dict]]:
    """X/Twitter API v2 で金融ツイートを取得。lang:ja / lang:en を分けて返す。"""
    if not TWITTER_BEARER_TOKEN:
        return [], []

    auth  = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")

    query_ja = (
        "(株 OR 日経 OR 為替 OR 金利 OR ETF OR 投資信託 OR 債券"
        " OR 不動産 OR マンション OR レバレッジ) lang:ja -is:retweet -is:reply"
    )
    query_en = (
        "(stocks OR investing OR markets OR Fed OR SPX OR Nikkei OR ETF"
        " OR leverage OR bond OR forex OR realestate) lang:en -is:retweet -is:reply"
    )

    def _query(query: str) -> list[dict]:
        out = []
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
                    out.append({
                        "id":         t["id"],
                        "text":       t["text"],
                        "engagement": eng,
                        "lang":       t.get("lang", ""),
                        "likes":      m.get("like_count", 0),
                        "retweets":   m.get("retweet_count", 0),
                    })
            elif resp.status_code == 429:
                print("[WARN] Twitter rate limit")
            else:
                print(f"[WARN] Twitter API {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"[WARN] Twitter fetch error: {e}")
        out.sort(key=lambda x: x["engagement"], reverse=True)
        return out[:15]

    tw_ja = _query(query_ja)
    time.sleep(1)
    tw_en = _query(query_en)

    print(f"  → Twitter JA:{len(tw_ja)} EN:{len(tw_en)} 件")
    return tw_ja, tw_en


# ── Groq 要約（3セクション） ──────────────────────────────

def summarize_all(jp_articles: list, ov_articles: list,
                  tw_ja: list, tw_en: list) -> str:
    """
    3セクションを一度に生成:
      1. 📈 注目の投資商品・投資手法（最大5件）
      2. 🗾 日本で話題のトピック（5件）
      3. 🌍 海外で話題のトピック（5件）
    """
    client = Groq(api_key=GROQ_API_KEY)

    body = ""

    # ── 日本ソース ──
    if tw_ja:
        body += "【X/Twitter 日本語ツイート】\n"
        for t in tw_ja[:10]:
            body += f"  {t['text'][:180]}\n"
    if jp_articles:
        body += "【日本RSSニュース】\n"
        for a in jp_articles[:15]:
            body += f"  [{a['source']}] {a['title']}  {a['summary'][:120]}\n"

    body += "\n"

    # ── 海外ソース ──
    if tw_en:
        body += "【X/Twitter 英語ツイート】\n"
        for t in tw_en[:10]:
            body += f"  {t['text'][:180]}\n"
    if ov_articles:
        body += "【海外RSSニュース】\n"
        for a in ov_articles[:15]:
            body += f"  [{a['source']}] {a['title']}  {a['summary'][:120]}\n"

    if not body.strip():
        return ""

    prompt = f"""あなたは金融メディアのアナリストです。
以下の日本・海外の投稿・ニュースデータを分析し、
下記の3セクションを日本語で作成してください。

【共通ルール】
- 投資商品は必ず具体的な商品名・ティッカーを使う
  （NVIDIA(NVDA)、DRAM ETF(SOXQ)、S&P500(SPY)、ドル円(USDJPY)、
   米10年債、東京マンション、米国REIT(VNQ) など）
- 商品・トピック名は太字（**名前**）で書く
- 各項目1〜2文で簡潔に

【出力形式（3セクションのみ出力、余分なテキスト不要）】

## 📈 注目の投資商品・投資手法（最大5件）
1. **[商品名・ティッカー]** — 注目理由

## 🗾 日本で話題のトピック（5件）
1. **[トピック名]** — 内容を1〜2文

## 🌍 海外で話題のトピック（5件）
1. **[トピック名]** — 内容を1〜2文

---
{body}
"""
    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
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


def md_to_html(md: str) -> str:
    """Markdown 3セクションを HTML に変換"""
    h = _escape(md)
    # セクションヘッダー
    h = re.sub(r"## (📈[^\n]*)",
               r"<h3 style='color:#2e7d32;margin:20px 0 4px;font-size:15px;'>\1</h3>", h)
    h = re.sub(r"## (🗾[^\n]*)",
               r"<h3 style='color:#c62828;margin:20px 0 4px;font-size:15px;'>\1</h3>", h)
    h = re.sub(r"## (🌍[^\n]*)",
               r"<h3 style='color:#1565c0;margin:20px 0 4px;font-size:15px;'>\1</h3>", h)
    h = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", h)
    h = re.sub(r"^(\d+)\. ", r"<br>&nbsp;\1. ", h, flags=re.MULTILINE)
    h = h.replace("\n---\n", "").replace("\n", "<br>")
    return h


def build_top10_html(tw_ja: list, tw_en: list,
                     jp_articles: list, ov_articles: list) -> str:
    """TOP10 を直接HTML描画。ツイートがあればツイート、なければRSS。"""

    # ツイートがあれば JA + EN を合わせてエンゲージメント順
    all_tweets = sorted(tw_ja + tw_en,
                        key=lambda x: x["engagement"], reverse=True)

    if all_tweets:
        section_title = "🔥 注目ツイート TOP10"
        subtitle      = "エンゲージメント順"
        items_html    = ""
        for i, t in enumerate(all_tweets[:10], 1):
            url      = f"https://x.com/i/web/status/{t['id']}"
            text_esc = _escape(t["text"])
            eng_str  = f"♥{t['likes']:,}&nbsp;&nbsp;RT{t['retweets']:,}"
            lang     = t.get("lang", "").upper()
            lang_bg  = "#e3f2fd" if lang == "EN" else "#fce4ec"
            lang_fg  = "#1565c0" if lang == "EN" else "#c62828"
            lang_tag = (f"<span style='background:{lang_bg};color:{lang_fg};"
                        f"border-radius:3px;padding:1px 5px;font-size:10px;"
                        f"margin-right:6px;'>{_escape(lang)}</span>") if lang else ""
            items_html += f"""
    <div style="border-bottom:1px solid #f0f0f0;padding:10px 0;">
      <div style="font-size:12px;color:#aaa;margin-bottom:4px;">
        {lang_tag}#{i}&nbsp;&nbsp;{eng_str}
      </div>
      <div style="font-size:13px;line-height:1.6;color:#222;">{text_esc}</div>
      <a href="{url}" target="_blank"
         style="font-size:11px;color:#1976d2;text-decoration:none;">
        → X で見る ↗
      </a>
    </div>"""

    else:
        # RSS フォールバック（日本・海外交互に並べる）
        all_articles = []
        jp_q = list(jp_articles[:5])
        ov_q = list(ov_articles[:5])
        while jp_q or ov_q:
            if jp_q:
                all_articles.append(jp_q.pop(0))
            if ov_q:
                all_articles.append(ov_q.pop(0))
        all_articles = all_articles[:10]

        if not all_articles:
            return ("<p style='color:#aaa;font-size:12px;margin-top:16px;'>"
                    "⚠️ データソースへの接続に失敗しました。"
                    "次回の配信をお待ちください。</p>")

        section_title = "📰 注目金融ニュース TOP10"
        subtitle      = "最新順（日本・海外）"
        items_html    = ""
        for i, a in enumerate(all_articles, 1):
            title_esc   = _escape(a["title"])
            src_esc     = _escape(a["source"])
            url         = a.get("link", "")
            date_str    = (a["date"].strftime("%m/%d %H:%M")
                           if a.get("date") else "")
            summary_esc = _escape(a.get("summary", ""))
            link_html   = (f"<a href='{_escape(url)}' target='_blank' "
                           f"style='font-size:11px;color:#1976d2;"
                           f"text-decoration:none;'>→ 記事を読む ↗</a>"
                           if url else "")
            items_html += f"""
    <div style="border-bottom:1px solid #f0f0f0;padding:10px 0;">
      <div style="font-size:11px;color:#aaa;margin-bottom:3px;">
        <span style="background:#fff3e0;color:#e65100;border-radius:3px;
                     padding:1px 5px;font-size:10px;margin-right:6px;">{src_esc}</span>
        #{i}&nbsp;&nbsp;{date_str}
      </div>
      <div style="font-size:13px;font-weight:bold;line-height:1.5;
                  color:#222;margin-bottom:3px;">{title_esc}</div>
      {"<div style='font-size:12px;color:#555;line-height:1.5;'>" + summary_esc + "</div>" if summary_esc else ""}
      {link_html}
    </div>"""

    return (f"<h3 style='color:#b71c1c;margin:20px 0 2px;font-size:15px;'>"
            f"{section_title}</h3>"
            f"<div style='font-size:11px;color:#aaa;margin-bottom:6px;'>{subtitle}</div>"
            f"{items_html}")


def build_html(summary_md: str, tw_ja: list, tw_en: list,
               jp_articles: list, ov_articles: list,
               date_str: str, time_label: str,
               since_str: str, now_str: str,
               has_twitter: bool) -> str:

    summary_html = md_to_html(summary_md) if summary_md else ""
    top10_html   = build_top10_html(tw_ja, tw_en, jp_articles, ov_articles)
    source_str   = ("X (Twitter) + RSS" if has_twitter
                    else "RSS（Reuters・CNBC・WSJ・NHK 等）")
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
    {summary_html}
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

def build_plain_text(summary_md: str, jp_articles: list,
                     ov_articles: list) -> str:
    """スパム判定対策用プレーンテキストパート"""
    lines = []
    if summary_md:
        # Markdown の装飾を除去
        plain = re.sub(r"\*\*(.*?)\*\*", r"\1", summary_md)
        plain = re.sub(r"^## ", "", plain, flags=re.MULTILINE)
        lines.append(plain)
    lines.append("\n--- 注目ニュース ---")
    for a in (jp_articles + ov_articles)[:10]:
        lines.append(f"[{a['source']}] {a['title']}")
        if a.get("link"):
            lines.append(f"  {a['link']}")
    return "\n".join(lines)


def send_email(subject: str, html_body: str, plain_body: str = "") -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = GMAIL_USER
    # plain text を先に attach（スパムフィルタ対策）
    msg.attach(MIMEText(plain_body or subject, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, GMAIL_USER, msg.as_string())
    print(f"メール送信完了: {subject}")


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

    try:
        print("[1/4] RSS フィード取得中...")
        jp_articles, ov_articles = fetch_rss(hours=hours_back)

        print("[2/4] X/Twitter 取得中...")
        tw_ja, tw_en = fetch_twitter(hours=hours_back)
        if not (tw_ja or tw_en) and not TWITTER_BEARER_TOKEN:
            print("  → Twitter API なし（Bearer Token 未設定）")

        print("[3/4] Groq サマリー生成中...")
        summary_md = summarize_all(jp_articles, ov_articles, tw_ja, tw_en)
        print(f"  → {len(summary_md)} 文字")

        print("[4/4] メール送信中...")
        html  = build_html(
            summary_md, tw_ja, tw_en, jp_articles, ov_articles,
            date_str, time_label, since_str, now_str,
            bool(TWITTER_BEARER_TOKEN),
        )
        plain = build_plain_text(summary_md, jp_articles, ov_articles)
        subject = (f"{'🌅' if '朝' in time_label else '🌆'} 金融ダイジェスト"
                   f" {now_sgt.strftime('%m/%d(%a)')} {time_label}")
        send_email(subject, html, plain)

    except Exception as e:
        import traceback
        print(f"[ERROR] 未処理の例外: {e}")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
