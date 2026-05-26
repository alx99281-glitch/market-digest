import re
import feedparser
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from groq import Groq
from datetime import datetime, timedelta, timezone
import pytz
import os
import time

# ── 設定 ───────────────────────────────────────────────
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
GMAIL_USER         = os.environ.get("GMAIL_USER", "alx99281@gmail.com")
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
FEEDBACK_FORM_URL  = os.environ.get("FEEDBACK_FORM_URL", "")

RSS_FEEDS = {
    "NHK経済":        "https://www.nhk.or.jp/rss/news/cat6.xml",
    "ロイター(日本語)": "https://jp.reuters.com/rssFeed/businessNews",
    "時事通信":        "https://www.jiji.com/rss/ranking.rdf",
    "Yahoo Finance":   "https://finance.yahoo.co.jp/rss/category/market",
    "株探":            "https://kabutan.jp/rss/news.xml",
    "Reuters(EN)":    "https://feeds.reuters.com/reuters/businessNews",
    "CNBC":           "https://www.cnbc.com/id/10001147/device/rss/rss.html",
    "Al Jazeera":     "https://www.aljazeera.com/xml/rss/all.xml",
    "WSJ Markets":    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "FT":             "https://www.ft.com/?format=rss",
    "Nikkei(EN)":     "https://www.nikkei.com/rss/rss.aspx?ce=MH",
}

FINANCE_KEYWORDS = [
    "株", "相場", "市場", "円", "ドル", "金利", "債券", "為替", "日銀", "FRB",
    "経済", "GDP", "インフレ", "物価", "日経", "TOPIX", "指数", "原油", "金価格",
    "利上げ", "利下げ", "政策金利", "貿易", "関税", "景気",
    "stock", "market", "bond", "forex", "currency", "rate", "economy",
    "inflation", "GDP", "Fed", "central bank", "equity", "yield", "treasury",
    "finance", "trade", "tariff", "recession", "oil", "gold", "yen", "dollar",
    "interest rate", "monetary", "fiscal", "nasdaq", "s&p", "dow",
]

# ── ニュース取得 ──────────────────────────────────────
def fetch_articles(hours: int = 20) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    articles = []

    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            for entry in feed.entries[:25]:
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                pub_dt = datetime(*pub[:6], tzinfo=timezone.utc) if pub else None
                if pub_dt and pub_dt < cutoff:
                    continue

                title   = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                link    = entry.get("link", "")

                text = (title + " " + summary).lower()
                is_finance_source = source in ("NHK経済", "Yahoo Finance", "株探",
                                               "ロイター(日本語)", "WSJ Markets")
                has_keyword = any(kw.lower() in text for kw in FINANCE_KEYWORDS)

                if is_finance_source or has_keyword:
                    articles.append({
                        "source":  source,
                        "title":   title,
                        "summary": summary[:600],
                        "link":    link,
                        "date":    pub_dt,
                    })
        except Exception as e:
            print(f"[WARN] {source} 取得失敗: {e}")
        time.sleep(0.3)

    articles.sort(
        key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True)
    return articles[:60]

# ── ニュース要約 ──────────────────────────────────────
def summarize(articles: list[dict]) -> str:
    client = Groq(api_key=GROQ_API_KEY)

    news_text = ""
    for i, a in enumerate(articles, 1):
        news_text += f"[{i}] ({a['source']}) {a['title']}\n"
        if a["summary"]:
            news_text += f"    {a['summary'][:300]}\n"

    prompt = f"""あなたはプロの金融ジャーナリストです。
以下のニュース記事一覧を読み、株式・債券・為替の専門家向けに日本語でダイジェストを作成してください。

【作成ルール】
- 英語記事は必ず日本語に翻訳・要約する
- 長い記事は3〜4行に圧縮する
- 各ポイントに記事番号[N]を付ける
- 推測・補足は不要。記事の内容のみを正確に伝える
- 箇条書きを使い読みやすくする

【出力フォーマット】
## 🔥 本日のハイライト
（最重要ニュース 4〜6件を箇条書き）

## 📈 株式市場
（主要株価指数・個別株の動向）

## 💴 為替
（ドル円・ユーロ円・主要通貨ペア）

## 🏦 債券・金利
（各国国債利回り・中央銀行動向）

## 🌍 経済・政策
（経済指標・政府・中央銀行の政策発表）

## 📌 その他注目
（上記以外で重要なニュース）

---
ニュース記事:
{news_text}
"""
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096,
    )
    return response.choices[0].message.content

# ── HTML メール生成 ──────────────────────────────────
def build_html(summary: str, articles: list[dict], date_str: str) -> str:
    summary_html = (
        summary
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n## ", "\n<h3 style='color:#1a73e8;margin:16px 0 4px;font-size:15px;'>")
        .replace("\n", "<br>")
    )
    summary_html = re.sub(
        r"<h3 style='color:#1a73e8;margin:16px 0 4px;font-size:15px;'>(.*?)<br>",
        r"<h3 style='color:#1a73e8;margin:16px 0 4px;font-size:15px;'>\1</h3>",
        summary_html,
    )

    sgt  = pytz.timezone("Asia/Singapore")
    rows = ""
    for i, a in enumerate(articles[:40], 1):
        title_esc = (a["title"].replace("&", "&amp;")
                               .replace("<", "&lt;").replace(">", "&gt;"))
        time_str  = a["date"].astimezone(sgt).strftime("%H:%M") if a["date"] else "--:--"
        rows += (
            f"<tr style='border-bottom:1px solid #f0f0f0;'>"
            f"<td style='padding:5px 6px;color:#999;font-size:11px;white-space:nowrap;'>[{i}]</td>"
            f"<td style='padding:5px 6px;font-size:11px;'>"
            f"<span style='color:#aaa;font-size:10px;'>{a['source']} · {time_str} SGT</span><br>"
            f"<a href='{a['link']}' style='color:#333;text-decoration:none;'>{title_esc}</a>"
            f"</td></tr>"
        )

    feedback_section = ""
    if FEEDBACK_FORM_URL:
        feedback_section = f"""
        <div style="text-align:center;margin-top:16px;">
          <a href="{FEEDBACK_FORM_URL}"
             style="background:#1a73e8;color:white;padding:8px 24px;border-radius:4px;
                    text-decoration:none;font-size:13px;display:inline-block;">
            📝 フィードバックを送る
          </a>
          <p style="font-size:10px;color:#bbb;margin:6px 0 0;">
            良かった記事・不要なカテゴリを教えてください。次回のダイジェストに反映します。
          </p>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"></head>
<body style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:680px;
             margin:0 auto;color:#333;font-size:14px;">
  <div style="background:#1a73e8;color:white;padding:16px 24px;border-radius:8px 8px 0 0;">
    <div style="font-size:20px;font-weight:bold;">📊 マーケットニュースダイジェスト</div>
    <div style="font-size:12px;opacity:.85;margin-top:4px;">{date_str}</div>
  </div>
  <div style="background:#fff;padding:20px 24px;border:1px solid #e8e8e8;
              border-top:none;line-height:1.85;">
    {summary_html}
  </div>
  <div style="background:#fafafa;padding:16px 24px;border:1px solid #e8e8e8;border-top:none;">
    <div style="font-size:13px;font-weight:bold;color:#555;margin-bottom:8px;">
      📰 参照記事一覧
    </div>
    <table style="width:100%;border-collapse:collapse;">{rows}</table>
    {feedback_section}
    <p style="font-size:10px;color:#ccc;text-align:center;margin-top:12px;">
      Powered by Groq (Llama 3.3 70B) · {len(articles)} articles collected
    </p>
  </div>
</body></html>"""

# ── メール送信 ────────────────────────────────────────
def send_email(subject: str, html_body: str, text_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = GMAIL_USER
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html",  "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, GMAIL_USER, msg.as_string())
    print("メール送信完了")

# ── メイン ────────────────────────────────────────────
def main():
    sgt      = pytz.timezone("Asia/Singapore")
    now_sgt  = datetime.now(sgt)
    days_ja  = ["月", "火", "水", "木", "金", "土", "日"]
    date_str = now_sgt.strftime(f"%Y年%m月%d日（{days_ja[now_sgt.weekday()]}）%H:%M SGT")

    print(f"=== {date_str} ===")
    print("ニュース取得中...")
    articles = fetch_articles(hours=20)
    print(f"{len(articles)} 件取得")

    if not articles:
        print("記事が見つかりませんでした。終了します。")
        return

    print("ニュース要約中 (Groq)...")
    summary = summarize(articles)

    subject = f"📊 マーケットダイジェスト {now_sgt.strftime('%m/%d(%a)')} | 株式・債券・為替"
    html    = build_html(summary, articles, date_str)
    plain   = f"マーケットダイジェスト {date_str}\n\n{summary}"

    print("メール送信中...")
    send_email(subject, html, plain)

if __name__ == "__main__":
    main()
