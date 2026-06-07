#!/usr/bin/env python3
"""
市場レジーム分析ツール (Market Regime Analyzer)

直近1ヶ月の市場動向を過去の類似局面と比較し、
類似局面の翌月にアウトパフォームした資産を特定します。

使い方:
    python market_regime.py                 # デフォルト設定で実行
    python market_regime.py --top 15        # 類似局面を15件表示
    python market_regime.py --no-cache      # データを再取得
    python market_regime.py --history 15    # 過去15年のデータを使用
    python market_regime.py --window 10     # 10営業日ウィンドウで比較
"""

import argparse
import io
import os
import pickle
import sys
import warnings

# Windows コンソールの文字コード問題を回避
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

CACHE_DIR = Path(__file__).parent / ".market_cache"
FETCH_YEARS = 20

# ────────────────────────────────────────────────────────────────────────────
# 注目局面ラベル（類似局面の文脈表示用）
# ────────────────────────────────────────────────────────────────────────────
NOTABLE_PERIODS = {
    ("2000-03", "2002-09"): "ITバブル崩壊",
    ("2007-07", "2009-04"): "世界金融危機(GFC)",
    ("2010-04", "2012-08"): "欧州債務危機",
    ("2013-05", "2013-07"): "バーナンキショック(テーパータントラム)",
    ("2015-08", "2016-02"): "チャイナショック/人民元切り下げ",
    ("2018-09", "2019-01"): "米中貿易摩擦・FRB利上げ",
    ("2020-01", "2020-04"): "COVID-19パンデミック",
    ("2020-05", "2021-12"): "ポストコロナ回復・リフレ相場",
    ("2022-01", "2022-12"): "インフレ・利上げサイクル",
    ("2023-02", "2023-05"): "米地銀危機(SVB等)",
    ("2023-10", "2023-10"): "米金利急騰・ドル高",
    ("2024-07", "2024-08"): "円キャリー巻き戻し",
}

# ────────────────────────────────────────────────────────────────────────────
# 資産ユニバース定義（yfinance から取得）
# ────────────────────────────────────────────────────────────────────────────
YFINANCE_PRICE_TICKERS: dict[str, str] = {
    "NKY":    "^N225",      # 日経225
    "SPX":    "^GSPC",      # S&P500
    "NDQ":    "^IXIC",      # NASDAQ総合
    "USDJPY": "JPY=X",      # 円/ドル (JPY per USD)
    "EURUSD": "EURUSD=X",   # ユーロ/ドル
    "Gold":   "GC=F",       # 金先物 (USD/oz)
}

YFINANCE_YIELD_TICKERS: dict[str, str] = {
    "US2Y":  "^IRX",   # 13週T-bill（短期金利プロキシ）
    "US10Y": "^TNX",   # 米10年債利回り
}

ALL_ASSETS = list(YFINANCE_PRICE_TICKERS.keys())


# ────────────────────────────────────────────────────────────────────────────
# データ取得
# ────────────────────────────────────────────────────────────────────────────

def _cache_save(name: str, obj: object) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    with open(CACHE_DIR / f"{name}.pkl", "wb") as f:
        pickle.dump(obj, f)


def _cache_load(name: str, max_age_hours: float = 6.0) -> Optional[object]:
    path = CACHE_DIR / f"{name}.pkl"
    if not path.exists():
        return None
    age = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds()
    if age > max_age_hours * 3600:
        return None
    with open(path, "rb") as f:
        return pickle.load(f)




def _yf_download(ticker: str, start: str, end: str) -> pd.Series:
    """yfinance で1銘柄の終値を取得して Series で返す（timezone除去済み）"""
    df = yf.download(ticker, start=start, end=end,
                     auto_adjust=True, progress=False, multi_level_index=False)
    if df.empty:
        return pd.Series(dtype=float)
    close = df["Close"] if "Close" in df.columns else df.iloc[:, 0]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index)
    if hasattr(close.index, "tz") and close.index.tz is not None:
        close.index = close.index.tz_localize(None)
    return close.dropna()


def load_prices(years: int = FETCH_YEARS, use_cache: bool = True) -> pd.DataFrame:
    today = datetime.today()
    start = (today - timedelta(days=years * 365 + 90)).strftime("%Y-%m-%d")
    end   = today.strftime("%Y-%m-%d")

    print(f"  価格データ: yfinance から {start} 〜 取得中...")
    frames: dict[str, pd.Series] = {}
    for name, ticker in YFINANCE_PRICE_TICKERS.items():
        try:
            series = _yf_download(ticker, start, end)
            if not series.empty:
                frames[name] = series
                print(f"    ✓ {name} ({ticker}): {len(series)}件")
            else:
                print(f"    - {name} ({ticker}): データなし")
        except Exception as e:
            print(f"    × {name} ({ticker}): {e}", file=sys.stderr)

    if not frames:
        return pd.DataFrame()
    df = pd.DataFrame(frames)
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def load_yields(years: int = FETCH_YEARS, use_cache: bool = True) -> pd.DataFrame:
    today = datetime.today()
    start = (today - timedelta(days=years * 365 + 90)).strftime("%Y-%m-%d")
    end   = today.strftime("%Y-%m-%d")

    print(f"  金利データ: yfinance から {start} 〜 取得中...")
    frames: dict[str, pd.Series] = {}
    for name, ticker in YFINANCE_YIELD_TICKERS.items():
        try:
            series = _yf_download(ticker, start, end)
            if not series.empty:
                frames[name] = series
                print(f"    ✓ {name} ({ticker}): {len(series)}件")
            else:
                print(f"    - {name} ({ticker}): データなし")
        except Exception as e:
            print(f"    Skip {name} ({ticker}): {e}", file=sys.stderr)

    if not frames:
        return pd.DataFrame()
    df = pd.DataFrame(frames)
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


# ────────────────────────────────────────────────────────────────────────────
# 特徴量計算
# ────────────────────────────────────────────────────────────────────────────

def compute_features(
    prices: pd.DataFrame,
    yields: pd.DataFrame,
    end_date: pd.Timestamp,
    window: int,
) -> Optional[pd.Series]:
    """
    end_dateを終点とするwindow営業日の特徴量ベクトルを計算。
    価格系列: % リターン
    金利系列: 変化幅 (pp)
    """
    p_slice = prices.loc[:end_date].tail(window + 1)
    if len(p_slice) < max(window // 2, 5):
        return None

    p_start = p_slice.iloc[0].replace(0, np.nan)
    p_end = p_slice.iloc[-1]
    p_ret = (p_end - p_start) / p_start

    y_chg = pd.Series(dtype=float)
    if not yields.empty:
        y_slice = yields.loc[:end_date].ffill().tail(window + 1)
        if len(y_slice) >= 2:
            y_chg = y_slice.iloc[-1] - y_slice.iloc[0]

    feat = pd.concat([p_ret, y_chg]).dropna()
    return feat if not feat.empty else None


def normalize_all(hist_raw: pd.DataFrame, current_raw: pd.Series) -> tuple[pd.DataFrame, pd.Series]:
    """全期間データを使ってz-score正規化。"""
    combined = pd.concat([hist_raw, current_raw.to_frame(hist_raw.index[-1] + pd.Timedelta(days=1)).T])
    mu = combined.mean()
    sigma = combined.std(ddof=1).replace(0, 1.0)
    normed = (combined - mu) / sigma
    return normed.iloc[:-1], normed.iloc[-1]


# ────────────────────────────────────────────────────────────────────────────
# 類似度計算
# ────────────────────────────────────────────────────────────────────────────

def find_similar(
    current: pd.Series,
    history: pd.DataFrame,
    top_n: int,
) -> pd.DataFrame:
    common = current.index.intersection(history.columns)
    if len(common) < 3:
        return pd.DataFrame(columns=["date", "similarity", "n_features"])

    curr_vec = current[common].values.astype(float)
    norm_c = np.linalg.norm(curr_vec)
    if norm_c == 0:
        return pd.DataFrame(columns=["date", "similarity", "n_features"])

    rows = []
    for date, row in history[common].iterrows():
        vals = row.values.astype(float)
        if np.isnan(vals).any():
            # 使える特徴だけで計算
            mask = ~np.isnan(vals)
            if mask.sum() < 3:
                continue
            cv = curr_vec[mask]
            rv = vals[mask]
        else:
            cv = curr_vec
            rv = vals

        norm_r = np.linalg.norm(rv)
        if norm_r == 0:
            continue

        sim = float(np.dot(cv, rv) / (np.linalg.norm(cv) * norm_r))
        rows.append({"date": date, "similarity": sim, "n_features": (~np.isnan(vals)).sum()})

    if not rows:
        return pd.DataFrame(columns=["date", "similarity", "n_features"])

    df = pd.DataFrame(rows).sort_values("similarity", ascending=False).head(top_n)
    return df.reset_index(drop=True)


# ────────────────────────────────────────────────────────────────────────────
# 翌月リターン計算
# ────────────────────────────────────────────────────────────────────────────

def compute_forward_returns(
    prices: pd.DataFrame,
    dates: list,
    window: int,
) -> pd.DataFrame:
    records = []
    for date in dates:
        future = prices.loc[date:].head(window + 1)
        if len(future) < max(window // 2, 3):
            continue
        p0 = future.iloc[0].replace(0, np.nan)
        ret = (future.iloc[-1] - p0) / p0
        ret.name = date
        records.append(ret)

    return pd.DataFrame(records) if records else pd.DataFrame()


# ────────────────────────────────────────────────────────────────────────────
# 出力
# ────────────────────────────────────────────────────────────────────────────

def period_label(date: pd.Timestamp) -> str:
    ym = date.strftime("%Y-%m")
    for (s, e), label in NOTABLE_PERIODS.items():
        if s <= ym <= e:
            return label
    return ""


def bar_chart(val: float, scale: float = 200.0, width: int = 20) -> str:
    n = min(int(abs(val) * scale), width)
    char = "█" if val >= 0 else "░"
    return (char * n).ljust(width)


def print_report(
    current_raw: pd.Series,
    similar: pd.DataFrame,
    fwd_rets: pd.DataFrame,
    window: int,
) -> None:
    SEP = "═" * 72
    THIN = "─" * 72
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{SEP}")
    print(f"  市場レジーム分析レポート   ({now})")
    print(f"  比較ウィンドウ: {window}営業日 (約1ヶ月)")
    print(SEP)

    # ── 直近動向スナップショット ─────────────────────────────────────────
    price_items = [(k, v) for k, v in current_raw.items() if k in ALL_ASSETS]
    yield_items = [(k, v) for k, v in current_raw.items() if k not in ALL_ASSETS]

    print(f"\n  【直近 {window} 営業日のリターン / 変化】\n")
    print(f"  {'資産':<10}  {'変化':>8}    バー")
    print(f"  {'─'*10}  {'─'*8}    {'─'*20}")

    for name, val in sorted(price_items, key=lambda x: x[1], reverse=True):
        sign = "▲" if val >= 0 else "▼"
        print(f"  {name:<10}  {sign}{val*100:>6.1f}%    {bar_chart(val)}")

    if yield_items:
        print()
        for name, val in sorted(yield_items, key=lambda x: x[1], reverse=True):
            sign = "▲" if val >= 0 else "▼"
            print(f"  {name:<10}  {sign}{val:>+6.2f}pp   {bar_chart(val, scale=500)}")

    # ── 類似局面 ─────────────────────────────────────────────────────────
    print(f"\n{THIN}")
    print(f"\n  【過去の類似局面  (上位 {len(similar)} 件 / コサイン類似度)】\n")
    print(f"  {'#':>3}  {'日付':<12}  {'類似度':>8}  {'特徴数':>6}  局面・文脈")
    print(f"  {'─'*3}  {'─'*12}  {'─'*8}  {'─'*6}  {'─'*30}")

    for i, row in similar.iterrows():
        lbl = period_label(row["date"])
        lbl_str = f"[{lbl}]" if lbl else ""
        print(
            f"  {i+1:>3}  {row['date'].strftime('%Y-%m-%d'):<12}"
            f"  {row['similarity']:>8.4f}  {int(row['n_features']):>6}  {lbl_str}"
        )

    # ── 翌月パフォーマンス集計 ──────────────────────────────────────────
    if fwd_rets.empty:
        print("\n  ※ 翌月データが取得できませんでした。\n")
        print(SEP)
        return

    n_samples = len(fwd_rets)
    mean_ret = fwd_rets.mean() * 100
    median_ret = fwd_rets.median() * 100
    hit_rate = (fwd_rets > 0).mean() * 100
    std_ret = fwd_rets.std() * 100

    stats = pd.DataFrame({
        "平均(%)": mean_ret,
        "中央値(%)": median_ret,
        "勝率(%)": hit_rate,
        "標準偏差(%)": std_ret,
    }).sort_values("平均(%)", ascending=False)

    print(f"\n{THIN}")
    print(f"\n  【類似局面の翌月パフォーマンス  (サンプル数: {n_samples}件)】\n")
    print(
        f"  {'資産':<10}  {'平均':>8}  {'中央値':>8}  {'勝率':>8}  {'標準偏差':>10}  バー(平均)"
    )
    print(f"  {'─'*10}  {'─'*8}  {'─'*8}  {'─'*8}  {'─'*10}  {'─'*20}")

    for asset, row in stats.iterrows():
        avg = row["平均(%)"]
        sign = "▲" if avg >= 0 else "▼"
        print(
            f"  {asset:<10}  {sign}{avg:>6.1f}%  {row['中央値(%)']:>+7.1f}%"
            f"  {row['勝率(%)']:>7.0f}%  {row['標準偏差(%)']:>9.1f}%  {bar_chart(avg/100)}"
        )

    # ── 個別局面の詳細 ───────────────────────────────────────────────────
    print(f"\n{THIN}")
    print(f"\n  【個別局面の翌月リターン詳細】\n")

    show_assets = [a for a in ALL_ASSETS if a in fwd_rets.columns][:10]

    header = f"  {'日付':<12}  {'類似度':>7}"
    for a in show_assets:
        header += f"  {a:>7}"
    header += f"  {'文脈':<25}"
    print(header)
    print("  " + "─" * (len(header) - 2))

    for _, sim_row in similar.iterrows():
        date = sim_row["date"]
        if date not in fwd_rets.index:
            continue
        ret_row = fwd_rets.loc[date]
        lbl = period_label(date) or "-"
        line = f"  {date.strftime('%Y-%m-%d'):<12}  {sim_row['similarity']:>7.4f}"
        for a in show_assets:
            val = ret_row.get(a, np.nan)
            if pd.isna(val):
                line += f"  {'N/A':>7}"
            else:
                line += f"  {val*100:>+6.1f}%"
        line += f"  {lbl[:25]:<25}"
        print(line)

    # ── アウトパフォーマーサマリー ────────────────────────────────────────
    print(f"\n{THIN}")
    print(f"\n  【翌月アウトパフォーマー予測 (平均+勝率の総合スコア)】\n")

    # スコア = 正規化平均 * 0.6 + 正規化勝率 * 0.4
    m_norm = (mean_ret - mean_ret.mean()) / (mean_ret.std() + 1e-9)
    h_norm = (hit_rate - hit_rate.mean()) / (hit_rate.std() + 1e-9)
    score = m_norm * 0.6 + h_norm * 0.4
    score_sorted = score.sort_values(ascending=False)

    print(f"  アウトパフォーム候補:")
    for asset in score_sorted.head(4).index:
        avg = mean_ret[asset]
        hr = hit_rate[asset]
        sign = "▲" if avg >= 0 else "▼"
        print(f"    ◆ {asset:<8}  平均 {sign}{abs(avg):.1f}%  勝率 {hr:.0f}%")

    print(f"\n  アンダーパフォーム候補:")
    for asset in score_sorted.tail(4).index:
        avg = mean_ret[asset]
        hr = hit_rate[asset]
        sign = "▲" if avg >= 0 else "▼"
        print(f"    ◇ {asset:<8}  平均 {sign}{abs(avg):.1f}%  勝率 {hr:.0f}%")

    print(f"\n{SEP}")
    print("  ※ 本ツールの分析結果は投資判断を保証するものではありません。")
    print(f"{SEP}\n")


# ────────────────────────────────────────────────────────────────────────────
# エントリポイント
# ────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="市場レジーム分析ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  python market_regime.py                  デフォルト (21営業日・上位10局面・20年履歴)
  python market_regime.py --top 15         上位15局面を表示
  python market_regime.py --no-cache       データを再取得
  python market_regime.py --history 15     過去15年分の履歴を使用
  python market_regime.py --window 10      10営業日ウィンドウで比較
        """,
    )
    parser.add_argument("--window", type=int, default=21,
                        help="比較ウィンドウ (営業日数、デフォルト: 21)")
    parser.add_argument("--top", type=int, default=10,
                        help="表示する類似局面数 (デフォルト: 10)")
    parser.add_argument("--history", type=int, default=20,
                        help="参照する過去年数 (デフォルト: 20年)")
    parser.add_argument("--step", type=int, default=5,
                        help="履歴サンプリング間隔・営業日 (デフォルト: 5)")
    parser.add_argument("--no-cache", action="store_true",
                        help="キャッシュを無視してデータを再取得")
    args = parser.parse_args()

    use_cache = not args.no_cache

    print("\n市場レジーム分析ツール 起動中...\n")

    # ── データ取得 ──────────────────────────────────────────────────────
    prices = load_prices(args.history, use_cache)
    yields = load_yields(args.history, use_cache)

    # 金利インデックスを価格インデックスに合わせてforward fill
    if not yields.empty:
        yields = yields.reindex(prices.index, method="ffill")

    available = [a for a in ALL_ASSETS if a in prices.columns]
    missing = [a for a in ALL_ASSETS if a not in prices.columns]

    print(f"\n  取得済み価格系列: {len(available)} 系列  ({', '.join(available)})")
    if missing:
        print(f"  取得失敗: {', '.join(missing)}")
    if not yields.empty:
        print(f"  取得済み金利系列: {len(yields.columns)} 系列  ({', '.join(yields.columns)})")
    print(f"  データ期間: {prices.index[0].date()} 〜 {prices.index[-1].date()}"
          f"  ({len(prices)} 営業日)")

    # ── 特徴量行列を構築 ────────────────────────────────────────────────
    print("\n  過去の特徴量を構築中...")
    # SPXが開場している日のみ対象（米国祝日を除外）
    us_open = prices.index[prices["SPX"].notna()] if "SPX" in prices.columns else prices.index
    sample_dates = us_open[args.window::args.step]
    feat_records: dict[pd.Timestamp, pd.Series] = {}

    for date in sample_dates:
        feat = compute_features(prices, yields, date, args.window)
        if feat is not None:
            feat_records[date] = feat

    if not feat_records:
        print("Error: 特徴量の構築に失敗しました。", file=sys.stderr)
        sys.exit(1)

    hist_raw = pd.DataFrame(feat_records).T

    # ── 現在の特徴量 ────────────────────────────────────────────────────
    current_date = prices.index[-1]
    current_raw = compute_features(prices, yields, current_date, args.window)
    if current_raw is None:
        print("Error: 現在の特徴量の計算に失敗しました。", file=sys.stderr)
        sys.exit(1)

    # ── 正規化 ──────────────────────────────────────────────────────────
    hist_norm, current_norm = normalize_all(hist_raw, current_raw)

    # 直近ウィンドウを除外（ルックアヘッドバイアス防止）
    cutoff = current_date - pd.Timedelta(days=args.window * 3)
    hist_filtered = hist_norm[hist_norm.index <= cutoff]
    print(f"  比較対象局面数: {len(hist_filtered)}")

    # ── 類似局面検索 ────────────────────────────────────────────────────
    print("  類似局面を検索中...")
    similar = find_similar(current_norm, hist_filtered, top_n=args.top)

    if similar.empty:
        print("Error: 類似局面が見つかりませんでした。", file=sys.stderr)
        sys.exit(1)

    # ── 翌月リターン計算 ────────────────────────────────────────────────
    print("  翌月リターンを計算中...")
    fwd_rets = compute_forward_returns(prices, similar["date"].tolist(), args.window)

    # ── レポート出力 ────────────────────────────────────────────────────
    print_report(current_raw, similar, fwd_rets, args.window)


if __name__ == "__main__":
    main()
