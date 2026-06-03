#!/usr/bin/env python3
"""
金融ツイートダイジェスト

直近24時間のエンゲージメント上位ツイートを
  1. 📈 注目の投資商品・投資手法（最大5件）
  2. 🗾 日本で話題のトピック（5件）
  3. 🌍 海外で話題のトピック（5件）
  4. 🔥 注目ツイート TOP10（リンク付き）
にまとめて朝6時・夕18時（SGT）に送信する。

Twitter Bearer Token 未設定時は RSS フィードにフォールバック。
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

# ── RSS フォールバック用フィード ───────────────────────────
RSS_FEEDS = {
    "NHK経済":          "https://www.nhk.or.jp/rss/news/cat6.xml",
    "ロイター(日本語)":  "https://jp.reuters.com/rssFeed/businessNews",
    "株探":              "https://kabutan.jp/rss/news.xml",
    "Reuters(EN)":       "https://feeds.reuters.com/reuters/businessNews",
    "CNBC":              "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "WSJ Markets":       "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "Yahoo Finance":     "https://finance.yahoo.com/news/rssindex",
}

JP_SOURCES  = {"NHK経済", "ロイター(日本語)", "株探"}
OV_SOURCES  = {"Reuters(EN)", "CNBC", "WSJ Markets", "Yahoo Finance"}


# ── Twitter 取得 ──────────────────────────────────────────

def fetch_twitter(hours: int = 24) -> tuple[list[dict], list[dict]]:
    """
    直近 hours 時間の金融ツイートをエンゲージメント順で返す。
    Returns: (ja_tweets, en_tweets)  各最大20件
    """
    if not TWITTER_BEARER_TOKEN:
        return [], []

    auth  = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")

    queries = {
        "ja": ("(株 OR 日経 OR 為替 OR 金利 OR ETF OR 投資信託 OR 債券"
               " OR 不動産 OR マンション OR REIT OR 仮想通貨 OR ビットコイン)"
               " lang:ja -is:retweet -is:reply"),
        "en": ("(stocks OR investing OR markets OR Fed OR SPX OR Nikkei"
               " OR ETF OR bond OR forex OR crypto OR bitcoin OR realestate)"
               " lang:en -is:retweet -is:reply"),
    }

    results = {}
    for lang, query in queries.items():
        tweets = []
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
                        "lang":       t.get("lang", lang),
                        "likes":      m.get("like_count", 0),
                        "retweets":   m.get("retweet_count", 0),
                    })
                tweets.sort(key=lambda x: x["engagement"], reverse=True)
                print(f"  → Twitter {lang.upper()}: {len(tweets)} 件")
            elif resp.status_code == 429:
                print(f"[WARN] Twitter rate limit ({lang})")
            else:
                print(f"[WARN] Twitter {lang} {resp.status_code}: {resp.text[:150]}")
        except Exception as e:
            print(f"[WARN] Twitter {lang}: {e}")
        results[lang] = tweets[:20]
        time.sleep(1)

    return results.get("ja", []), results.get("en", [])


# ── RSS フォールバック ─────────────────────────────────────

def fetch_rss_fallback(hours: int = 24) -> tuple[list[dict], list[dict]]:
    """Twitter 取得不可時に RSS から日本・海外記事を返す"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    jp, ov = [], []

    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url,
                       request_headers={"User-Agent": "Mozilla/5.0"})
            for entry in feed.entries[:20]:
                pub    = entry.get("published_parsed") or entry.get("updated_parsed")
                pub_dt = datetime(*pub[:6], tzinfo=timezone.utc) if pub else None
                if pub_dt and pub_dt < cutoff:
                    continue
                item = {
                    "source":  source,
                    "title":   entry.get("title", "").strip(),
                    "summary": re.sub(r"<[^>]+>", "",
                               entry.get("summary",
                               entry.get("description", ""))).strip()[:200],
                    "link":    entry.get("link", ""),
                    "date":    pub_dt,
                }
                if source in JP_SOURCES:
                    jp.append(item)
                else:
                    ov.append(item)
        except Exception as e:
            print(f"[WARN] RSS {source}: {e}")
        time.sleep(0.2)

    jp.sort(key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    ov.sort(key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    print(f"  → RSS JP:{len(jp)} OV:{len(ov)} 件")
    return jp[:20], ov[:20]


# ── Groq 要約 ─────────────────────────────────────────────

def summarize(tw_ja: list, tw_en: list,
              jp_articles: list, ov_articles: list) -> str:
    """
    ツイート（あれば優先）またはRSS記事から3セクションを生成。
    ## 📈 注目の投資商品・投資手法
    ## 🗾 日本で話題のトピック
    ## 🌍 海外で話題のトピック
    """
    client = Groq(api_key=GROQ_API_KEY)

    body = ""
    use_twitter = bool(tw_ja or tw_en)

    if tw_ja:
        body += "【日本語ツイート（エンゲージメント順）】\n"
        for i, t in enumerate(tw_ja[:15], 1):
            body += f"{i}. {t['text'][:200]}  (♥{t['likes']} RT{t['retweets']})\n"
        body += "\n"

    if tw_en:
        body += "【英語ツイート（エンゲージメント順）】\n"
        for i, t in enumerate(tw_en[:15], 1):
            body += f"{i}. {t['text'][:200]}  (♥{t['likes']} RT{t['retweets']})\n"
        body += "\n"

    if jp_articles:
        body += "【日本ニュース（RSS）】\n"
        for a in jp_articles[:15]:
            body += f"- [{a['source']}] {a['title']}  {a['summary'][:120]}\n"
        body += "\n"

    if ov_articles:
        body += "【海外ニュース（RSS）】\n"
        for a in ov_articles[:15]:
            body += f"- [{a['source']}] {a['title']}  {a['summary'][:120]}\n"
        body += "\n"

    if not body.strip():
        return ""

    src_desc = "X/Twitterのツイート" if use_twitter else "金融ニュース"

    prompt = f"""あなたは金融メディアのアナリストです。
以下の直近24時間の{src_desc}データをもとに、3つのセクションを日本語で作成してください。

【ルール】
- 投資商品は必ず具体的な商品名・ティッカーで書く
  （例: NVIDIA(NVDA)、S&P500(SPY)、ドル円(USDJPY)、米10年債、東京マンション、Bitcoin(BTC)）
- 「日本で話題」は日本語データ、「海外で話題」は英語データから優先的に抽出
- 各項目は太字（**名前**）＋1〜2文の説明
- 余分なテキストや前置きは不要

【出力形式（この3セクションのみ）】

## 📈 注目の投資商品・投資手法（最大5件）
1. **[商品名・ティッカー]** — 注目理由

## 🗾 日本で話題のトピック（5件）
1. **[トピック名]** — 内容

## 🌍 海外で話題のトピック（5件）
1. **[トピック名]** — 内容

---
{body}"""

    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
        )
        result = resp.choices[0].message.content
        print(f"  → Groq: {len(result)} 文字")
        return result
    except Exception as e:
        print(f"[WARN] Groq error: {e}")
        return ""


# ── HTML 生成 ──────────────────────────────────────────────

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def md_to_html(md: str) -> str:
    h = _esc(md)
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
                     jp_rss: list, ov_rss: list) -> str:
    """ツイートがあればTOP10ツイート、なければRSSニュースTOP10"""
    all_tweets = sorted(tw_ja + tw_en,
                        key=lambda x: x["engagement"], reverse=True)

    if all_tweets:
        title    = "🔥 注目ツイート TOP10"
        subtitle = "直近24時間・エンゲージメント順"
        rows = ""
        for i, t in enumerate(all_tweets[:10], 1):
            url     = f"https://x.com/i/web/status/{t['id']}"
            lang    = t.get("lang", "").upper()
            bg, fg  = ("#fce4ec", "#c62828") if lang == "JA" else ("#e3f2fd", "#1565c0")
            tag     = (f"<span style='background:{bg};color:{fg};border-radius:3px;"
                       f"padding:1px 6px;font-size:10px;margin-right:6px;'>{_esc(lang)}</span>"
                       ) if lang else ""
            rows += f"""
      <div style="border-bottom:1px solid #f0f0f0;padding:10px 0;">
        <div style="font-size:11px;color:#aaa;margin-bottom:3px;">
          {tag}#{i}&nbsp; ♥{t['likes']:,}&nbsp; RT{t['retweets']:,}
        </div>
        <div style="font-size:13px;line-height:1.6;color:#222;">{_esc(t['text'])}</div>
        <a href="{url}" target="_blank"
           style="font-size:11px;color:#1976d2;text-decoration:none;">
          → X で見る ↗
        </a>
      </div>"""

    else:
        title    = "📰 注目金融ニュース TOP10"
        subtitle = "直近24時間・最新順"
        # 日本・海外を交互に
        merged, qi, qo = [], list(jp_rss[:5]), list(ov_rss[:5])
        while qi or qo:
            if qi: merged.append(qi.pop(0))
            if qo: merged.append(qo.pop(0))
        merged = merged[:10]

        if not merged:
            return ("<p style='color:#aaa;font-size:12px;margin-top:16px;'>"
                    "⚠️ データ取得に失敗しました。次回の配信をお待ちください。</p>")

        rows = ""
        for i, a in enumerate(merged, 1):
            date_s = a["date"].strftime("%m/%d %H:%M") if a.get("date") else ""
            lnk    = (f"<a href='{_esc(a['link'])}' target='_blank' "
                      f"style='font-size:11px;color:#1976d2;text-decoration:none;'>"
                      f"→ 記事を読む ↗</a>") if a.get("link") else ""
            rows += f"""
      <div style="border-bottom:1px solid #f0f0f0;padding:10px 0;">
        <div style="font-size:11px;color:#aaa;margin-bottom:3px;">
          <span style="background:#fff3e0;color:#e65100;border-radius:3px;
                       padding:1px 5px;font-size:10px;margin-right:6px;">
            {_esc(a['source'])}</span>#{i}&nbsp;{date_s}
        </div>
        <div style="font-size:13px;font-weight:bold;line-height:1.5;color:#222;">
          {_esc(a['title'])}</div>
        {"<div style='font-size:12px;color:#555;'>" + _esc(a['summary']) + "</div>" if a.get("summary") else ""}
        {lnk}
      </div>"""

    return (f"<h3 style='color:#b71c1c;margin:20px 0 2px;font-size:15px;'>{title}</h3>"
            f"<div style='font-size:11px;color:#aaa;margin-bottom:4px;'>{subtitle}</div>"
            f"{rows}")


def build_html(summary_md: str, tw_ja: list, tw_en: list,
               jp_rss: list, ov_rss: list,
               date_str: str, time_label: str,
               since_str: str, now_str: str,
               has_twitter: bool) -> str:

    body_html  = md_to_html(summary_md) if summary_md else ""
    top10_html = build_top10_html(tw_ja, tw_en, jp_rss, ov_rss)
    src_note   = "X (Twitter)" if has_twitter else "RSS（Reuters・CNBC・WSJ・NHK 等）"
    icon       = "🌅" if "朝" in time_label else "🌆"

    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:640px;
             margin:0 auto;color:#333;font-size:14px;">
  <div style="background:#1a237e;color:white;padding:16px 24px;border-radius:8px 8px 0 0;">
    <div style="font-size:20px;font-weight:bold;">{icon} 金融ダイジェスト</div>
    <div style="font-size:12px;opacity:.85;margin-top:4px;">{date_str}　{time_label}</div>
    <div style="font-size:11px;opacity:.7;margin-top:3px;">
      集計期間: {since_str} 〜 {now_str} ／ ソース: {src_note}
    </div>
  </div>
  <div style="background:#fff;padding:20px 24px;border:1px solid #e8e8e8;
              border-top:none;line-height:1.85;">
    {body_html}
    {top10_html}
  </div>
  <div style="background:#f5f5f5;padding:10px 24px;border:1px solid #e8e8e8;
              border-top:none;border-radius:0 0 8px 8px;text-align:center;">
    <p style="font-size:10px;color:#bbb;margin:0;">
      Powered by Groq (Llama 3.3 70B) · {src_note}
    </p>
  </div>
</body></html>"""


# ── メール送信 ─────────────────────────────────────────────

def send_email(subject: str, html_body: str, plain_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = GMAIL_USER
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

    print(f"=== 金融ダイジェスト {date_str} {time_label} ===")

    try:
        print("[1/4] Twitter 取得中（直近24h）...")
        tw_ja, tw_en = fetch_twitter(hours=hours_back)

        jp_rss, ov_rss = [], []
        if not (tw_ja or tw_en):
            print("[1b] Twitter 取得なし → RSS フォールバック...")
            jp_rss, ov_rss = fetch_rss_fallback(hours=hours_back)

        print("[2/4] Groq サマリー生成中...")
        summary_md = summarize(tw_ja, tw_en, jp_rss, ov_rss)

        print("[3/4] HTML 生成中...")
        html = build_html(
            summary_md, tw_ja, tw_en, jp_rss, ov_rss,
            date_str, time_label, since_str, now_str,
            bool(tw_ja or tw_en),
        )

        # プレーンテキスト（スパムフィルタ対策）
        plain = re.sub(r"\*\*(.*?)\*\*", r"\1",
                re.sub(r"^## ", "", summary_md, flags=re.MULTILINE)) if summary_md else subject

        print("[4/4] メール送信中...")
        subject = (f"{'🌅' if '朝' in time_label else '🌆'} 金融ダイジェスト"
                   f" {now_sgt.strftime('%m/%d(%a)')} {time_label}")
        send_email(subject, html, plain)

    except Exception as e:
        import traceback
        print(f"[ERROR] {e}")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
