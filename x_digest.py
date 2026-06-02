#!/usr/bin/env python3
"""
X（Twitter）金融ソーシャルダイジェスト

X/Twitter・Reddit・StockTwits から金融関連の注目投稿をピックアップし、
  - 注目の金融商品・投資手法（最大5件、具体的なティッカー付き）
  - 注目トピック（5件）
を毎朝6時・夕方6時に送信する。各項目には引用元リンク付き。

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

    auth = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")

    queries = [
        "(株 OR 日経 OR 為替 OR 金利 OR ETF OR 投資信託 OR 債券 OR 仕組み債"
        " OR レバレッジ OR 空売り) lang:ja -is:retweet -is:reply",
        "(stocks OR investing OR markets OR Fed OR SPX OR Nikkei OR ETF"
        " OR leverage OR hedge OR bond OR forex) lang:en -is:retweet -is:reply",
    ]

    tweets = []
    for query in queries:
        try:
            resp = requests.get(
                "https://api.twitter.com/2/tweets/search/recent",
                headers=auth,
                params={
                    "query": query,
                    "max_results": 20,
                    "start_time": since,
                    "tweet.fields": "public_metrics,created_at,lang",
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
    for sub in subreddits:
        try:
            resp = requests.get(
                f"https://www.reddit.com/r/{sub}/hot.json?limit=10",
                headers=HEADERS, timeout=10,
            )
            if resp.status_code == 200:
                for item in resp.json()["data"]["children"]:
                    d = item["data"]
                    posts.append({
                        "title":    d["title"],
                        "score":    d.get("score", 0),
                        "comments": d.get("num_comments", 0),
                        "sub":      sub,
                        "url":      f"https://www.reddit.com{d.get('permalink', '')}",
                    })
        except Exception as e:
            print(f"[WARN] Reddit r/{sub}: {e}")
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


# ── Groq 要約 ─────────────────────────────────────────────

def summarize(tweets: list, reddit: list, stocktwits: list,
              time_label: str, since_str: str, now_str: str) -> tuple[str, dict]:
    """
    Returns:
        summary  : Groq が生成したMarkdownテキスト（[N] 引用番号付き）
        src_map  : {番号(int) -> URL(str)} の対応表
    """
    client = Groq(api_key=GROQ_API_KEY)

    # ── ソース番号→URL マップを構築 ──
    src_map: dict[int, str] = {}
    n = 1

    body = f"=== 集計期間: {since_str} 〜 {now_str} ===\n\n"

    if tweets:
        body += "【X (Twitter) 高エンゲージメント投稿】\n"
        for t in tweets[:20]:
            url = f"https://x.com/i/web/status/{t['id']}" if t.get("id") else ""
            src_map[n] = url
            body += (f"[{n}] ({t['lang']}) {t['text'][:200]}"
                     f" (eng:{t['engagement']})\n")
            n += 1

    if reddit:
        body += "\n【Reddit 金融コミュニティ 注目投稿】\n"
        for p in reddit[:20]:
            src_map[n] = p.get("url", "")
            body += f"[{n}] r/{p['sub']}: {p['title']} (score:{p['score']})\n"
            n += 1

    if stocktwits:
        body += "\n【StockTwits トレンドシンボル】\n"
        body += "  ".join(f"{s['symbol']}({s['title']})" for s in stocktwits) + "\n"

    sources_label = "X/Twitter・Reddit・StockTwits" if tweets else "Reddit・StockTwits"

    prompt = f"""あなたは金融ソーシャルメディアのアナリストです。
以下は{sources_label}から収集した金融関連の投稿データです（各投稿には[番号]が振られています）。
日本語でダイジェストを作成してください。

【ルール】
- 日本・米国・欧州など世界中の投稿を対象とする
- 英語投稿は日本語に翻訳・要約する
- 注目度・エンゲージメントの高いものを優先する
- 「金融商品」は必ず具体的な商品名・ティッカーシンボルで記載すること
  （例: NVIDIA(NVDA)、DRAM ETF(SOXQ)、S&P500(SPY)、ドル円(USDJPY)、米10年債など）
  個別株・ETF・投資信託・債券・為替・仕組み商品・仮想通貨・コモディティのほか、
  レバレッジ・空売りなどの具体的な投資手法も含む
- 言及が少ない場合は金融商品を省略してよい（5件に満たなくてもよい）
- 各項目の末尾に、根拠となった投稿番号を [1][3] のように必ず引用すること

【出力形式（必ずこの形式で）】

## 📈 注目の金融商品・投資手法（最大5件）
1. **[具体的な商品名・ティッカー]** — 注目理由を1〜2文 [引用番号]

## 💬 注目の金融トピック（5件）
1. **[トピック名]** — 内容を1〜2文 [引用番号]

---
{body}
"""
    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2048,
    )
    return resp.choices[0].message.content, src_map


# ── HTML 生成 ──────────────────────────────────────────────

def build_html(summary: str, src_map: dict,
               date_str: str, time_label: str,
               since_str: str, now_str: str,
               has_twitter: bool) -> str:

    # Markdown → HTML（先にエスケープ）
    html = (summary
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    html = re.sub(
        r"## (📈[^\n]*)",
        r"<h3 style='color:#2e7d32;margin:16px 0 4px;font-size:15px;'>\1</h3>",
        html)
    html = re.sub(
        r"## (💬[^\n]*)",
        r"<h3 style='color:#1565C0;margin:16px 0 4px;font-size:15px;'>\1</h3>",
        html)
    html = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"^(\d+)\. ", r"<br>&nbsp;\1. ", html, flags=re.MULTILINE)
    html = html.replace("\n---\n", "").replace("\n", "<br>")

    # [N] → クリッカブルな上付き引用番号
    def replace_citation(m):
        num = int(m.group(1))
        url = src_map.get(num, "")
        if url:
            return (
                f"<sup><a href='{url}' target='_blank' "
                f"style='color:#1976d2;text-decoration:none;font-size:10px;'>"
                f"[{num}]</a></sup>"
            )
        return f"<sup style='font-size:10px;color:#999;'>[{num}]</sup>"

    html = re.sub(r"\[(\d+)\]", replace_citation, html)

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
    {html}
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

    hour = now_sgt.hour
    time_label = "朝版（6:00 SGT）" if 4 <= hour < 14 else "夕版（18:00 SGT）"

    # 集計期間
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
    summary, src_map = summarize(tweets, reddit, st, time_label, since_str, now_str)
    html = build_html(summary, src_map, date_str, time_label,
                      since_str, now_str, bool(TWITTER_BEARER_TOKEN))
    subject = (f"{'🌅' if '朝' in time_label else '🌆'} 金融ソーシャル"
               f" {now_sgt.strftime('%m/%d(%a)')} {time_label} | 注目商品・トピック")
    send_email(subject, html)


if __name__ == "__main__":
    main()
