#!/usr/bin/env python3
"""
マーケット分析メール
・市場レジーム分析（金利・為替・株式の局面比較）
・日経平均チャートパターン分析
を毎朝 6:00 SGT にメール送信する
"""

import io as _io
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

# ── 設定 ───────────────────────────────────────────────
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
GMAIL_USER         = os.environ.get("GMAIL_USER", "alx99281@gmail.com")
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

# ── 局面分析 ──────────────────────────────────────────
def run_regime_analysis() -> dict | None:
    try:
        import numpy as np
        import pandas as pd
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from market_regime import (
            load_prices, load_yields, compute_features, find_similar,
            compute_forward_returns, period_label, ALL_ASSETS, normalize_all,
        )

        WINDOW = 21
        YEARS  = 15
        STEP   = 5
        TOP_N  = 10

        print("  [局面] 価格データ取得中...")
        prices = load_prices(YEARS, use_cache=False)
        print("  [局面] 金利データ取得中...")
        yields = load_yields(YEARS, use_cache=False)
        if not yields.empty:
            yields = yields.reindex(prices.index, method="ffill")

        us_open = prices.index[prices["SPX"].notna()] if "SPX" in prices.columns else prices.index
        feat_records: dict = {}
        for date in us_open[WINDOW::STEP]:
            feat = compute_features(prices, yields, date, WINDOW)
            if feat is not None:
                feat_records[date] = feat

        if not feat_records:
            return None

        hist_raw     = pd.DataFrame(feat_records).T
        current_date = prices.index[-1]
        current_raw  = compute_features(prices, yields, current_date, WINDOW)
        if current_raw is None:
            return None

        hist_norm, current_norm = normalize_all(hist_raw, current_raw)
        cutoff    = current_date - pd.Timedelta(days=WINDOW * 3)
        hist_filt = hist_norm[hist_norm.index <= cutoff]
        similar   = find_similar(current_norm, hist_filt, top_n=TOP_N)
        if similar.empty:
            return None

        fwd_rets = compute_forward_returns(prices, similar["date"].tolist(), WINDOW)

        outperform, underperform = [], []
        if not fwd_rets.empty:
            mean_ret = fwd_rets.mean() * 100
            hit_rate = (fwd_rets > 0).mean() * 100
            m_n   = (mean_ret - mean_ret.mean()) / (mean_ret.std() + 1e-9)
            h_n   = (hit_rate  - hit_rate.mean()) / (hit_rate.std()  + 1e-9)
            score = (m_n * 0.6 + h_n * 0.4).sort_values(ascending=False)
            for asset in score.head(3).index:
                outperform.append({"asset": asset,
                                   "avg": round(float(mean_ret[asset]), 1),
                                   "hit": round(float(hit_rate[asset]))})
            for asset in score.tail(3).index:
                underperform.append({"asset": asset,
                                     "avg": round(float(mean_ret[asset]), 1),
                                     "hit": round(float(hit_rate[asset]))})

        top2 = []
        for _, row in similar.head(2).iterrows():
            date     = row["date"]
            lbl      = period_label(date)
            raw_feat = feat_records.get(date)

            fwd_single = compute_forward_returns(prices, [date], WINDOW)
            fwd_dict: dict = {}
            if not fwd_single.empty:
                ret_row = fwd_single.iloc[0]
                for asset in ALL_ASSETS:
                    if asset in ret_row.index:
                        v = float(ret_row[asset])
                        if not np.isnan(v):
                            fwd_dict[asset] = round(v * 100, 1)

            top2.append({
                "date":            date.strftime("%Y-%m-%d"),
                "similarity":      round(float(row["similarity"]), 4),
                "label":           lbl,
                "features":        {k: round(float(v), 4)
                                    for k, v in raw_feat.items()
                                    if not np.isnan(float(v))} if raw_feat is not None else {},
                "forward_returns": fwd_dict,
            })

        price_snap = {k: round(float(v), 4) for k, v in current_raw.items()
                      if k in ALL_ASSETS and not np.isnan(float(v))}
        yield_snap = {k: round(float(v), 4) for k, v in current_raw.items()
                      if k not in ALL_ASSETS and not np.isnan(float(v))}

        print(f"  [局面] 完了 — 類似局面 {len(top2)} 件取得")
        return {
            "top2": top2, "outperform": outperform, "underperform": underperform,
            "price_snap": price_snap, "yield_snap": yield_snap,
            "current_date": current_date.strftime("%Y-%m-%d"), "window": WINDOW,
        }

    except Exception as e:
        print(f"[WARN] 局面分析失敗: {e}")
        traceback.print_exc()
        return None


def explain_regime(regime: dict) -> str:
    client = Groq(api_key=GROQ_API_KEY)

    def fmt_price(d):
        return "\n".join(f"  {k}: {'+' if v>0 else '-'}{abs(v)*100:.1f}%"
                         for k, v in sorted(d.items(), key=lambda x: abs(x[1]), reverse=True))

    def fmt_yield(d):
        return "\n".join(f"  {k}: {v:+.2f}%"
                         for k, v in sorted(d.items(), key=lambda x: abs(x[1]), reverse=True))

    similar_text = ""
    for i, p in enumerate(regime["top2"], 1):
        lbl = f"[{p['label']}]" if p["label"] else "[ラベルなし]"
        similar_text += f"\n類似局面{i}: {p['date']} {lbl}  類似度: {p['similarity']:.2%}\n"
        price_f = {k: v for k, v in p["features"].items() if k in regime["price_snap"]}
        yield_f = {k: v for k, v in p["features"].items() if k in regime["yield_snap"]}
        for k, v in sorted(price_f.items(), key=lambda x: abs(x[1]), reverse=True)[:6]:
            similar_text += f"  {k}: {'+' if v>0 else '-'}{abs(v)*100:.1f}%\n"
        for k, v in sorted(yield_f.items(), key=lambda x: abs(x[1]), reverse=True)[:3]:
            similar_text += f"  {k}: {v:+.2f}%\n"
        fwd = p.get("forward_returns", {})
        if fwd:
            fwd_str = "  ".join(
                f"{k}: {'+' if v>=0 else ''}{v:.1f}%"
                for k, v in sorted(fwd.items(), key=lambda x: abs(x[1]), reverse=True)
            )
            similar_text += f"  ↓翌{regime['window']}日実績: {fwd_str}\n"

    prompt = f"""あなたはプロのマーケットアナリストです。以下の情報をもとに日本語で分析してください。

【現在の市場動向】({regime['current_date']} 基準、直近{regime['window']}営業日)
価格変動:
{fmt_price(regime['price_snap'])}
金利変動:
{fmt_yield(regime['yield_snap'])}

【過去の類似局面トップ2（コサイン類似度）】
{similar_text}

以下の形式で、簡潔に出力してください：

## 類似局面①: [日付] [局面名]
- 現在と似ている点を2〜3点（箇条書き、数値は表を参照するため不要）
- その後の特徴的な動き（翌月実績から読み取れること）

## 類似局面②: [日付] [局面名]
- 同上

## 今回の示唆
- 2局面の共通点・相違点から読み取れる今後の注目ポイントを2〜3点
"""
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2048,
    )
    return response.choices[0].message.content


# ── 局面比較テーブル生成 ──────────────────────────────
def build_regime_table(regime: dict) -> str:
    import numpy as np
    top2       = regime["top2"]
    price_snap = regime["price_snap"]
    yield_snap = regime["yield_snap"]

    def fmt(val, is_price):
        if val is None:
            return "—"
        try:
            v = float(val)
        except Exception:
            return "—"
        if is_price:
            color = "#2e7d32" if v > 0 else "#c62828"
            sign  = "+" if v > 0 else "▲"
            return f"<span style='color:{color};font-weight:bold;'>{sign}{abs(v)*100:.1f}%</span>"
        else:
            color = "#c62828" if v > 0 else "#2e7d32"
            return f"<span style='color:{color};'>{v:+.2f}%</span>"

    # Feature order: price assets first, then yields
    price_keys = sorted(price_snap.keys(), key=lambda k: -abs(price_snap[k]))
    yield_keys = sorted(yield_snap.keys(), key=lambda k: -abs(yield_snap[k]))
    all_keys   = price_keys + yield_keys

    h1  = f"{top2[0]['date'][:7]}<br><small style='color:#888;'>{top2[0]['label'] or ''}</small>" if top2 else ""
    h2  = f"{top2[1]['date'][:7]}<br><small style='color:#888;'>{top2[1]['label'] or ''}</small>" if len(top2) > 1 else ""

    rows = ""
    for k in all_keys:
        is_price = k in price_snap
        cur = price_snap.get(k) or yield_snap.get(k)
        v1  = top2[0]["features"].get(k) if top2 else None
        v2  = top2[1]["features"].get(k) if len(top2) > 1 else None
        bg  = "#fff" if all_keys.index(k) % 2 == 0 else "#fafafa"
        rows += (f"<tr style='background:{bg};'>"
                 f"<td style='padding:5px 8px;font-size:12px;font-weight:bold;'>{k}</td>"
                 f"<td style='padding:5px 8px;font-size:12px;text-align:center;'>{fmt(v1, is_price)}</td>"
                 f"<td style='padding:5px 8px;font-size:12px;text-align:center;'>{fmt(v2, is_price)}</td>"
                 f"<td style='padding:5px 8px;font-size:12px;text-align:center;'>{fmt(cur, is_price)}</td>"
                 f"</tr>")

    return f"""<table style='width:100%;border-collapse:collapse;margin-top:10px;'>
  <thead>
    <tr style='background:#fce4ec;'>
      <th style='padding:6px 8px;font-size:12px;text-align:left;'>特徴量</th>
      <th style='padding:6px 8px;font-size:12px;text-align:center;'>類似局面①<br>{h1}</th>
      <th style='padding:6px 8px;font-size:12px;text-align:center;'>類似局面②<br>{h2}</th>
      <th style='padding:6px 8px;font-size:12px;text-align:center;background:#fff3e0;'>現在</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>"""


# ── チャートパターン分析 ──────────────────────────────
def run_pattern_analysis(window_days: int = 42, forward_days: int = 21,
                         top_n: int = 5) -> dict | None:
    try:
        import numpy as np
        import pandas as pd
        from numpy.lib.stride_tricks import sliding_window_view

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from market_regime import load_prices

        print("  [パターン] 日経平均データ読み込み中 (Excel + 差分取得)...")
        prices = load_prices()
        if prices.empty or "NKY" not in prices.columns:
            print("  [パターン] NKYデータなし")
            return None
        nky  = prices["NKY"].dropna().sort_index()
        nky.index = pd.to_datetime(nky.index)
        vals = nky.values.astype(float)

        if len(vals) < window_days + forward_days + 10:
            return None

        def znorm(a):
            s = a.std()
            return (a - a.mean()) / (s if s > 1e-9 else 1.0)

        current_norm = znorm(vals[-window_days:])

        all_wins = sliding_window_view(vals, window_days)
        means    = all_wins.mean(axis=1, keepdims=True)
        stds     = np.where(all_wins.std(axis=1, keepdims=True) < 1e-9, 1.0,
                            all_wins.std(axis=1, keepdims=True))
        all_norm = (all_wins - means) / stds
        corrs    = (all_norm @ current_norm) / window_days

        cutoff = len(corrs) - forward_days - 5
        corrs  = corrs[:cutoff]
        top_idx = np.argsort(corrs)[::-1]

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
            fwd_rets = ((fwd_vals - base) / base * 100).tolist() if base != 0 else []

            base = vals[idx] if vals[idx] != 0 else 1.0
            hist_indexed = (vals[idx: idx + window_days] / base * 100).tolist()
            fwd_indexed  = (vals[idx + window_days: idx + window_days + forward_days] / base * 100).tolist()
            results.append({
                "start_date":      nky.index[idx].strftime("%Y-%m-%d"),
                "end_date":        nky.index[end_idx].strftime("%Y-%m-%d"),
                "corr":            round(float(corrs[idx]), 4),
                "pattern_norm":    znorm(vals[idx: idx + window_days]).tolist(),
                "forward_returns": fwd_rets,
                "forward_final":   round(float(fwd_rets[-1]), 2) if fwd_rets else None,
                "pattern_indexed": hist_indexed,
                "forward_indexed": fwd_indexed,
            })

        if not results:
            return None

        cur_base = vals[-window_days] if vals[-window_days] != 0 else 1.0
        finals = [r["forward_final"] for r in results if r["forward_final"] is not None]
        print(f"  [パターン] 完了 — 類似局面 {len(results)} 件")
        return {
            "current_norm":          znorm(vals[-window_days:]).tolist(),
            "current_indexed":       (vals[-window_days:] / cur_base * 100).tolist(),
            "current_start":         nky.index[-window_days].strftime("%Y-%m-%d"),
            "current_end":           nky.index[-1].strftime("%Y-%m-%d"),
            "top_similar":           results,
            "avg_forward_return":    round(float(__import__("numpy").mean(finals)), 2) if finals else None,
            "median_forward_return": round(float(__import__("numpy").median(finals)), 2) if finals else None,
            "window_days":           window_days,
            "forward_days":          forward_days,
        }

    except Exception as e:
        print(f"[WARN] パターン分析失敗: {e}")
        traceback.print_exc()
        return None


# ── チャート生成 ──────────────────────────────────────
def build_pattern_chart(data: dict) -> str:
    import base64
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    plt.rcParams["font.family"]        = ["Noto Sans CJK JP", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(10, 5), facecolor="white")

    window_days  = data["window_days"]
    forward_days = data["forward_days"]

    # 現在チャート（実価格インデックス）
    cur_idx = np.array(data.get("current_indexed", []))
    if cur_idx.size:
        x_cur = np.arange(len(cur_idx))
        ax.plot(x_cur, cur_idx, color="#1565C0", linewidth=2.8, zorder=5,
                label=f"現在  ({data['current_start']} 〜 {data['current_end']})")

    # トップ1類似局面（マッチ窓 + 翌1ヶ月）
    if data["top_similar"]:
        top1 = data["top_similar"][0]
        patt = np.array(top1.get("pattern_indexed", []))
        fwd  = np.array(top1.get("forward_indexed", []))
        lbl  = top1["start_date"][:7]
        ff   = top1.get("forward_final")
        ff_str = f"  翌{forward_days}日: {ff:+.1f}%" if ff is not None else ""

        if patt.size:
            x_p = np.arange(len(patt))
            ax.plot(x_p, patt, color="#E53935", linewidth=2.2,
                    label=f"類似①  {lbl}  (r={top1['corr']:.3f}){ff_str}")
        if fwd.size and patt.size:
            # 接続点を含めて破線で描画
            join  = np.concatenate([[patt[-1]], fwd])
            x_fwd = np.arange(len(patt) - 1, len(patt) + len(fwd))
            ax.plot(x_fwd, join, color="#E53935", linewidth=2.0,
                    linestyle="--", label=f"類似①その後 {forward_days}営業日（点線）")

    # 現在の末端（"今日"）を縦線で示す
    ax.axvline(x=window_days - 1, color="#555", linewidth=1.0,
               linestyle=":", alpha=0.6, label="今日")
    ax.axhline(y=100, color="#aaa", linewidth=0.6, linestyle="--", alpha=0.5)

    avg_r = data.get("avg_forward_return")
    med_r = data.get("median_forward_return")
    sub = ""
    if avg_r is not None:
        sub = f"  （類似局面 {len(data['top_similar'])} 件の翌{forward_days}日平均 {avg_r:+.1f}%  中央値 {med_r:+.1f}%）"

    ax.set_title(
        f"日経平均 現在 vs 類似局面トップ1  ―  インデックス（期初=100）{sub}",
        fontsize=9)
    ax.set_xlabel("営業日数", fontsize=8)
    ax.set_ylabel("インデックス（期初=100）", fontsize=8)
    ax.legend(fontsize=8, loc="best", framealpha=0.85)
    ax.grid(True, alpha=0.2)

    plt.tight_layout()
    buf = _io.BytesIO()
    plt.savefig(buf, format="jpeg", dpi=90, bbox_inches="tight",
                pil_kwargs={"quality": 80, "optimize": True})
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


# ── HTML 生成 ─────────────────────────────────────────
def build_html(regime: dict | None, regime_exp: str,
               pattern: dict | None, date_str: str) -> str:

    # ── 局面分析セクション ──────────────────────────────
    regime_section = ""
    if regime:
        exp_html = (regime_exp
                    .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        exp_html = re.sub(r"## (類似局面[①②③\d]+.*?)(\n|$)",
                          r"<h4 style='color:#b71c1c;font-size:13px;margin:12px 0 3px;'>🕐 \1</h4>",
                          exp_html)
        exp_html = re.sub(r"## (アウトパフォーム.*?)(\n|$)",
                          r"<h4 style='color:#1b5e20;font-size:13px;margin:12px 0 3px;'>▲ \1</h4>",
                          exp_html)
        exp_html = re.sub(r"## (アンダーパフォーム.*?)(\n|$)",
                          r"<h4 style='color:#b71c1c;font-size:13px;margin:12px 0 3px;'>▼ \1</h4>",
                          exp_html)
        exp_html = exp_html.replace("\n- ", "<br>&nbsp;• ").replace("\n", "<br>")

        chips = ""
        for i, p in enumerate(regime["top2"], 1):
            lbl = f" {p['label']}" if p["label"] else ""
            chips += (f"<span style='display:inline-block;background:#fff3e0;"
                      f"border:1px solid #fb8c00;border-radius:12px;padding:3px 10px;"
                      f"margin:2px;font-size:11px;'>#{i} {p['date']}{lbl} "
                      f"({p['similarity']:.1%})</span>")

        def fmt_ret(v):
            if v is None:
                return "<span style='color:#bbb;'>—</span>"
            color = "#2e7d32" if v >= 0 else "#c62828"
            sign  = "+" if v >= 0 else ""
            return f"<span style='color:{color};font-weight:bold;'>{sign}{v:.1f}%</span>"

        fwd_headers = ""
        for i, p in enumerate(regime["top2"], 1):
            num = ["①", "②"][i - 1]
            lbl_s = f"<br><small style='color:#888;font-weight:normal;'>{p['label'] or ''}</small>" if p["label"] else ""
            fwd_headers += (f"<th style='padding:5px 8px;font-size:11px;text-align:center;"
                            f"background:#e8f5e9;'>類似局面{num}<br>"
                            f"<small style='color:#555;'>{p['date'][:7]}</small>{lbl_s}</th>")

        all_assets = []
        for p in regime["top2"]:
            for a in p.get("forward_returns", {}).keys():
                if a not in all_assets:
                    all_assets.append(a)

        fwd_rows = ""
        for idx, asset in enumerate(all_assets):
            bg = "#fff" if idx % 2 == 0 else "#fafafa"
            cells = "".join(
                f"<td style='padding:4px 8px;font-size:12px;text-align:center;'>"
                f"{fmt_ret(p.get('forward_returns', {}).get(asset))}</td>"
                for p in regime["top2"]
            )
            fwd_rows += (f"<tr style='background:{bg};'>"
                         f"<td style='padding:4px 8px;font-size:12px;font-weight:bold;'>{asset}</td>"
                         f"{cells}</tr>")

        regime_table = build_regime_table(regime)

        regime_section = f"""
  <div style="background:#fff8f8;padding:16px 24px;border:1px solid #e8e8e8;border-top:none;">
    <div style="font-size:15px;font-weight:bold;color:#b71c1c;margin-bottom:10px;">
      📊 市場レジーム分析（直近{regime['window']}営業日 / {regime['current_date']}基準）
    </div>
    <div style="margin-bottom:10px;">
      <span style="font-size:11px;color:#888;font-weight:bold;">類似局面トップ2：</span><br>
      {chips}
    </div>
    {regime_table}
    <div style="background:white;padding:12px;border-radius:6px;font-size:12px;
                line-height:1.7;border-left:3px solid #ef9a9a;margin-top:12px;margin-bottom:14px;">
      {exp_html}
    </div>
    <div style="margin-top:4px;">
      <span style="font-size:12px;font-weight:bold;color:#555;">
        📊 類似局面の翌{regime['window']}日リターン実績
      </span>
      <table style="width:100%;border-collapse:collapse;margin-top:6px;">
        <thead>
          <tr style="background:#f1f8e9;">
            <th style="padding:5px 8px;font-size:11px;text-align:left;">資産</th>
            {fwd_headers}
          </tr>
        </thead>
        <tbody>{fwd_rows}</tbody>
      </table>
    </div>
    <p style="font-size:10px;color:#ccc;margin:10px 0 0;">
      ※ 本分析は過去データに基づく参考情報です。投資判断を保証するものではありません。
    </p>
  </div>"""

    # ── パターン分析セクション ──────────────────────────
    pattern_section = ""
    if pattern:
        chart_b64 = build_pattern_chart(pattern)
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

        pattern_section = f"""
  <div style="background:#f0f4ff;padding:16px 24px;border:1px solid #e8e8e8;border-top:none;">
    <div style="font-size:15px;font-weight:bold;color:#1565C0;margin-bottom:12px;">
      📉 日経平均 チャートパターン分析
      （{pattern['current_start']} 〜 {pattern['current_end']}）
    </div>
    <img src="data:image/jpeg;base64,{chart_b64}"
         style="width:100%;max-width:640px;border-radius:6px;display:block;"
         alt="パターン分析チャート">
    <div style="margin-top:14px;">
      <span style="font-size:12px;font-weight:bold;color:#555;">
        形状類似局面トップ{len(pattern['top_similar'])}
        （翌{pattern['forward_days']}営業日リターン）
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
      ※ Pearson r で形状類似度を計算。投資判断を保証するものではありません。
    </p>
  </div>"""

    no_data_msg = ""
    if not regime and not pattern:
        no_data_msg = """
  <div style="padding:20px 24px;border:1px solid #e8e8e8;border-top:none;color:#999;font-size:13px;">
    本日は分析データを取得できませんでした。
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"></head>
<body style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:680px;
             margin:0 auto;color:#333;font-size:14px;">
  <div style="background:#37474f;color:white;padding:16px 24px;border-radius:8px 8px 0 0;">
    <div style="font-size:20px;font-weight:bold;">🔬 マーケット分析レポート</div>
    <div style="font-size:12px;opacity:.8;margin-top:4px;">{date_str}</div>
  </div>
  {regime_section}
  {pattern_section}
  {no_data_msg}
  <div style="background:#f5f5f5;padding:10px 24px;border:1px solid #e8e8e8;
              border-top:none;border-radius:0 0 8px 8px;text-align:center;">
    <p style="font-size:10px;color:#bbb;margin:0;">
      Powered by Groq (Llama 3.3 70B) · FRED
    </p>
  </div>
</body></html>"""


# ── メール送信 ────────────────────────────────────────
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


# ── メイン ────────────────────────────────────────────
def main():
    sgt     = pytz.timezone("Asia/Singapore")
    now_sgt = datetime.now(sgt)
    days_ja = ["月", "火", "水", "木", "金", "土", "日"]
    date_str = now_sgt.strftime(
        f"%Y年%m月%d日（{days_ja[now_sgt.weekday()]}）%H:%M SGT")

    print(f"=== マーケット分析 {date_str} ===")

    print("\n[1/4] 局面分析 (market_regime)...")
    regime = run_regime_analysis()

    regime_exp = ""
    if regime:
        print("[2/4] 局面説明生成 (Groq)...")
        regime_exp = explain_regime(regime)
    else:
        print("[2/4] 局面分析スキップ")

    print("[3/4] チャートパターン分析 (日経平均)...")
    pattern = run_pattern_analysis(window_days=42, forward_days=21, top_n=5)

    print("[4/4] メール送信...")
    html    = build_html(regime, regime_exp, pattern, date_str)
    subject = f"🔬 マーケット分析 {now_sgt.strftime('%m/%d(%a)')} | 局面分析・日経パターン"
    send_email(subject, html)


if __name__ == "__main__":
    main()
