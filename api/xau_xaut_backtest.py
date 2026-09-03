"""
XAU vs XAUT Pairs Trading Backtest — proxy data từ OKX
========================================================
Variational KHÔNG có API lịch sử giá (chỉ có snapshot hiện tại qua
/metadata/stats), nên script này dùng OKX làm proxy:

  - XAU  (gold index, $/oz)         -> OKX index-candles, instId = XAU-USDT
  - XAUT (Tether Gold spot, $/oz)   -> OKX spot candles,  instId = XAUT-USDT

LƯU Ý: đây là proxy OKX, không phải mark/quote thật của Variational.
Basis giữa 2 venue có thể lệch vài đô so với số bạn thấy trên Variational.

Task 1: Thống kê spread 2 tháng, nến 1H (Mean/Std/P10/P50/P90/Min/Max)
Task 2: Backtest PnL mean-reversion theo nhiều ngưỡng
        - $5,000/leg, tổng $10,000 vốn mở
        - Phí 2.2 bps/fill x 4 fill/vòng = $4.40/round-trip
        - Vào lệnh: |spread - mean| >= ngưỡng
        - Ra lệnh: spread chạm/cắt lại mean
        - Chỉ 1 vị thế tại 1 thời điểm; vị thế còn mở cuối mẫu -> mark-to-market
        - Có 2 bản: mean cả mẫu (in-sample, dùng để nhìn nhanh) và
          expanding mean warmup 7 ngày (không look-ahead, số đáng tin hơn)

Yêu cầu: pip install requests pandas numpy
"""

import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone

OKX_BASE = "https://www.okx.com"
BAR = "1H"
LOOKBACK_DAYS = 60      # ~2 tháng
WARMUP_DAYS = 7         # warmup cho expanding mean

FEE_BPS = 2.2
FILLS_PER_ROUNDTRIP = 4
LEG_NOTIONAL = 5_000    # $ / leg lúc vào lệnh


# ---------------------------------------------------------------
# 1. FETCH DATA (OKX public API — không cần key)
# ---------------------------------------------------------------

def _paginate(endpoint: str, inst_id: str, start_ms: int, end_ms: int, has_volume: bool) -> pd.DataFrame:
    """Kéo candles OKX lùi dần từ end_ms về start_ms bằng cursor `after`."""
    rows, after, session = [], str(end_ms), requests.Session()
    while True:
        params = {"instId": inst_id, "bar": BAR, "limit": 100, "after": after}
        r = session.get(OKX_BASE + endpoint, params=params, timeout=10)
        r.raise_for_status()
        batch = r.json().get("data", [])
        if not batch:
            break
        rows.extend(batch)
        oldest_ts = int(batch[-1][0])
        if oldest_ts <= start_ms:
            break
        after = str(oldest_ts)
        time.sleep(0.15)  # né rate limit (OKX: 20 req / 2s cho public data)

    if not rows:
        raise RuntimeError(f"Không lấy được dữ liệu cho {inst_id} qua {endpoint}")

    ncols = 9 if has_volume else 6
    cols = ["ts", "o", "h", "l", "c", "vol", "volCcy", "volCcyQuote", "confirm"][:ncols]
    df = pd.DataFrame([row[:ncols] for row in rows], columns=cols)
    df["ts"] = pd.to_datetime(df["ts"].astype(np.int64), unit="ms", utc=True)
    df["close"] = df["c"].astype(float)
    df = df[["ts", "close"]].drop_duplicates("ts").sort_values("ts")
    df = df[df["ts"] >= pd.to_datetime(start_ms, unit="ms", utc=True)]
    return df.reset_index(drop=True)


def fetch_xau_xaut(days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    end_ms, start_ms = int(end.timestamp() * 1000), int(start.timestamp() * 1000)

    xau = _paginate("/api/v5/market/history-index-candles", "XAU-USDT", start_ms, end_ms, has_volume=False)
    xaut = _paginate("/api/v5/market/history-candles", "XAUT-USDT", start_ms, end_ms, has_volume=True)

    xau = xau.rename(columns={"close": "xau"})
    xaut = xaut.rename(columns={"close": "xaut"})

    df = pd.merge(xau, xaut, on="ts", how="inner")
    df["spread"] = df["xau"] - df["xaut"]      # $/oz
    df["mid"] = (df["xau"] + df["xaut"]) / 2
    return df


# ---------------------------------------------------------------
# TASK 1 — THỐNG KÊ SPREAD
# ---------------------------------------------------------------

def task1_stats(df: pd.DataFrame) -> pd.DataFrame:
    s = df["spread"]
    stats = {
        "Mean": s.mean(), "Std": s.std(),
        "P10": s.quantile(0.10), "P50": s.quantile(0.50), "P90": s.quantile(0.90),
        "Min": s.min(), "Max": s.max(),
    }
    return pd.DataFrame({"Chỉ số": stats.keys(),
                          "Giá trị ($/oz)": [round(v, 4) for v in stats.values()]})


# ---------------------------------------------------------------
# TASK 2 — BACKTEST PNL MEAN-REVERSION
# ---------------------------------------------------------------

def _run_one_threshold(spread, mean_, mid, ts, threshold: float) -> dict:
    trades = []
    in_pos, direction = False, 0
    entry_spread = entry_mid = entry_ts = None

    for i in range(len(spread)):
        m = mean_[i]
        if np.isnan(m):
            continue
        dev = spread[i] - m
        if not in_pos:
            if abs(dev) >= threshold:
                in_pos = True
                direction = -1 if dev > 0 else 1   # cao hơn mean -> short spread; thấp hơn -> long spread
                entry_spread, entry_mid, entry_ts = spread[i], mid[i], ts[i]
        else:
            crossed = (direction == 1 and spread[i] >= m) or (direction == -1 and spread[i] <= m)
            if crossed:
                trades.append(_close_trade(direction, entry_mid, entry_spread, spread[i], entry_ts, ts[i]))
                in_pos = False

    if in_pos:  # vị thế còn mở cuối mẫu -> mark-to-market
        trades.append(_close_trade(direction, entry_mid, entry_spread, spread[-1], entry_ts, ts[-1]))

    if not trades:
        return dict(trades=0, gross=0.0, fee=0.0, net=0.0, winrate=np.nan, hold_h=np.nan, net_per_trade=np.nan)

    t = pd.DataFrame(trades)
    return dict(
        trades=len(t), gross=t["gross"].sum(), fee=t["fee"].sum(), net=t["net"].sum(),
        winrate=(t["net"] > 0).mean() * 100, hold_h=t["hold_h"].mean(), net_per_trade=t["net"].mean(),
    )


def _close_trade(direction, entry_mid, entry_spread, exit_spread, entry_ts, exit_ts) -> dict:
    oz = LEG_NOTIONAL / entry_mid
    gross = direction * oz * (exit_spread - entry_spread)
    fee = FILLS_PER_ROUNDTRIP * (FEE_BPS / 10_000) * LEG_NOTIONAL
    hold_h = (pd.Timestamp(exit_ts) - pd.Timestamp(entry_ts)) / pd.Timedelta(hours=1)
    return {"gross": gross, "fee": fee, "net": gross - fee, "hold_h": hold_h}


def task2_backtest(df: pd.DataFrame, thresholds=None, mode: str = "in_sample") -> pd.DataFrame:
    """
    mode='in_sample' -> mean cả mẫu (CÓ look-ahead bias, chỉ để tham khảo nhanh)
    mode='expanding' -> expanding mean, warmup WARMUP_DAYS ngày (không look-ahead)
    """
    df = df.copy()
    std = df["spread"].std()

    if mode == "in_sample":
        df["mean_col"] = df["spread"].mean()
    else:
        df["mean_col"] = df["spread"].expanding(min_periods=WARMUP_DAYS * 24).mean()

    if thresholds is None:
        thresholds = [round(std * k, 2) for k in [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5]]

    spread, mean_, mid, ts = df["spread"].values, df["mean_col"].values, df["mid"].values, df["ts"].values
    rows = []
    for thr in thresholds:
        res = _run_one_threshold(spread, mean_, mid, ts, thr)
        res["threshold"] = thr
        res["sigma_mult"] = round(thr / std, 2)
        rows.append(res)

    out = pd.DataFrame(rows).rename(columns={
        "threshold": "Ngưỡng ($/oz)", "sigma_mult": "~σ", "trades": "Trades",
        "gross": "Gross", "fee": "Phí", "net": "Net PnL", "winrate": "Winrate (%)",
        "hold_h": "Hold (h)", "net_per_trade": "Net/trade",
    })
    cols = ["Ngưỡng ($/oz)", "~σ", "Trades", "Gross", "Phí", "Net PnL", "Winrate (%)", "Hold (h)", "Net/trade"]
    return out[cols].round(2)


# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------

if __name__ == "__main__":
    print("Đang tải dữ liệu OKX (XAU-USDT index + XAUT-USDT spot, 1H, ~60 ngày)...")
    df = fetch_xau_xaut(LOOKBACK_DAYS)
    print(f"Đã tải {len(df)} nến khớp thời gian giữa 2 mã.\n")

    print("=== TASK 1 — Thống kê spread 2 tháng, nến 1H ===")
    t1 = task1_stats(df)
    print(t1.to_string(index=False))

    print("\n=== TASK 2a — Mean cả mẫu (in-sample, CÓ look-ahead — chỉ tham khảo) ===")
    t2a = task2_backtest(df, mode="in_sample")
    print(t2a.to_string(index=False))

    print("\n=== TASK 2b — Expanding mean, warmup 7 ngày (KHÔNG look-ahead — số đáng tin) ===")
    t2b = task2_backtest(df, mode="expanding")
    print(t2b.to_string(index=False))

    t1.to_csv("task1_spread_stats.csv", index=False)
    t2a.to_csv("task2a_pnl_in_sample.csv", index=False)
    t2b.to_csv("task2b_pnl_expanding_mean.csv", index=False)
    print("\nĐã lưu: task1_spread_stats.csv, task2a_pnl_in_sample.csv, task2b_pnl_expanding_mean.csv")
