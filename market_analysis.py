#!/usr/bin/env python3
"""
マーケット分析メール
・市場レジーム分析（金利・為替・株式の局面比較）
・日経平均チャートパターン分析（SVG）
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

        WINDOW    = 63   # 3ヶ月（マクロ局面の特徴づけに適切）
        FWD_DAYS  = 5    # 類似局面後1週間
        YEARS     = 20
        STEP      = 5
        TOP_N     = 10

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
        # 直近3ウィンドウは除外（ルックアヘッドバイアス防止）
        cutoff    = current_date - pd.Timedelta(days=WINDOW * 3)
        hist_filt = hist_norm[hist_norm.index <= cutoff]
        similar   = find_similar(current_norm, hist_filt, top_n=TOP_N)
        if similar.empty:
            return None

        # 類似局面トップ1のみ使用
        row       = similar.iloc[0]
        sim_date  = row["date"]
        lbl       = period_label(sim_date)
        raw_feat  = feat_records.get(sim_date)

        # 類似局面のその後5日（1週間）リターン
        fwd_single = compute_forward_returns(prices, [sim_date], FWD_DAYS)
        fwd_5d: dict = {}
        if not fwd_single.empty:
            ret_row = fwd_single.iloc[0]
            for asset in ALL_ASSETS:
                if asset in ret_row.index:
                    v = float(ret_row[asset])
                    if not import_numpy_isnan(v):
                        fwd_5d[asset] = round(v * 100, 1)
        # 金利の1週間変化も含める
        yield_fwd = compute_forward_returns(
            yields.reindex(prices.index, method="ffill") if not yields.empty else pd.DataFrame(),
            [sim_date], FWD_DAYS
        ) if not yields.empty else pd.DataFrame()
        if not yield_fwd.empty:
            ret_row = yield_fwd.iloc[0]
            for col in ret_row.index:
                v = float(ret_row[col])
                if not import_numpy_isnan(v):
                    fwd_5d[col] = round(v, 4)  # yield は pp変化なので生の値

        top1 = {
            "date":          sim_date.strftime("%Y-%m-%d"),
            "similarity":    round(float(row["similarity"]), 4),
            "label":         lbl,
            "features":      {k: round(float(v), 4)
                              for k, v in raw_feat.items()
                              if not import_numpy_isnan(float(v))} if raw_feat is not None else {},
            "forward_5d":    fwd_5d,
        }

        price_snap = {k: round(float(v), 4) for k, v in current_raw.items()
                      if k in ALL_ASSETS and not import_numpy_isnan(float(v))}
        yield_snap = {k: round(float(v), 4) for k, v in current_raw.items()
                      if k not in ALL_ASSETS and not import_numpy_isnan(float(v))}

        print(f"  [局面] 完了 — 類似局面: {top1['date']} ({top1['similarity']:.1%})")
        return {
            "top1":         top1,
            "price_snap":   price_snap,
            "yield_snap":   yield_snap,
            "current_date": current_date.strftime("%Y-%m-%d"),
            "window":       WINDOW,
            "fwd_window":   FWD_DAYS,
        }

    except Exception as e:
        print(f"[WARN] 局面分析失敗: {e}")
        traceback.print_exc()
        return None


def import_numpy_isnan(v):
    import math
    try:
        return math.isnan(float(v))
    except Exception:
        return True


def explain_regime(regime: dict) -> str:
    client = Groq(api_key=GROQ_API_KEY)
    top1   = regime["top1"]

    def fmt_price(d):
        return "\n".join(f"  {k}: {'+' if v>0 else '-'}{abs(v)*100:.1f}%"
                         for k, v in sorted(d.items(), key=lambda x: abs(x[1]), reverse=True))

    def fmt_yield(d):
        return "\n".join(f"  {k}: {v:+.2f}%"
                         for k, v in sorted(d.items(), key=lambda x: abs(x[1]), reverse=True))

    lbl = f"[{top1['label']}]" if top1["label"] else ""
    price_f = {k: v for k, v in top1["features"].items() if k in regime["price_snap"]}
    yield_f = {k: v for k, v in top1["features"].items() if k in regime["yield_snap"]}

    feat_str = ""
    for k, v in sorted(price_f.items(), key=lambda x: abs(x[1]), reverse=True)[:6]:
        feat_str += f"  {k}: {'+' if v>0 else '-'}{abs(v)*100:.1f}%\n"
    for k, v in sorted(yield_f.items(), key=lambda x: abs(x[1]), reverse=True)[:3]:
        feat_str += f"  {k}: {v:+.2f}%\n"

    fwd = top1.get("forward_5d", {})
    fwd_str = "  ".join(
        f"{k}: {'+' if v>=0 else ''}{v:.1f}%"
        for k, v in sorted(fwd.items(), key=lambda x: abs(x[1]), reverse=True)
    ) if fwd else "データなし"

    prompt = f"""あなたはプロのマーケットアナリストです。以下の情報をもとに日本語で簡潔に分析してください。

【現在の市場動向】({regime['current_date']} 基準、直近{regime['window']}営業日)
価格変動:
{fmt_price(regime['price_snap'])}
金利変動:
{fmt_yield(regime['yield_snap'])}

【最も類似した過去局面】
{top1['date']} {lbl}  類似度: {top1['similarity']:.1%}
当時の動き:
{feat_str}
翌1週間実績: {fwd_str}

以下の形式で出力してください：

## 類似局面: {top1['date']} {lbl}
- 現在と似ている点を2〜3点（箇条書き）
- 当時のその後の特徴的な動き

## 今回の示唆
- 上記を踏まえた今後1〜2週間の注目ポイントを2〜3点
"""
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
    )
    return response.choices[0].message.content


# ── 統合テーブル生成 ──────────────────────────────────
def build_regime_table(regime: dict) -> str:
    """
    1本のテーブルで表示:
    資産 | 現在（直近N日）| 類似局面①当時 | 類似局面①その後5日
    """
    top1       = regime["top1"]
    price_snap = regime["price_snap"]
    yield_snap = regime["yield_snap"]
    fwd_5d     = top1.get("forward_5d", {})
    fwd_days   = regime.get("fwd_window", 5)
    window     = regime["window"]

    def fmt_price(v):
        if v is None:
            return "<span style='color:#ccc;'>—</span>"
        try:
            v = float(v)
        except Exception:
            return "<span style='color:#ccc;'>—</span>"
        color = "#2e7d32" if v > 0 else "#c62828"
        sign  = "+" if v > 0 else "-"
        return f"<span style='color:{color};font-weight:bold;'>{sign}{abs(v)*100:.1f}%</span>"

    def fmt_yield(v):
        if v is None:
            return "<span style='color:#ccc;'>—</span>"
        try:
            v = float(v)
        except Exception:
            return "<span style='color:#ccc;'>—</span>"
        color = "#2e7d32" if v > 0 else "#c62828"
        return f"<span style='color:{color};'>{v:+.2f}%</span>"

    def fmt_fwd(key, val):
        if val is None:
            return "<span style='color:#ccc;'>—</span>"
        try:
            v = float(val)
        except Exception:
            return "<span style='color:#ccc;'>—</span>"
        is_yield = key in regime["yield_snap"]
        color = "#2e7d32" if v > 0 else "#c62828"
        if is_yield:
            return f"<span style='color:{color};'>{v:+.4f}%</span>"
        else:
            sign = "+" if v > 0 else "-"
            return f"<span style='color:{color};font-weight:bold;'>{sign}{abs(v):.1f}%</span>"

    # 価格系列（変化の大きい順）+ 金利系列
    price_keys = sorted(price_snap.keys(), key=lambda k: -abs(price_snap[k]))
    yield_keys = sorted(yield_snap.keys(), key=lambda k: -abs(yield_snap[k]))
    all_keys   = price_keys + yield_keys

    rows = ""
    for i, k in enumerate(all_keys):
        is_price = k in price_snap
        cur      = price_snap.get(k) or yield_snap.get(k)
        past     = top1["features"].get(k)
        fwd_val  = fwd_5d.get(k)
        bg       = "#fff" if i % 2 == 0 else "#fafafa"
        rows += (
            f"<tr style='background:{bg};'>"
            f"<td style='padding:5px 8px;font-size:12px;font-weight:bold;'>{k}</td>"
            f"<td style='padding:5px 8px;font-size:12px;text-align:center;background:#fffde7;'>"
            f"{fmt_price(cur) if is_price else fmt_yield(cur)}</td>"
            f"<td style='padding:5px 8px;font-size:12px;text-align:center;'>"
            f"{fmt_price(past) if is_price else fmt_yield(past)}</td>"
            f"<td style='padding:5px 8px;font-size:12px;text-align:center;background:#f1f8e9;'>"
            f"{fmt_fwd(k, fwd_val)}</td>"
            f"</tr>"
        )

    sim_lbl = top1["label"] or ""
    sim_hdr = f"{top1['date'][:7]}<br><small style='color:#888;font-weight:normal;'>{sim_lbl}</small>"

    return f"""<table style='width:100%;border-collapse:collapse;margin-top:10px;font-family:sans-serif;'>
  <thead>
    <tr style='background:#eceff1;'>
      <th style='padding:6px 8px;font-size:11px;text-align:left;'>資産</th>
      <th style='padding:6px 8px;font-size:11px;text-align:center;background:#fff8e1;'>
        現在<br><small style='color:#888;font-weight:normal;'>直近{window}日</small></th>
      <th style='padding:6px 8px;font-size:11px;text-align:center;'>
        類似局面①<br><small style='color:#888;font-weight:normal;'>{sim_hdr}</small></th>
      <th style='padding:6px 8px;font-size:11px;text-align:center;background:#f1f8e9;'>
        その後{fwd_days}日間<br><small style='color:#888;font-weight:normal;'>（類似局面①翌1週間）</small></th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>"""


# ── チャートパターン分析 ──────────────────────────────
def run_pattern_analysis(window_days: int = 63, forward_days: int = 21,
                         top_n: int = 5) -> dict | None:
    try:
        import numpy as np
        import pandas as pd
        from numpy.lib.stride_tricks import sliding_window_view

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from market_regime import load_prices

        print("  [パターン] 日経平均データ読み込み中...")
        prices = load_prices()
        if prices.empty or "NKY" not in prices.columns:
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

        cutoff  = len(corrs) - forward_days - 5
        corrs   = corrs[:cutoff]
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

            base_s        = vals[idx] if vals[idx] != 0 else 1.0
            hist_indexed  = (vals[idx: idx + window_days] / base_s * 100).tolist()
            fwd_indexed   = (vals[idx + window_days: idx + window_days + forward_days] / base_s * 100).tolist()
            results.append({
                "start_date":      nky.index[idx].strftime("%Y-%m-%d"),
                "end_date":        nky.index[end_idx].strftime("%Y-%m-%d"),
                "corr":            round(float(corrs[idx]), 4),
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


# ── SVGチャート生成（携帯対応） ───────────────────────
def build_pattern_svg(data: dict) -> str:
    """日経平均パターン分析をインラインSVGで描画。画像不使用のため携帯メールで確実表示。"""
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

    all_y = list(cur)
    if top1:
        all_y.extend(top1.get("pattern_indexed", []))
        all_y.extend(top1.get("forward_indexed", []))
    all_y = [y for y in all_y if not np.isnan(float(y))]
    if not all_y:
        return ""

    y_min = min(all_y) * 0.997
    y_max = max(all_y) * 1.003
    total_x = win_days + fwd_days

    def sx(i):
        return ML + (i / max(total_x - 1, 1)) * pw

    def sy(v):
        return MT + (1 - (v - y_min) / max(y_max - y_min, 1e-6)) * ph

    def to_path(pts, x_offset=0):
        valid = [(i, float(v)) for i, v in enumerate(pts) if not np.isnan(float(v))]
        if not valid:
            return ""
        parts = [f"M {sx(valid[0][0]+x_offset):.1f},{sy(valid[0][1]):.1f}"]
        for i, v in valid[1:]:
            parts.append(f"L {sx(i+x_offset):.1f},{sy(v):.1f}")
        return " ".join(parts)

    elems = []

    # グリッド・Y軸ラベル
    for i in range(5):
        yv  = y_min + (y_max - y_min) * i / 4
        yp  = sy(yv)
        elems.append(f'<line x1="{ML}" y1="{yp:.1f}" x2="{W-MR}" y2="{yp:.1f}" '
                     f'stroke="#f0f0f0" stroke-width="1"/>')
        elems.append(f'<text x="{ML-4}" y="{yp+4:.1f}" text-anchor="end" '
                     f'font-size="9" fill="#aaa">{yv:.0f}</text>')

    # ベースライン100
    if y_min < 100 < y_max:
        y100 = sy(100)
        elems.append(f'<line x1="{ML}" y1="{y100:.1f}" x2="{W-MR}" y2="{y100:.1f}" '
                     f'stroke="#ddd" stroke-width="1" stroke-dasharray="3,2"/>')

    # 「今日」縦線
    tx = sx(win_days - 1)
    elems.append(f'<line x1="{tx:.1f}" y1="{MT}" x2="{tx:.1f}" y2="{H-MB}" '
                 f'stroke="#bbb" stroke-width="1" stroke-dasharray="3,2"/>')
    elems.append(f'<text x="{tx:.1f}" y="{H-MB+11}" text-anchor="middle" '
                 f'font-size="8" fill="#999">今日</text>')

    # 類似局面①（赤・実線）＋ その後（赤・破線）
    if top1:
        patt = top1.get("pattern_indexed", [])
        fwdv = top1.get("forward_indexed", [])
        ff   = top1.get("forward_final")
        if patt:
            d = to_path(patt)
            if d:
                elems.append(f'<path d="{d}" stroke="#E53935" stroke-width="2.2" '
                             f'fill="none" opacity="0.75"/>')
        if fwdv and patt:
            join = [patt[-1]] + list(fwdv)
            d = to_path(join, x_offset=len(patt)-1)
            if d:
                elems.append(f'<path d="{d}" stroke="#E53935" stroke-width="1.8" '
                             f'fill="none" stroke-dasharray="5,3" opacity="0.7"/>')

    # 現在（青・実線・前面）
    if cur.size:
        d = to_path(cur.tolist())
        if d:
            elems.append(f'<path d="{d}" stroke="#1565C0" stroke-width="2.5" fill="none"/>')

    # タイトル
    avg_r = data.get("avg_forward_return")
    title = f"日経平均 現在 vs 類似局面TOP1（インデックス 期初=100）"
    if avg_r is not None:
        n = len(data["top_similar"])
        title += f"  上位{n}局面 翌{fwd_days}日平均 {avg_r:+.1f}%"
    elems.append(f'<text x="{ML+pw//2}" y="{MT-10}" text-anchor="middle" '
                 f'font-size="9" font-weight="bold" fill="#444">{title}</text>')

    # 凡例
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


# ── HTML 生成 ─────────────────────────────────────────
def build_html(regime: dict | None, regime_exp: str,
               pattern: dict | None, date_str: str) -> str:

    # ── 局面分析セクション ──────────────────────────────
    regime_section = ""
    if regime:
        top1    = regime["top1"]
        lbl     = f" {top1['label']}" if top1["label"] else ""
        chip    = (f"<span style='display:inline-block;background:#fff3e0;"
                   f"border:1px solid #fb8c00;border-radius:12px;padding:3px 12px;"
                   f"font-size:11px;'>{top1['date']}{lbl} "
                   f"({top1['similarity']:.1%})</span>")

        exp_html = (regime_exp
                    .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        exp_html = re.sub(r"## (類似局面.*?)(\n|$)",
                          r"<h4 style='color:#b71c1c;font-size:13px;margin:10px 0 2px;'>🕐 \1</h4>",
                          exp_html)
        exp_html = re.sub(r"## (今回の示唆.*?)(\n|$)",
                          r"<h4 style='color:#1565c0;font-size:13px;margin:10px 0 2px;'>💡 \1</h4>",
                          exp_html)
        exp_html = exp_html.replace("\n- ", "<br>&nbsp;• ").replace("\n", "<br>")

        regime_table = build_regime_table(regime)

        regime_section = f"""
  <div style="background:#fff8f8;padding:16px 24px;border:1px solid #e8e8e8;border-top:none;">
    <div style="font-size:15px;font-weight:bold;color:#b71c1c;margin-bottom:10px;">
      📊 市場レジーム分析（直近{regime['window']}営業日 ≈ 3ヶ月 / {regime['current_date']}基準）
    </div>
    <div style="margin-bottom:10px;">
      <span style="font-size:11px;color:#888;font-weight:bold;">類似局面：</span>
      {chip}
    </div>
    {regime_table}
    <div style="background:white;padding:12px;border-radius:6px;font-size:12px;
                line-height:1.7;border-left:3px solid #ef9a9a;margin-top:12px;">
      {exp_html}
    </div>
    <p style="font-size:10px;color:#ccc;margin:10px 0 0;">
      ※ 本分析は過去データに基づく参考情報です。投資判断を保証するものではありません。
    </p>
  </div>"""

    # ── パターン分析セクション ──────────────────────────
    pattern_section = ""
    if pattern:
        svg_chart = build_pattern_svg(pattern)

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
    {svg_chart}
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
      ※ Pearson r で形状類似度を計算。チャート形状の一致は参考指標です。
    </p>
  </div>"""

    no_data_msg = ""
    if not regime and not pattern:
        no_data_msg = """
  <div style="padding:20px 24px;border:1px solid #e8e8e8;border-top:none;color:#999;font-size:13px;">
    本日は分析データを取得できませんでした。
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:640px;
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
      Powered by Groq (Llama 3.3 70B) · yfinance
    </p>
  </div>
</body></html>"""


# ── メール送信 ────────────────────────────────────────
def send_email(subject: str, html_body: str) -> None:
    msg             = MIMEMultipart("alternative")
    msg["Subject"]  = subject
    msg["From"]     = GMAIL_USER
    msg["To"]       = GMAIL_USER
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
    pattern = run_pattern_analysis(window_days=63, forward_days=21, top_n=5)

    print("[4/4] メール送信...")
    html    = build_html(regime, regime_exp, pattern, date_str)
    subject = f"🔬 マーケット分析 {now_sgt.strftime('%m/%d(%a)')} | 局面分析・日経パターン"
    send_email(subject, html)


if __name__ == "__main__":
    main()
