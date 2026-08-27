"""
Script phụ trợ: liệt kê TOÀN BỘ symbol đang có trên dex "xyz" (Trade.xyz,
HIP-3 deployer đang host xyz:CL, xyz:BRENTOIL, xyz:XYZ100, xyz:SP500...).

Mục đích: xác nhận ĐÚNG tên ticker cho Gold và Silver trước khi viết script
fetch data / backtest — vì HIP-3 không có quy tắc naming cố định (VD: oil
dùng "xyz:CL"/"xyz:BRENTOIL" chứ không phải "xyz:WTI"/"xyz:BRENT"), nên đoán
mò rồi backtest sai symbol sẽ ra dữ liệu rác hoặc lỗi.

Cách chạy:
    pip install requests
    python list_xyz_universe.py

Output: in ra toàn bộ tên symbol trong universe của dex "xyz". Tìm dòng nào
chứa "GOLD"/"XAU"/"SILVER"/"XAG" (không phân biệt hoa thường) để lấy đúng
tên — sau đó điền vào GOLD_SYMBOL / SILVER_SYMBOL trong
fetch_spread_stats_goldsilver.py.
"""

import requests

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HIP3_DEX = "xyz"


def main():
    payload = {"type": "metaAndAssetCtxs", "dex": HIP3_DEX}
    resp = requests.post(HL_INFO_URL, json=payload, timeout=15)
    resp.raise_for_status()
    meta, asset_ctxs = resp.json()

    universe = meta["universe"]
    print(f"Tổng {len(universe)} symbol trên dex '{HIP3_DEX}':\n")
    for asset in universe:
        print(f"  {asset['name']}")

    print("\n--- Có thể liên quan tới Gold/Silver ---")
    keywords = ("gold", "silver", "xau", "xag")
    found = False
    for asset in universe:
        name = asset["name"]
        if any(k in name.lower() for k in keywords):
            print(f"  {name}")
            found = True
    if not found:
        print("  (Không tìm thấy — kiểm tra lại tên thủ công trong danh sách đầy đủ ở trên,"
              " hoặc gold/silver có thể đang nằm trên 1 dex HIP-3 khác, không phải 'xyz'.)")


if __name__ == "__main__":
    main()
