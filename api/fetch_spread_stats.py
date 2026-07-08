"""
Task 2: Lấy dữ liệu nến m15, 3 tháng gần nhất cho xyz:CL (WTI) và xyz:BRENTOIL (Brent)
trên Hyperliquid, tính spread ($/bbl) và xuất bảng thống kê.

Cách chạy:
    pip install requests pandas numpy
    python fetch_spread_stats.py

Output:
    - spread_data.csv       : toàn bộ dữ liệu nến A/B + spread theo thời gian
    - spread_stats_table.md : bảng thống kê (Mean/std/p10/p50/p90/min/max)

Ghi chú kỹ thuật:
    - Hyperliquid API endpoint: POST https://api.hyperliquid.xyz/info
      body: {"type": "candleSnapshot", "req": {"coin": <symbol>, "interval": "15m",
             "startTime": <ms>, "endTime": <ms>}}
    - API thường giới hạn số nến trả về mỗi request (thường ~5000), nên script
      tự động chia nhỏ khoảng thời gian 3 tháng thành nhiều request và ghép lại.
    - HIP-3 symbols (xyz:CL, xyz:BRENTOIL) không xuất hiện trong allMids, phải
      dùng đúng candleSnapshot với coin="xyz:CL" / "xyz:BRENTOIL".
"""

import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

HL_INFO_URL = "https://api.hyperliquid.xyz/info"

LEG_A_SYMBOL = "xyz:CL"          # WTI perp
LEG_B_SYMBOL = "xyz:BRENTOIL"    # Brent perp
INTERVAL = "1H"
LOOKBACK_DAYS = 90                # ~3 tháng

# Hyperliquid giới hạn khoảng ~ 5000 nến / request -> với m15 là ~52 ngày.
# Chia theo block 45 ngày cho an toàn.
CHUNK_DAYS = 45


def fetch_candles(coin: str, start_ms: int, end_ms: int) -> list:
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": INTERVAL,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    resp = requests.post(HL_INFO_URL, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_full_history(coin: str, days: int) -> pd.DataFrame:
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)

    all_rows = []
    cursor = start_dt
    while cursor < end_dt:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), end_dt)
        start_ms = int(cursor.timestamp() * 1000)
        end_ms = int(chunk_end.timestamp() * 1000)

        candles = fetch_candles(coin, start_ms, end_ms)
        for c in candles:
            all_rows.append({
                "t": c["t"],          # open time ms
                "close": float(c["c"]),
                "open": float(c["o"]),
                "high": float(c["h"]),
                "low": float(c["l"]),
                "volume": float(c.get("v", 0)),
            })
        cursor = chunk_end
        time.sleep(0.3)  # tránh rate limit

    df = pd.DataFrame(all_rows).drop_duplicates(subset="t").sort_values("t")
    df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    return df.reset_index(drop=True)


def build_spread_dataframe() -> pd.DataFrame:
    print(f"Fetching {LEG_A_SYMBOL} ...")
    df_a = fetch_full_history(LEG_A_SYMBOL, LOOKBACK_DAYS)
    print(f"  -> {len(df_a)} candles")

    print(f"Fetching {LEG_B_SYMBOL} ...")
    df_b = fetch_full_history(LEG_B_SYMBOL, LOOKBACK_DAYS)
    print(f"  -> {len(df_b)} candles")

    merged = pd.merge(
        df_a[["timestamp", "close"]].rename(columns={"close": "close_A"}),
        df_b[["timestamp", "close"]].rename(columns={"close": "close_B"}),
        on="timestamp",
        how="inner",
    )
    merged["spread"] = merged["close_A"] - merged["close_B"]  # $/bbl, WTI - Brent
    return merged.sort_values("timestamp").reset_index(drop=True)


def compute_stats_table(df: pd.DataFrame) -> pd.DataFrame:
    s = df["spread"]
    stats = {
        "Mean": s.mean(),
        "Std": s.std(),
        "P10": s.quantile(0.10),
        "P50 (Median)": s.quantile(0.50),
        "P90": s.quantile(0.90),
        "Min": s.min(),
        "Max": s.max(),
    }
    out = pd.DataFrame(
        [{"Chỉ số": k, "Giá trị ($/bbl)": round(v, 4)} for k, v in stats.items()]
    )
    return out


def main():
    df = build_spread_dataframe()
    df.to_csv("spread_data.csv", index=False)
    print(f"Saved {len(df)} rows -> spread_data.csv")

    stats_table = compute_stats_table(df)
    with open("spread_stats_table.md", "w") as f:
        f.write(f"# Spread Stats: {LEG_A_SYMBOL} - {LEG_B_SYMBOL} "
                f"(m15, {LOOKBACK_DAYS} ngày gần nhất)\n\n")
        f.write(stats_table.to_markdown(index=False))
    print("Saved -> spread_stats_table.md")
    print(stats_table.to_string(index=False))


if __name__ == "__main__":
    main()
