#!/usr/bin/env python3
"""
金融ツイートダイジェスト

直近24時間のエンゲージメント上位ツイートをそのまま表示する。
  🗾 日本語ツイート TOP5
  🌍 英語ツイート TOP5
  🔥 全体 TOP10

Twitter Bearer Token 未設定時は RSS ニュースにフォールバック。
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

# ── 設定 ─────────────────────────────────────────────────
GMAIL_USER           = os.environ.get("GMAIL_USER", "alx99281@gmail.com")
GMAIL_APP_PASSWORD   = os.environ["GMAIL_APP_PASSWORD"]
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN", "")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; market-digest-bot/1.0)"}

RSS_FEEDS = {
    "NHK経済":         "https://www.nhk.or.jp/rss/news/cat6.xml",
    "ロイター(日本語)": "https://jp.reuters.com/rssFeed/businessNews",
    "株探":             "https://kabutan.jp/rss/news.xml",
    "Reuters(EN)":     "https://feeds.reuters.com/reuters/businessNews",
    "CNBC":            "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "WSJ Markets":     "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "Yahoo Finance":   "https://finance.yahoo.com/news/rssindex",
}
RSS_JP = {"NHK経済", "ロイター(日本語)", "株探"}

# 投資商品・ティッカー検出キーワード（大文字小文字不問）
PRODUCT_KEYWORDS = [
    # 個別株・ティッカー
    "nvda", "nvidia", "tsla", "tesla", "aapl", "apple", "msft", "microsoft",
    "amzn", "amazon", "meta", "googl", "google", "7203", "7974", "softbank",
    "ソフトバンク", "トヨタ", "ソニー",
    # 指数・ETF
    "s&p", "s&p500", "spy", "qqq", "nasdaq", "dow", "日経", "nikkei", "topix",
    "etf", "投資信託",
    # 債券・金利
    "10年債", "米国債", "treasury", "bond", "yield", "金利",
    # 為替
    "ドル円", "usdjpy", "eurusd", "ユーロ", "為替", "fx",
    # コモディティ
    "gold", "金価格", "原油", "oil", "wti", "copper", "銅",
    # 仮想通貨
    "bitcoin", "btc", "ethereum", "eth", "crypto", "仮想通貨", "ビットコイン",
    # 不動産
    "reit", "不動産", "マンション", "real estate",
    # 投資手法
    "レバレッジ", "空売り", "leverage", "short",
]


# ── Twitter 取得 ──────────────────────────────────────────

def fetch_twitter(hours: int = 24) -> tuple[list[dict], list[dict]]:
    """直近 hours 時間の金融ツイートを取得。(ja, en) に分けて返す。"""
    if not TWITTER_BEARER_TOKEN:
        return [], []

    auth  = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")

    queries = {
        "ja": ("(株 OR 日経 OR 為替 OR 金利 OR ETF OR 投資信託 OR 債券"
               " OR 不動産 OR マンション OR REIT OR 仮想通貨 OR ビットコイン"
               " OR レバレッジ OR 空売り) lang:ja -is:retweet -is:reply"),
        "en": ("(stocks OR investing OR markets OR Fed OR SPX OR Nikkei"
               " OR ETF OR bond OR forex OR crypto OR bitcoin OR realestate"
               " OR earnings OR inflation) lang:en -is:retweet -is:reply"),
    }

    results: dict[str, list] = {}
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
                    "tweet.fields": "public_metrics,created_at,lang,author_id",
                    "expansions":   "author_id",
                    "user.fields":  "username,name",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                body = resp.json()
                # author_id → username マップを構築
                user_map = {
                    u["id"]: {"username": u.get("username", ""),
                              "name":     u.get("name", "")}
                    for u in body.get("includes", {}).get("users", [])
                }
                for t in body.get("data", []):
                    m = t.get("public_metrics", {})
                    eng = (m.get("like_count", 0)
                           + m.get("retweet_count", 0) * 2
                           + m.get("reply_count", 0))
                    author = user_map.get(t.get("author_id", ""), {})
                    tweets.append({
                        "id":          t["id"],
                        "text":        t["text"],
                        "eng":         eng,
                        "likes":       m.get("like_count", 0),
                        "retweets":    m.get("retweet_count", 0),
                        "lang":        lang,
                        "username":    author.get("username", ""),
                        "name":        author.get("name", ""),
                    })
                tweets.sort(key=lambda x: x["eng"], reverse=True)
                print(f"  Twitter {lang.upper()}: {len(tweets)} 件")
            elif resp.status_code == 429:
                print(f"[WARN] Twitter rate limit ({lang})")
            else:
                print(f"[WARN] Twitter {lang} {resp.status_code}: {resp.text[:120]}")
        except Exception as e:
            print(f"[WARN] Twitter {lang}: {e}")
        results[lang] = tweets[:20]
        time.sleep(1)

    return results.get("ja", []), results.get("en", [])


# ── RSS フォールバック ─────────────────────────────────────

def fetch_rss(hours: int = 24) -> tuple[list[dict], list[dict]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    jp, ov = [], []
    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url,
                       request_headers={"User-Agent": "Mozilla/5.0"})
            for e in feed.entries[:20]:
                pub = e.get("published_parsed") or e.get("updated_parsed")
                pub_dt = datetime(*pub[:6], tzinfo=timezone.utc) if pub else None
                if pub_dt and pub_dt < cutoff:
                    continue
                item = {
                    "source":  source,
                    "title":   e.get("title", "").strip(),
                    "summary": re.sub(r"<[^>]+>", "",
                               e.get("summary", e.get("description", ""))).strip()[:200],
                    "link":    e.get("link", ""),
                    "date":    pub_dt,
                }
                (jp if source in RSS_JP else ov).append(item)
        except Exception as ex:
            print(f"[WARN] RSS {source}: {ex}")
        time.sleep(0.2)

    srt = lambda lst: sorted(
        lst, key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True)
    jp, ov = srt(jp)[:20], srt(ov)[:20]
    print(f"  RSS JP:{len(jp)} OV:{len(ov)} 件")
    return jp, ov


# ── HTML パーツ ───────────────────────────────────────────

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def tweet_card(t: dict, rank: int) -> str:
    url      = f"https://x.com/i/web/status/{t['id']}"
    lang     = t.get("lang", "").upper()
    bg       = "#fce4ec" if lang == "JA" else "#e3f2fd"
    fg       = "#c62828" if lang == "JA" else "#1565c0"
    lang_tag = (f"<span style='background:{bg};color:{fg};border-radius:3px;"
                f"padding:1px 6px;font-size:10px;margin-right:6px;'>{lang}</span>"
                ) if lang else ""
    username = t.get("username", "")
    name     = t.get("name", "")
    author_html = ""
    if username:
        profile_url = f"https://x.com/{username}"
        author_html = (
            f"<a href='{profile_url}' target='_blank' "
            f"style='color:#555;text-decoration:none;font-weight:bold;'>"
            f"{_esc(name)}</a>"
            f"<span style='color:#aaa;margin-left:4px;'>@{_esc(username)}</span>"
        )
    return f"""
<div style="border-bottom:1px solid #f0f0f0;padding:10px 0;">
  <div style="font-size:11px;color:#aaa;margin-bottom:4px;">
    {lang_tag}#{rank}&nbsp;&nbsp;♥{t['likes']:,}&nbsp;&nbsp;RT{t['retweets']:,}
  </div>
  {"<div style='font-size:12px;margin-bottom:4px;'>" + author_html + "</div>" if author_html else ""}
  <div style="font-size:13px;line-height:1.65;color:#222;">{_esc(t['text'])}</div>
  <a href="{url}" target="_blank"
     style="font-size:11px;color:#1976d2;text-decoration:none;display:inline-block;margin-top:3px;">
    → X で見る ↗
  </a>
</div>"""


def rss_card(a: dict, rank: int) -> str:
    date_s = a["date"].strftime("%m/%d %H:%M") if a.get("date") else ""
    lnk    = (f"<a href='{_esc(a['link'])}' target='_blank' "
              f"style='font-size:11px;color:#1976d2;text-decoration:none;'>"
              f"→ 記事を読む ↗</a>") if a.get("link") else ""
    return f"""
<div style="border-bottom:1px solid #f0f0f0;padding:10px 0;">
  <div style="font-size:11px;color:#aaa;margin-bottom:3px;">
    <span style="background:#fff3e0;color:#e65100;border-radius:3px;
                 padding:1px 5px;font-size:10px;margin-right:6px;">{_esc(a['source'])}</span>
    #{rank}&nbsp;{date_s}
  </div>
  <div style="font-size:13px;font-weight:bold;line-height:1.5;color:#222;">{_esc(a['title'])}</div>
  {"<div style='font-size:12px;color:#555;margin-top:2px;'>" + _esc(a.get('summary','')) + "</div>" if a.get('summary') else ""}
  {lnk}
</div>"""


def section(icon: str, title: str, subtitle: str, cards_html: str,
            title_color: str = "#b71c1c") -> str:
    return (f"<h3 style='color:{title_color};margin:22px 0 2px;font-size:15px;'>"
            f"{icon} {title}</h3>"
            f"<div style='font-size:11px;color:#aaa;margin-bottom:4px;'>{subtitle}</div>"
            f"{cards_html}")


def filter_product_tweets(tweets: list, top_n: int = 5) -> list[dict]:
    """投資商品・ティッカーに言及しているツイートをエンゲージメント順で返す"""
    matched = []
    for t in tweets:
        text_lower = t["text"].lower()
        if any(kw in text_lower for kw in PRODUCT_KEYWORDS):
            matched.append(t)
    # エンゲージメント順（既にソート済みのはずだが念のため）
    matched.sort(key=lambda x: x["eng"], reverse=True)
    return matched[:top_n]


def build_html(tw_ja: list, tw_en: list,
               date_str: str, time_label: str,
               since_str: str, now_str: str) -> str:

    icon_hdr = "🌅" if "朝" in time_label else "🌆"
    sections = []

    if tw_ja or tw_en:
        # 📈 投資商品 TOP5（JA+EN合算からキーワードフィルタ）
        product_tw = filter_product_tweets(
            sorted(tw_ja + tw_en, key=lambda x: x["eng"], reverse=True)
        )
        if product_tw:
            cards = "".join(tweet_card(t, i) for i, t in enumerate(product_tw, 1))
            sections.append(section("📈", "注目の投資商品",
                                    "商品・ティッカー言及ツイート・エンゲージメント順",
                                    cards, "#2e7d32"))

        # 🗾 日本語 TOP5
        if tw_ja:
            cards = "".join(tweet_card(t, i) for i, t in enumerate(tw_ja[:5], 1))
            sections.append(section("🗾", "日本で話題", "日本語ツイート・エンゲージメント順",
                                    cards, "#c62828"))

        # 🌍 英語 TOP5
        if tw_en:
            cards = "".join(tweet_card(t, i) for i, t in enumerate(tw_en[:5], 1))
            sections.append(section("🌍", "海外で話題", "英語ツイート・エンゲージメント順",
                                    cards, "#1565c0"))

        # 🔥 全体 TOP10
        all_tw = sorted(tw_ja + tw_en, key=lambda x: x["eng"], reverse=True)
        if all_tw:
            cards = "".join(tweet_card(t, i) for i, t in enumerate(all_tw[:10], 1))
            sections.append(section("🔥", "総合ランキング TOP10",
                                    "JA + EN 合算・エンゲージメント順", cards, "#b71c1c"))

        body_html = "\n".join(sections)
    else:
        # ツイート取得失敗 — RSSには逃がさずエラーを明示
        body_html = """
        <div style="background:#fff3e0;border-left:4px solid #ff9800;
                    padding:16px;border-radius:4px;margin-top:16px;">
          <div style="font-size:14px;font-weight:bold;color:#e65100;margin-bottom:8px;">
            ⚠️ X (Twitter) のツイートデータを取得できませんでした
          </div>
          <div style="font-size:13px;color:#555;line-height:1.7;">
            考えられる原因:<br>
            &nbsp;・Twitter API の月間レート制限に達した<br>
            &nbsp;・Bearer Token の権限が不足（Basic プラン以上が必要）<br>
            &nbsp;・Twitter API の一時的な障害<br><br>
            次回の配信まで自動的に再試行します。
          </div>
        </div>"""

    src_note = "X (Twitter) · 直近24h" if (tw_ja or tw_en) else "X (Twitter) · データ取得失敗"
    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:640px;
             margin:0 auto;color:#333;font-size:14px;">
  <div style="background:#1a237e;color:white;padding:16px 24px;border-radius:8px 8px 0 0;">
    <div style="font-size:20px;font-weight:bold;">{icon_hdr} 金融ツイートダイジェスト</div>
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
    subject    = (f"{'🌅' if '朝' in time_label else '🌆'} 金融ダイジェスト"
                  f" {now_sgt.strftime('%m/%d(%a)')} {time_label}")

    print(f"=== {subject} ===")

    try:
        print("[1/3] Twitter 取得中（直近24h）...")
        tw_ja, tw_en = fetch_twitter(hours=hours_back)

        # RSSフォールバックは使わない。ツイートが取れない場合はメールにその旨を表示する。

        print("[2/3] HTML 生成中...")
        html  = build_html(tw_ja, tw_en,
                           date_str, time_label, since_str, now_str)

        # プレーンテキスト（スパムフィルタ対策）
        all_tw = sorted(tw_ja + tw_en, key=lambda x: x["eng"], reverse=True)
        plain_lines = [subject, ""]
        if all_tw:
            for t in all_tw[:10]:
                who = f"@{t['username']} " if t.get("username") else ""
                plain_lines.append(f"♥{t['likes']} RT{t['retweets']}  {who}{t['text'][:120]}")
                plain_lines.append(f"https://x.com/i/web/status/{t['id']}")
                plain_lines.append("")
        else:
            plain_lines.append("X (Twitter) のデータを取得できませんでした。")
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
