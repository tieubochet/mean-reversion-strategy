"""
Task 3 (bản 1H, 3 tháng): Backtest pairs trading WTI (xyz:CL) vs Brent (xyz:BRENTOIL)
trên dữ liệu 1H, 3 tháng đã lấy từ fetch_spread_stats_h1.py (file spread_data_h1.csv).

Giả định:
    - $5,000 / leg, tổng $10,000 vốn thế mỗi vòng trade
    - Phí: 2.2 bps (0.022%) MỖI FILL, 4 fill / vòng (mở A, mở B, đóng A, đóng B)
      => tổng phí ~ 4 * 0.022% * notional_per_fill
    - Chiến lược: z-score mean reversion trên spread
        z = (spread - rolling_mean) / rolling_std   (rolling window có thể chỉnh)
        Vào lệnh: |z| > threshold
            z > threshold  -> Short A, Long B  (spread quá cao, kỳ vọng co lại)
            z < -threshold -> Long A, Short B  (spread quá thấp, kỳ vọng giãn lên)
        Thoát lệnh: z quay về 0 (hoặc đổi dấu), hoặc hết dữ liệu.

Cách chạy:
    python backtest_pairs_h1.py

Output: backtest_results_table_h1.md, backtest_results_h1.csv
"""

import pandas as pd
import numpy as np

INPUT_CSV = "spread_data_h1.csv"       # từ fetch_spread_stats_h1.py
ROLLING_WINDOW = 24                    # 24 nến 1h = 1 ngày, dùng làm baseline rolling
CAPITAL_PER_LEG = 5_000.0
FEE_BPS_PER_FILL = 2.2                 # basis points
FILLS_PER_ROUND = 4
THRESHOLDS = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5]


def load_data() -> pd.DataFrame:
    df = pd.read_csv(INPUT_CSV, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["roll_mean"] = df["spread"].rolling(ROLLING_WINDOW).mean()
    df["roll_std"] = df["spread"].rolling(ROLLING_WINDOW).std()
    df["z"] = (df["spread"] - df["roll_mean"]) / df["roll_std"]
    return df.dropna().reset_index(drop=True)


def run_backtest_for_threshold(df: pd.DataFrame, threshold: float) -> dict:
    position = 0          # 0 = flat, 1 = long spread (long A/short B), -1 = short spread
    entry_idx = None
    entry_spread = None

    trades = []  # list of dict: pnl_gross, hold_bars, direction

    for i in range(len(df)):
        z = df.loc[i, "z"]
        spread = df.loc[i, "spread"]

        if position == 0:
            if z > threshold:
                position = -1
                entry_idx = i
                entry_spread = spread
            elif z < -threshold:
                position = 1
                entry_idx = i
                entry_spread = spread
        else:
            exit_now = (position == 1 and z >= 0) or (position == -1 and z <= 0)
            last_bar = (i == len(df) - 1)
            if exit_now or last_bar:
                exit_spread = spread
                # PnL per barrel: position * (exit_spread - entry_spread)
                # (long spread = long A, short B -> profit khi spread tăng)
                spread_change = exit_spread - entry_spread
                pnl_per_barrel = position * spread_change
                # Số barrel/leg = CAPITAL_PER_LEG / giá tài sản thực lúc vào lệnh
                # (KHÔNG chia cho giá spread — đó là lỗi cũ khiến gross_pnl bị
                # thổi phồng ~17 lần, vì giá spread ~$4-5 nhỏ hơn nhiều so với
                # giá tài sản thực ~$74-79).
                entry_avg_price = (df.loc[entry_idx, "close_A"] + df.loc[entry_idx, "close_B"]) / 2
                barrels_per_leg = CAPITAL_PER_LEG / max(entry_avg_price, 1)
                gross_pnl = pnl_per_barrel * barrels_per_leg
                hold_bars = i - entry_idx
                trades.append({
                    "entry_time": df.loc[entry_idx, "timestamp"],
                    "exit_time": df.loc[i, "timestamp"],
                    "direction": "Long spread" if position == 1 else "Short spread",
                    "gross_pnl": gross_pnl,
                    "hold_bars": hold_bars,
                })
                position = 0
                entry_idx = None
                entry_spread = None

    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        return {
            "Ngưỡng": threshold, "Trades": 0, "Gross": 0.0, "Phí": 0.0,
            "Net PnL": 0.0, "Winrate": 0.0, "Hold (m15 bars)": 0.0, "Net/Trade": 0.0,
        }

    fee_per_round = (FEE_BPS_PER_FILL / 10_000) * (CAPITAL_PER_LEG * 2) * (FILLS_PER_ROUND / 4)
    # 4 fill/vòng, mỗi fill trên notional CAPITAL_PER_LEG (2 leg mở + 2 leg đóng
    # = 4 fill, mỗi fill notional = CAPITAL_PER_LEG) -> fee = 4 * bps * CAPITAL_PER_LEG
    fee_per_round = (FEE_BPS_PER_FILL / 10_000) * CAPITAL_PER_LEG * FILLS_PER_ROUND

    total_gross = trades_df["gross_pnl"].sum()
    total_fees = fee_per_round * len(trades_df)
    net_pnl = total_gross - total_fees
    trades_df["net_pnl"] = trades_df["gross_pnl"] - fee_per_round
    winrate = (trades_df["net_pnl"] > 0).mean() * 100
    avg_hold = trades_df["hold_bars"].mean() * 60  # phút (1h -> *60)

    return {
        "Ngưỡng": threshold,
        "Trades": len(trades_df),
        "Gross": round(total_gross, 2),
        "Phí": round(total_fees, 2),
        "Net PnL": round(net_pnl, 2),
        "Winrate": round(winrate, 1),
        "Hold (phút)": round(avg_hold, 1),
        "Net/Trade": round(net_pnl / len(trades_df), 2),
    }


def main():
    df = load_data()
    print(f"Loaded {len(df)} bars sau khi tính rolling z-score (window={ROLLING_WINDOW})")

    results = [run_backtest_for_threshold(df, th) for th in THRESHOLDS]
    results_df = pd.DataFrame(results)

    results_df.to_csv("backtest_results_h1.csv", index=False)
    with open("backtest_results_table_h1.md", "w", encoding="utf-8") as f:
        f.write("# Backtest Pairs Trading: xyz:CL vs xyz:BRENTOIL (1h)\n\n")
        f.write(f"Giả định: ${CAPITAL_PER_LEG:,.0f}/leg, {FEE_BPS_PER_FILL}bps/fill, "
                f"{FILLS_PER_ROUND} fill/vòng, rolling window={ROLLING_WINDOW} nến\n\n")
        f.write(results_df.to_markdown(index=False))

    print(results_df.to_string(index=False))
    print("\nSaved -> backtest_results_table_h1.md, backtest_results_h1.csv")


if __name__ == "__main__":
    main()