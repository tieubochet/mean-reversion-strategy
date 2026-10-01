"""
=============================================================================
Pairs Trading Signal Bot — HỖ TRỢ NHIỀU CẶP (multi-pair)
=============================================================================
1 FILE Flask app duy nhất, 2 ROUTE nội bộ — giữ đúng kiến trúc gốc đã chạy
ổn định (không tự đoán nguồn request từ header, Flask nhận đúng request.path
thật /api hay /api/webhook nhờ vercel.json rewrites).

    GET/POST /api          -> cron-job.org ping mỗi 5 phút, quét các cặp đang
                               bật, gửi Telegram trạng thái mỗi lần quét.
    POST     /api/webhook  -> Telegram tự gọi mỗi khi có tin nhắn mới.
                               /check            -> trạng thái các cặp đang bật
                               /check <pair_id>  -> trạng thái 1 cặp cụ thể
                               /entry            -> gợi ý vào lệnh
                               /on <ids>         -> chỉ cron + /check báo các cặp này
                               (pair_id: "cl" / "xyz100" / "goldsilver" / "eurgbp" / "btceth" / "ethsol")

CẶP ĐANG THEO DÕI (mean/std/range = cửa sổ desk 1H, chốt 2026-10-01):
    1. cl         — xyz:CL vs xyz:BRENTOIL        — spread = price_A - price_B
                    mean=-4.391722 std=0.941472 mid_z=1.3 full_z=2.0
                    range [-7.772, -1.897] | hold 270h / full 292h (90d)
    2. xyz100     — xyz:XYZ100 vs xyz:SP500       — spread = ln(price_A / price_B)
                    mean=1.358061 std=0.020658 mid_z=1.5 full_z=2.1
                    range [1.3093, 1.4034] | hold 1113h / full 1301h (120d)
    3. goldsilver — xyz:GOLD vs xyz:SILVER        — spread = ln(price_A / price_B)
                    mean=4.218911 std=0.022074 mid_z=1.4 full_z=2.5
                    range [4.1696, 4.2829] | hold 227h / full 282.5h (90d)
    4. eurgbp     — xyz:EUR vs xyz:GBP            — spread = ln(price_A / price_B)
                    mean=-0.155131 std=0.003402 mid_z=1.75 full_z=2.5
                    range [-0.1663, -0.1414] | hold 145h / full 221h (90d)
    5. btceth     — BTC vs ETH                    — spread = ln(price_A / price_B)
                    mean=3.496143 std=0.046999 mid_z=1.5 full_z=2.1
                    range [3.4053, 3.5899] | hold 1077h / full 1200h (90d)
    6. ethsol     — ETH vs SOL                    — spread = ln(price_A / price_B)
                    mean=3.188690 std=0.050344 mid_z=1.5 full_z=2.5
                    range [3.0475, 3.2923] | hold 195h / full 317h (90d)

Không tính funding. Net PnL = expected PnL về mean − phí round-trip.

ENV VARS:
    Chung: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CRON_SECRET,
           TELEGRAM_WEBHOOK_SECRET, FEE_BPS_PER_FILL, FILLS_PER_ROUND
    Theo từng cặp (suffix _CL, _XYZ100, _GOLDSILVER, _EURGBP, _BTCETH, _ETHSOL):
           SPREAD_MEAN_<X>, SPREAD_STD_<X>, SIGNAL_THRESHOLD_<X>,
           MID_Z_<X>, FULL_Z_<X>, FULL_NEAR_PCT_<X>,
           RANGE_MIN_<X>, RANGE_MAX_<X>, EXIT_Z_THRESHOLD_<X>,
           EXPECTED_HOLD_DAYS_<X>, FULL_HOLD_HOURS_<X>, CAPITAL_PER_LEG_<X>
    Nếu Vercel còn SPREAD_MEAN_CL=-4.1789 thì default mới không có hiệu lực.
=============================================================================
"""

import os
import json
import math
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify

app = Flask(__name__)

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
INTERVAL = "15m"

VAR_STATS_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"
_VAR_LISTINGS_CACHE = {"ts": 0.0, "listings": None}
_VAR_CACHE_TTL_S = 8.0
_HL_MIDS_CACHE = {"ts": 0.0, "mids": None}
_HL_MIDS_TTL_S = 8.0
CANDLE_LOOKBACK_MS = 24 * 60 * 60 * 1000

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

FEE_BPS_PER_FILL = float(os.environ.get("FEE_BPS_PER_FILL", "2.2"))
FILLS_PER_ROUND = int(os.environ.get("FILLS_PER_ROUND", "4"))
NOTIFY_STATE_PATH = os.environ.get("NOTIFY_STATE_PATH", "/tmp/pairs_notify.json")
_NOTIFY_CACHE = {"ids": None}
_STATS_CACHE = {"day": None, "stats": None}
ICT = ZoneInfo("Asia/Ho_Chi_Minh")
WINDOW_DAYS = {"xyz100": 120}


def _pair_env(key: str, suffix: str, default: str) -> str:
    return os.environ.get(f"{key}_{suffix}", default)


PAIRS = [
    {
        "id": "cl",
        "label": "CL/BRENT",
        "venue": "hyperliquid",
        "symbol_a": "xyz:CL",
        "symbol_b": "xyz:BRENTOIL",
        "spread_type": "diff",
        "mean": float(_pair_env("SPREAD_MEAN", "CL", "-4.391722")),
        "std": float(_pair_env("SPREAD_STD", "CL", "0.941472")),
        "threshold": float(_pair_env("SIGNAL_THRESHOLD", "CL", "1.3")),
        "mid_z": float(_pair_env("MID_Z", "CL", "1.3")),
        "full_z": float(_pair_env("FULL_Z", "CL", "2.0")),
        "full_near_pct": float(_pair_env("FULL_NEAR_PCT", "CL", "0.03")),
        "range_min": float(_pair_env("RANGE_MIN", "CL", "-7.772")),
        "range_max": float(_pair_env("RANGE_MAX", "CL", "-1.897")),
        "exit_z": float(_pair_env("EXIT_Z_THRESHOLD", "CL", "0.0")),
        "expected_hold_days": float(_pair_env("EXPECTED_HOLD_DAYS", "CL", str(270.0 / 24))),
        "full_hold_hours": float(_pair_env("FULL_HOLD_HOURS", "CL", "292")),
        "capital_per_leg": float(_pair_env("CAPITAL_PER_LEG", "CL", "5000")),
    },
    {
        "id": "xyz100",
        "label": "XYZ100/SP500",
        "venue": "hyperliquid",
        "symbol_a": "xyz:XYZ100",
        "symbol_b": "xyz:SP500",
        "spread_type": "logratio",
        "mean": float(_pair_env("SPREAD_MEAN", "XYZ100", "1.358061")),
        "std": float(_pair_env("SPREAD_STD", "XYZ100", "0.020658")),
        "threshold": float(_pair_env("SIGNAL_THRESHOLD", "XYZ100", "1.5")),
        "mid_z": float(_pair_env("MID_Z", "XYZ100", "1.5")),
        "full_z": float(_pair_env("FULL_Z", "XYZ100", "2.1")),
        "full_near_pct": float(_pair_env("FULL_NEAR_PCT", "XYZ100", "0.03")),
        "range_min": float(_pair_env("RANGE_MIN", "XYZ100", "1.3093")),
        "range_max": float(_pair_env("RANGE_MAX", "XYZ100", "1.4034")),
        "exit_z": float(_pair_env("EXIT_Z_THRESHOLD", "XYZ100", "0.0")),
        "expected_hold_days": float(_pair_env("EXPECTED_HOLD_DAYS", "XYZ100", str(1113.0 / 24))),
        "full_hold_hours": float(_pair_env("FULL_HOLD_HOURS", "XYZ100", "1301")),
        "capital_per_leg": float(_pair_env("CAPITAL_PER_LEG", "XYZ100", "5000")),
    },
    {
        "id": "goldsilver",
        "label": "GOLD/SILVER",
        "venue": "hyperliquid",
        "symbol_a": "xyz:GOLD",
        "symbol_b": "xyz:SILVER",
        "spread_type": "logratio",
        "mean": float(_pair_env("SPREAD_MEAN", "GOLDSILVER", "4.218911")),
        "std": float(_pair_env("SPREAD_STD", "GOLDSILVER", "0.022074")),
        "threshold": float(_pair_env("SIGNAL_THRESHOLD", "GOLDSILVER", "1.4")),
        "mid_z": float(_pair_env("MID_Z", "GOLDSILVER", "1.4")),
        "full_z": float(_pair_env("FULL_Z", "GOLDSILVER", "2.5")),
        "full_near_pct": float(_pair_env("FULL_NEAR_PCT", "GOLDSILVER", "0.03")),
        "range_min": float(_pair_env("RANGE_MIN", "GOLDSILVER", "4.1696")),
        "range_max": float(_pair_env("RANGE_MAX", "GOLDSILVER", "4.2829")),
        "exit_z": float(_pair_env("EXIT_Z_THRESHOLD", "GOLDSILVER", "0.0")),
        "expected_hold_days": float(_pair_env("EXPECTED_HOLD_DAYS", "GOLDSILVER", str(227.0 / 24))),
        "full_hold_hours": float(_pair_env("FULL_HOLD_HOURS", "GOLDSILVER", "282.5")),
        "capital_per_leg": float(_pair_env("CAPITAL_PER_LEG", "GOLDSILVER", "5000")),
    },
    {
        "id": "eurgbp",
        "label": "EUR/GBP",
        "venue": "hyperliquid",
        "symbol_a": "xyz:EUR",
        "symbol_b": "xyz:GBP",
        "spread_type": "logratio",
        "mean": float(_pair_env("SPREAD_MEAN", "EURGBP", "-0.155131")),
        "std": float(_pair_env("SPREAD_STD", "EURGBP", "0.003402")),
        "threshold": float(_pair_env("SIGNAL_THRESHOLD", "EURGBP", "1.75")),
        "mid_z": float(_pair_env("MID_Z", "EURGBP", "1.75")),
        "full_z": float(_pair_env("FULL_Z", "EURGBP", "2.5")),
        "full_near_pct": float(_pair_env("FULL_NEAR_PCT", "EURGBP", "0.03")),
        "range_min": float(_pair_env("RANGE_MIN", "EURGBP", "-0.1663")),
        "range_max": float(_pair_env("RANGE_MAX", "EURGBP", "-0.1414")),
        "exit_z": float(_pair_env("EXIT_Z_THRESHOLD", "EURGBP", "0.0")),
        "expected_hold_days": float(_pair_env("EXPECTED_HOLD_DAYS", "EURGBP", str(145.0 / 24))),
        "full_hold_hours": float(_pair_env("FULL_HOLD_HOURS", "EURGBP", "221")),
        "capital_per_leg": float(_pair_env("CAPITAL_PER_LEG", "EURGBP", "5000")),
    },
    {
        "id": "btceth",
        "label": "BTC/ETH",
        "venue": "hyperliquid",
        "symbol_a": "BTC",
        "symbol_b": "ETH",
        "spread_type": "logratio",
        "mean": float(_pair_env("SPREAD_MEAN", "BTCETH", "3.496143")),
        "std": float(_pair_env("SPREAD_STD", "BTCETH", "0.046999")),
        "threshold": float(_pair_env("SIGNAL_THRESHOLD", "BTCETH", "1.5")),
        "mid_z": float(_pair_env("MID_Z", "BTCETH", "1.5")),
        "full_z": float(_pair_env("FULL_Z", "BTCETH", "2.1")),
        "full_near_pct": float(_pair_env("FULL_NEAR_PCT", "BTCETH", "0.03")),
        "range_min": float(_pair_env("RANGE_MIN", "BTCETH", "3.4053")),
        "range_max": float(_pair_env("RANGE_MAX", "BTCETH", "3.5899")),
        "exit_z": float(_pair_env("EXIT_Z_THRESHOLD", "BTCETH", "0.0")),
        "expected_hold_days": float(_pair_env("EXPECTED_HOLD_DAYS", "BTCETH", str(1077.0 / 24))),
        "full_hold_hours": float(_pair_env("FULL_HOLD_HOURS", "BTCETH", "1200")),
        "capital_per_leg": float(_pair_env("CAPITAL_PER_LEG", "BTCETH", "5000")),
    },
    {
        "id": "ethsol",
        "label": "ETH/SOL",
        "venue": "hyperliquid",
        "symbol_a": "ETH",
        "symbol_b": "SOL",
        "spread_type": "logratio",
        "mean": float(_pair_env("SPREAD_MEAN", "ETHSOL", "3.188690")),
        "std": float(_pair_env("SPREAD_STD", "ETHSOL", "0.050344")),
        "threshold": float(_pair_env("SIGNAL_THRESHOLD", "ETHSOL", "1.5")),
        "mid_z": float(_pair_env("MID_Z", "ETHSOL", "1.5")),
        "full_z": float(_pair_env("FULL_Z", "ETHSOL", "2.5")),
        "full_near_pct": float(_pair_env("FULL_NEAR_PCT", "ETHSOL", "0.03")),
        "range_min": float(_pair_env("RANGE_MIN", "ETHSOL", "3.0475")),
        "range_max": float(_pair_env("RANGE_MAX", "ETHSOL", "3.2923")),
        "exit_z": float(_pair_env("EXIT_Z_THRESHOLD", "ETHSOL", "0.0")),
        "expected_hold_days": float(_pair_env("EXPECTED_HOLD_DAYS", "ETHSOL", str(195.0 / 24))),
        "full_hold_hours": float(_pair_env("FULL_HOLD_HOURS", "ETHSOL", "317")),
        "capital_per_leg": float(_pair_env("CAPITAL_PER_LEG", "ETHSOL", "5000")),
    },
]

PAIRS_BY_ID = {p["id"]: p for p in PAIRS}


def _parse_pair_ids(raw: str):
    tokens = [t.strip().lower() for t in raw.replace(",", " ").split() if t.strip()]
    if not tokens or tokens == ["all"] or tokens == ["*"]:
        return [p["id"] for p in PAIRS], []
    unknown = [t for t in tokens if t not in PAIRS_BY_ID]
    ordered = [p["id"] for p in PAIRS if p["id"] in tokens]
    return ordered, unknown


def _read_notify_file():
    try:
        with open(NOTIFY_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        ids = data.get("ids")
        if ids == "*":
            parsed_ids = [p["id"] for p in PAIRS]
        elif isinstance(ids, list):
            parsed_ids = [p["id"] for p in PAIRS if p["id"] in ids]
        else:
            parsed_ids = None
        return parsed_ids, data.get("stats_day"), data.get("stats")
    except Exception:
        return None, None, None


def _write_state(ids, stats_day=None, stats=None):
    payload = {
        "ids": "*" if ids is None or set(ids) == {p["id"] for p in PAIRS} else list(ids),
        "stats_day": stats_day,
        "stats": stats,
    }
    try:
        with open(NOTIFY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"[WARN] notify state file: {e}")


def _write_notify_file(ids):
    _, day, stats = _read_notify_file()
    _write_state(ids, day, stats)


def _read_pin_text():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return ""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getChat",
            json={"chat_id": TELEGRAM_CHAT_ID},
            timeout=8,
        )
        pin = ((resp.json() or {}).get("result") or {}).get("pinned_message") or {}
        return pin.get("text") or ""
    except Exception as e:
        print(f"[WARN] notify pin: {e}")
        return ""


def _parse_pin(text: str):
    ids = None
    day = None
    stats = None
    for line in (text or "").splitlines():
        if line.startswith("NOTIFY_ON:"):
            body = line.split(":", 1)[1].strip()
            if body in ("*", "all"):
                ids = [p["id"] for p in PAIRS]
            else:
                ids = [p["id"] for p in PAIRS if p["id"] in {t.strip() for t in body.split(",")}]
        elif line.startswith("STATS:"):
            parts = line.split(":", 1)[1].split()
            if not parts:
                continue
            day = parts[0]
            stats = {}
            for part in parts[1:]:
                if "=" not in part:
                    continue
                pid, vals = part.split("=", 1)
                bits = vals.split(",")
                if len(bits) != 4 or pid not in PAIRS_BY_ID:
                    continue
                stats[pid] = {
                    "mean": float(bits[0]),
                    "std": float(bits[1]),
                    "min": float(bits[2]),
                    "max": float(bits[3]),
                }
    return ids, day, stats


def _read_notify_pin():
    ids, _, _ = _parse_pin(_read_pin_text())
    return ids


def _pin_state(ids, stats_day=None, stats=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    body = "*" if set(ids) == {p["id"] for p in PAIRS} else ",".join(ids)
    lines = [f"NOTIFY_ON:{body}"]
    if stats_day and stats:
        bits = []
        for pair in PAIRS:
            st = stats.get(pair["id"])
            if not st:
                continue
            bits.append(
                f"{pair['id']}={st['mean']:.6f},{st['std']:.6f},{st['min']:.4f},{st['max']:.4f}"
            )
        lines.append(f"STATS:{stats_day} " + " ".join(bits))
    try:
        sent = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": "\n".join(lines), "disable_notification": True},
            timeout=8,
        ).json()
        mid = ((sent or {}).get("result") or {}).get("message_id")
        if not mid:
            return False
        pinned = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/pinChatMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "message_id": mid, "disable_notification": True},
            timeout=8,
        ).json()
        return bool((pinned or {}).get("ok"))
    except Exception as e:
        print(f"[WARN] pin notify: {e}")
        return False


def _pin_notify_state(ids):
    _, day, stats = _parse_pin(_read_pin_text())
    return _pin_state(ids, day, stats)


def enabled_pair_ids(refresh=False):
    if not refresh and _NOTIFY_CACHE["ids"] is not None:
        return _NOTIFY_CACHE["ids"]
    ids = _read_notify_pin()
    if ids is None:
        ids, _, _ = _read_notify_file()
    if ids is None:
        raw = os.environ.get("ENABLED_PAIRS", "").strip()
        ids = _parse_pair_ids(raw)[0] if raw else [p["id"] for p in PAIRS]
    else:
        _write_notify_file(ids)
    _NOTIFY_CACHE["ids"] = ids
    return ids


def active_pairs(refresh=False):
    allow = set(enabled_pair_ids(refresh=refresh))
    return [p for p in PAIRS if p["id"] in allow]


def set_enabled_pairs(ids):
    _NOTIFY_CACHE["ids"] = list(ids)
    _write_notify_file(ids)
    return _pin_notify_state(ids)


def notify_status_text():
    on = enabled_pair_ids()
    off = [p["id"] for p in PAIRS if p["id"] not in set(on)]
    lines = ["*THÔNG BÁO CẶP*", "Đang bật: " + ", ".join(f"`{i}`" for i in on)]
    if off:
        lines.append("Đang ẩn: " + ", ".join(f"`{i}`" for i in off))
    else:
        lines.append("Đang ẩn: không")
    lines.append("Đặt lại: `/on cl, xyz100, goldsilver, eurgbp`")
    lines.append("Bật hết: `/on all`")
    return "\n".join(lines)


def ict_slot(now=None):
    now = now or datetime.now(ICT)
    slot = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now < slot:
        slot -= timedelta(days=1)
    return slot


def apply_stats(stats):
    if not stats:
        return
    for pair in PAIRS:
        st = stats.get(pair["id"])
        if not st or st.get("std", 0) <= 0:
            continue
        pair["mean"] = float(st["mean"])
        pair["std"] = float(st["std"])
        pair["range_min"] = float(st["min"])
        pair["range_max"] = float(st["max"])


def _load_saved_stats():
    if _STATS_CACHE["stats"]:
        return _STATS_CACHE["day"], _STATS_CACHE["stats"]
    _, day, stats = _parse_pin(_read_pin_text())
    if not stats:
        _, day, stats = _read_notify_file()
    if stats:
        _STATS_CACHE["day"] = day
        _STATS_CACHE["stats"] = stats
    return day, stats


def _fetch_hour_closes(coin: str, start: int, end: int):
    resp = requests.post(
        HL_INFO_URL,
        json={"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": start, "endTime": end}},
        timeout=12,
    )
    resp.raise_for_status()
    candles = resp.json()
    if not isinstance(candles, list) or not candles:
        raise RuntimeError(f"không có nến 1h {coin}")
    return candles


def _window_stats(pair, closes, end_ms):
    days = WINDOW_DAYS.get(pair["id"], 90)
    start = end_ms - days * 24 * 3600 * 1000
    by_b = {c["t"]: float(c["c"]) for c in closes[pair["symbol_b"]] if c["t"] >= start}
    spreads = []
    for ca in closes[pair["symbol_a"]]:
        if ca["t"] < start or ca["t"] > end_ms:
            continue
        pb = by_b.get(ca["t"])
        pa = float(ca["c"])
        if pb is None or pa <= 0 or pb <= 0:
            continue
        spreads.append(compute_spread(pa, pb, pair["spread_type"]))
    if len(spreads) < 48:
        raise RuntimeError(f"quá ít nến {pair['id']}")
    mean = sum(spreads) / len(spreads)
    var = sum((x - mean) ** 2 for x in spreads) / len(spreads)
    return {"mean": mean, "std": math.sqrt(var), "min": min(spreads), "max": max(spreads), "n": len(spreads)}


def maybe_refresh_pair_stats():
    """08:00 ICT: chốt mean/std/range cửa sổ 1H (90d, XYZ100 120d) như desk."""
    day, stats = _load_saved_stats()
    apply_stats(stats)
    slot = ict_slot()
    slot_key = slot.date().isoformat()
    if day == slot_key:
        return slot_key, False
    if datetime.now(ICT) < slot.replace(hour=8):
        return day, False
    end_ms = int(slot.timestamp() * 1000)
    coins = sorted({p[k] for p in PAIRS for k in ("symbol_a", "symbol_b")})
    start = end_ms - 120 * 24 * 3600 * 1000
    closes = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_fetch_hour_closes, coin, start, end_ms): coin for coin in coins}
        for fut in as_completed(futs):
            coin = futs[fut]
            try:
                closes[coin] = fut.result()
            except Exception as e:
                print(f"[WARN] refresh {coin}: {e}")
    fresh = {}
    for pair in PAIRS:
        if pair["symbol_a"] not in closes or pair["symbol_b"] not in closes:
            continue
        try:
            fresh[pair["id"]] = _window_stats(pair, closes, end_ms)
        except Exception as e:
            print(f"[WARN] refresh {pair['id']}: {e}")
    if len(fresh) < len(PAIRS):
        print(f"[WARN] refresh incomplete {len(fresh)}/{len(PAIRS)}")
        return day, False
    apply_stats(fresh)
    _STATS_CACHE["day"] = slot_key
    _STATS_CACHE["stats"] = fresh
    ids = enabled_pair_ids()
    _write_state(ids, slot_key, fresh)
    _pin_state(ids, slot_key, fresh)
    return slot_key, True


def fee_per_round(pair: dict) -> float:
    return (FEE_BPS_PER_FILL / 10_000) * pair["capital_per_leg"] * FILLS_PER_ROUND


def fetch_xyz_mids() -> dict:
    now = time.time()
    cached = _HL_MIDS_CACHE["mids"]
    if cached is not None and (now - _HL_MIDS_CACHE["ts"]) < _HL_MIDS_TTL_S:
        return cached
    merged = {}
    for payload in ({"type": "allMids"}, {"type": "allMids", "dex": "xyz"}):
        try:
            resp = requests.post(HL_INFO_URL, json=payload, timeout=8)
            resp.raise_for_status()
            mids = resp.json() or {}
            if isinstance(mids, dict):
                merged.update(mids)
        except Exception as e:
            print(f"[WARN] allMids {payload} failed ({e})")
    if not merged:
        raise RuntimeError("Hyperliquid allMids empty")
    _HL_MIDS_CACHE["ts"] = now
    _HL_MIDS_CACHE["mids"] = merged
    return merged


def fetch_mid_price(coin: str) -> float:
    mids = fetch_xyz_mids()
    raw = mids.get(coin)
    if raw is None:
        raise RuntimeError(f"allMids missing {coin}")
    px = float(raw)
    if px <= 0:
        raise RuntimeError(f"allMids invalid price for {coin}: {raw}")
    return px


def fetch_latest_close(coin: str) -> float:
    now_ms = int(time.time() * 1000)
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": INTERVAL,
            "startTime": now_ms - CANDLE_LOOKBACK_MS,
            "endTime": now_ms,
        },
    }
    try:
        resp = requests.post(HL_INFO_URL, json=payload, timeout=8)
        resp.raise_for_status()
        candles = resp.json()
        if candles:
            return float(candles[-1]["c"])
        print(f"[WARN] no {INTERVAL} candles in 24h for {coin}, fallback allMids")
    except Exception as e:
        print(f"[WARN] candleSnapshot {coin} failed ({e}), fallback allMids")
    return fetch_mid_price(coin)


def fetch_variational_listings() -> list:
    now = time.time()
    cached = _VAR_LISTINGS_CACHE["listings"]
    if cached is not None and (now - _VAR_LISTINGS_CACHE["ts"]) < _VAR_CACHE_TTL_S:
        return cached
    resp = requests.get(VAR_STATS_URL, timeout=8)
    resp.raise_for_status()
    listings = resp.json().get("listings") or []
    if not listings:
        raise RuntimeError("Variational /metadata/stats returned empty listings")
    _VAR_LISTINGS_CACHE["ts"] = now
    _VAR_LISTINGS_CACHE["listings"] = listings
    return listings


def fetch_variational_pair(symbol_a: str, symbol_b: str) -> dict:
    listings = fetch_variational_listings()
    by_ticker = {str(x.get("ticker")): x for x in listings}
    missing = [s for s in (symbol_a, symbol_b) if s not in by_ticker]
    if missing:
        raise RuntimeError(f"Variational listings missing ticker(s): {missing}")
    la, lb = by_ticker[symbol_a], by_ticker[symbol_b]
    return {"price_a": float(la["mark_price"]), "price_b": float(lb["mark_price"])}


def compute_spread(price_a: float, price_b: float, spread_type: str) -> float:
    if spread_type == "logratio":
        return math.log(price_a / price_b)
    return price_a - price_b


def compute_zscore(pair: dict) -> dict:
    symbol_a, symbol_b = pair["symbol_a"], pair["symbol_b"]
    if pair.get("venue") == "variational":
        var = fetch_variational_pair(symbol_a, symbol_b)
        price_a, price_b = var["price_a"], var["price_b"]
    else:
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_a = ex.submit(fetch_latest_close, symbol_a)
            fut_b = ex.submit(fetch_latest_close, symbol_b)
            price_a = fut_a.result()
            price_b = fut_b.result()
    spread = compute_spread(price_a, price_b, pair["spread_type"])
    std = pair["std"]
    z = (spread - pair["mean"]) / std if std > 0 else 0.0
    return {"spread": spread, "z": z, "price_A": price_a, "price_B": price_b}


def suggest_exit_level(pair: dict, z: float) -> dict:
    exit_z = pair["exit_z"] if z > 0 else -pair["exit_z"]
    exit_z = exit_z if exit_z != 0 else 0.0
    exit_spread = pair["mean"] + exit_z * pair["std"]
    return {"exit_z": exit_z, "exit_spread": exit_spread}


def estimate_expected_pnl(pair: dict, stats: dict) -> float:
    deviation = abs(stats["spread"] - pair["mean"])
    if pair["spread_type"] == "logratio":
        return deviation * pair["capital_per_leg"]
    avg_price = (stats["price_A"] + stats["price_B"]) / 2
    units_per_leg = pair["capital_per_leg"] / max(avg_price, 1)
    return deviation * units_per_leg


def evaluate_signal(pair: dict) -> dict:
    stats = compute_zscore(pair)
    z = stats["z"]
    expected_pnl = estimate_expected_pnl(pair, stats)
    fee = fee_per_round(pair)
    net_expected = expected_pnl - fee
    exit_level = suggest_exit_level(pair, z)
    result = {
        "pair_id": pair["id"], "pair_label": pair["label"],
        "z": z, "spread": stats["spread"],
        "price_A": stats["price_A"], "price_B": stats["price_B"],
        "expected_pnl": expected_pnl,
        "fee_per_round": fee,
        "net_expected": net_expected,
        "exit_level": exit_level,
        "should_enter": False,
        "reason": "z-score dưới ngưỡng",
    }
    if abs(z) >= pair["threshold"] and net_expected > 0:
        result["should_enter"] = True
        result["reason"] = "Đủ điều kiện vào lệnh (net kỳ vọng > 0)"
    elif abs(z) >= pair["threshold"]:
        result["reason"] = "Z-score đủ ngưỡng nhưng net kỳ vọng <= 0 (phí ăn hết lợi nhuận)"
    return result


def send_telegram_message(text: str, chat_id: str = None):
    target_chat_id = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target_chat_id:
        print(f"[TG] Missing token/chat_id, would have sent: {text}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": target_chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[ERROR] Gửi Telegram thất bại: {e}")


def _fmt_px(price: float) -> str:
    return f"{price:.4f}" if abs(price) < 20 else f"{price:.2f}"


def _direction_text(pair: dict, z: float) -> str:
    if z > 0:
        return f"🔴 SHORT {pair['symbol_a']} / LONG {pair['symbol_b']}"
    return f"🟢 LONG {pair['symbol_a']} / SHORT {pair['symbol_b']}"


def _near_spread_level(spread: float, level: float, pair: dict, pct: float) -> bool:
    if level is None:
        return False
    level = float(level)
    if pair.get("spread_type") == "logratio":
        return abs(spread - level) <= math.log(1.0 + pct)
    return abs(spread - level) / max(abs(level), 1e-9) <= pct


def classify_range_zone(pair: dict, result: dict):
    mid_z = float(pair.get("mid_z", pair.get("threshold", 1.3)))
    full_z = float(pair.get("full_z", 2.0))
    pct = float(pair.get("full_near_pct", 0.03))
    az = abs(result["z"])
    sp = result["spread"]
    full_hi = pair["mean"] + full_z * pair["std"]
    full_lo = pair["mean"] - full_z * pair["std"]
    rmin = pair.get("range_min")
    rmax = pair.get("range_max")
    levels = [full_hi, full_lo]
    if rmin is not None:
        levels.append(rmin)
    if rmax is not None:
        levels.append(rmax)
    at_full_moc = any(_near_spread_level(sp, lv, pair, pct) for lv in levels)
    extreme_lo = min([lv for lv in (full_lo, rmin) if lv is not None], default=full_lo)
    extreme_hi = max([lv for lv in (full_hi, rmax) if lv is not None], default=full_hi)
    beyond_extreme = sp <= extreme_lo or sp >= extreme_hi
    if az >= full_z and (at_full_moc or beyond_extreme):
        return "FULL"
    if az >= mid_z:
        return "MID"
    return None


def build_status_message(pair: dict, result: dict) -> str:
    z = result["z"]
    net = result.get("net_expected")
    net_txt = f"${net:.2f}" if net is not None else "n/a"
    exit_spread = (result.get("exit_level") or {}).get("exit_spread", pair["mean"])
    mid_z = float(pair.get("mid_z", pair.get("threshold", 1.3)))
    full_z = float(pair.get("full_z", 2.0))
    sign = 1 if z > 0 else -1
    mid_lvl = pair["mean"] + mid_z * pair["std"] * sign
    full_lvl = pair["mean"] + full_z * pair["std"] * sign
    hold_h = pair["expected_hold_days"] * 24
    zone = classify_range_zone(pair, result)
    if zone == "FULL":
        action = "FULL RANGE — VÀO LỆNH"
        hold_h = float(pair.get("full_hold_hours", hold_h))
    elif zone == "MID":
        action = "MID RANGE — VÀO LỆNH"
    else:
        action = "CHƯA NÊN VÀO"
    return (
        "--------------------------------\n\n"
        f"*{pair['label']} — {action}*\n"
        f"{_direction_text(pair, z)}\n\n"
        f"Spread: `{result['spread']:.4f}`\n"
        f"Giá {pair['symbol_a']}: `${_fmt_px(result['price_A'])}` | "
        f"Giá {pair['symbol_b']}: `${_fmt_px(result['price_B'])}`\n"
        f"Mean `{pair['mean']:.4f}` | Mid `{mid_lvl:.3f}` | Full `{full_lvl:.3f}`\n\n"
        f"*Net PnL nếu vào giờ → về mean: `{net_txt}`*\n"
        f"Đóng khi spread về `{exit_spread:.4f}`\n"
        f"Hold TB ~{hold_h:.0f}h"
    )


def build_signal_message(pair: dict, result: dict) -> str:
    return build_status_message(pair, result)


def build_check_message(pair: dict, result: dict) -> str:
    return build_status_message(pair, result)


HELP_TEXT = (
    "*PAIRS BOT — MULTI-PAIR*\n"
    "Đang theo dõi 6 cặp:\n"
    "• `cl` — CL/BRENT (WTI vs Brent) — Hyperliquid HIP-3\n"
    "• `xyz100` — XYZ100/SP500\n"
    "• `goldsilver` — GOLD/SILVER\n"
    "• `eurgbp` — EUR/GBP (`xyz:EUR` vs `xyz:GBP`)\n"
    "• `btceth` — BTC/ETH (perp chính)\n"
    "• `ethsol` — ETH/SOL (perp chính)\n\n"
    "Gõ /check để xem các cặp đang bật.\n"
    "Gõ /check cl, xyz100, goldsilver, eurgbp, btceth hoặc ethsol để xem riêng 1 cặp.\n"
    "Gõ /on cl, xyz100, goldsilver, eurgbp để chỉ báo những cặp đó, ẩn phần còn lại.\n"
    "Gõ /on all để bật lại hết. Gõ /on để xem đang bật/ẩn.\n"
    "Gõ /entry để xem gợi ý vào lệnh.\n"
    "Cron chỉ gửi các cặp đang bật, không cần đủ ngưỡng."
)

PAIRS_TEXT = (
    "*GỢI Ý VÀO LỆNH*\n\n"
    "🟢 LONG BRENTOIL / SHORT CL khi Net PnL <= 60 \n"
    "🔴 SHORT BRENTOIL / LONG CL khi Net PnL >= 90 \n\n"
    "Chia vốn thành 4-5 phần, cứ 10 giá dca 2k/leg\n"
    "Lưu ý: Net PnL dao động từ *30 đến 150*, chỉ vào lệnh khi Net PnL <= 60 hoặc >= 90.\n"
    "--------------------------------\n"
    "🟢 LONG QQQ / SHORT US500 khi Net PnL >= 190 \n"
    "🔴 SHORT QQQ / LONG US500 khi Net PnL <= 160 \n\n"
    "Chia vốn thành 4-5 phần, cứ 20 - 30 giá dca 2k/leg\n"
    "Lưu ý: Net PnL dao động từ *120 đến 350*, chỉ vào lệnh khi Net PnL >= 190 hoặc <= 160.\n"
    "--------------------------------\n"
    "🟢 LONG GOLD / SHORT SILVER khi Net PnL >= 150 \n"
    "🔴 SHORT GOLD / LONG SILVER khi Net PnL <= 50 \n\n"
    "Chia vốn thành 4-5 phần, cứ 35 - 45 giá dca 2k/leg\n"
    "Lưu ý: Net PnL dao động từ *20 đến 200*, chỉ vào lệnh khi Net PnL <= 50 hoặc >= 150.\n"
    "--------------------------------\n"
    "🟢 LONG EUR / SHORT GBP khi Net PnL >= 35 \n"
    "🔴 SHORT EUR / LONG GBP khi Net PnL >= 35 \n\n"
    "Chia vốn thành 4-5 phần, cứ ~20-30 pip chéo dca 2k/leg\n"
    "Lưu ý: Net PnL dao động từ *15 đến 65* (spread rất chặt). Chỉ vào khi Net >= 35 "
    "(~z 2.0). GBP HIP-3 thanh khoản mỏng hơn EUR — canh slippage.\n"
    "--------------------------------\n"
    "🟢 LONG BTC / SHORT ETH khi Net PnL >= 250 \n"
    "🔴 SHORT BTC / LONG ETH khi Net PnL >= 250 \n\n"
    "Chia vốn thành 4-5 phần, dca 2k/leg\n"
    "Lưu ý: Net mid ~370 / full ~520 (1H 90d). Band *150–600*. Hold TB dài (~1077h).\n"
    "--------------------------------\n"
    "🟢 LONG ETH / SHORT SOL khi Net PnL >= 280 \n"
    "🔴 SHORT ETH / LONG SOL khi Net PnL >= 280 \n\n"
    "Chia vốn thành 4-5 phần, dca 2k/leg\n"
    "Lưu ý: Net mid ~409 / full ~682 (1H 90d). Band *150–700*. Hold TB ~195h.\n\n\n"
    "*Giải thích*:\n"
    "2k/leg: 2k long và 2k short\n"
    "Net PnL: Lợi nhuận ròng đang tính với vol 5k/leg (không gồm funding)\n\n\n"
    "*LUÔN KỶ LUẬT KHI VÀO LỆNH*"
)


def result_to_json(result: dict) -> dict:
    response = {
        "pair_id": result["pair_id"], "pair_label": result["pair_label"],
        "z": round(result["z"], 4), "spread": round(result["spread"], 4),
        "should_enter": result["should_enter"], "reason": result["reason"],
    }
    if "net_expected" in result:
        response["expected_pnl"] = round(result["expected_pnl"], 2)
        response["fee_per_round"] = round(result["fee_per_round"], 2)
        response["net_expected"] = round(result["net_expected"], 2)
        response["suggested_exit_z"] = round(result["exit_level"]["exit_z"], 4)
        response["suggested_exit_spread"] = round(result["exit_level"]["exit_spread"], 4)
    return response


def check_cron_auth() -> bool:
    if not CRON_SECRET:
        return True
    return request.headers.get("Authorization", "") == f"Bearer {CRON_SECRET}"


def check_telegram_secret() -> bool:
    if not TELEGRAM_WEBHOOK_SECRET:
        return True
    return request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") == TELEGRAM_WEBHOOK_SECRET


@app.route("/", methods=["GET", "POST"])
@app.route("/api", methods=["GET", "POST"])
@app.route("/api/", methods=["GET", "POST"])
@app.route("/api/index", methods=["GET", "POST"])
def scan_bot():
    if not check_cron_auth():
        return jsonify({"error": "Unauthorized"}), 401

    results = {}
    errors = {}
    sections = []
    slot_key, refreshed = maybe_refresh_pair_stats()
    shown = active_pairs(refresh=True)
    hidden = [p["id"] for p in PAIRS if p["id"] not in {x["id"] for x in shown}]
    for pair in shown:
        try:
            result = evaluate_signal(pair)
            results[pair["id"]] = result_to_json(result)
            sections.append(build_check_message(pair, result))
        except Exception as e:
            print(f"[ERROR] scan_bot pair={pair['id']}: {e}")
            errors[pair["id"]] = str(e)
            sections.append(f"*{pair['label']}*\n❌ Lỗi: `{e}`")

    if sections:
        hide_txt = ("\n\n\n Đang ẩn: " + ", ".join(f"`{i}`" for i in hidden)) if hidden else ""
        stamp = f"\nParams chốt 08:00 ICT {slot_key or 'default'}" + (" — vừa tính lại" if refreshed else "")
        send_telegram_message(
            "*[SCAN]*\n\n"
            + "\n\n".join(sections)
            + hide_txt
            + stamp
            + "\n\n\nGõ /check để xem giá hiện tại"
            + "\n\n\n[Click xem dữ liệu real-time!](https://spread-desk-realtime.vercel.app/)"
        )

    status_code = 200 if not errors or results else 500
    return jsonify({"results": results, "errors": errors, "hidden": hidden}), status_code


@app.route("/api/webhook", methods=["GET", "POST"])
@app.route("/webhook", methods=["GET", "POST"])
def telegram_webhook():
    if not check_telegram_secret():
        return jsonify({"ok": True}), 200

    update = request.get_json(silent=True)
    if not update:
        return jsonify({"ok": True}), 200

    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify({"ok": True}), 200

    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        return jsonify({"ok": True}), 200

    parts = text.split(maxsplit=1)
    command = parts[0].split("@")[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    arg = rest.split()[0].lower().rstrip(",") if rest else None

    try:
        if command in ("/start", "/help"):
            send_telegram_message(HELP_TEXT, chat_id=chat_id)
        elif command == "/entry":
            send_telegram_message(PAIRS_TEXT, chat_id=chat_id)
        elif command == "/on":
            if not rest:
                send_telegram_message(notify_status_text(), chat_id=chat_id)
            else:
                ids, unknown = _parse_pair_ids(rest)
                if unknown:
                    send_telegram_message(
                        "Không nhận: " + ", ".join(f"`{u}`" for u in unknown)
                        + "\nHợp lệ: " + ", ".join(f"`{p['id']}`" for p in PAIRS),
                        chat_id=chat_id,
                    )
                elif not ids:
                    send_telegram_message("Danh sách trống. Ví dụ: `/on cl, xyz100`", chat_id=chat_id)
                else:
                    pinned = set_enabled_pairs(ids)
                    note = "" if pinned else "\n\nKhông ghim được tin trạng thái — cron instance mới có thể chưa thấy cho tới khi warm."
                    send_telegram_message(notify_status_text() + note, chat_id=chat_id)
        elif command == "/check":
            if arg and arg in PAIRS_BY_ID:
                pair = PAIRS_BY_ID[arg]
                result = evaluate_signal(pair)
                msg = f"*[CHECK] {pair['label']}*\n\n" + build_check_message(pair, result)
                send_telegram_message(msg, chat_id=chat_id)
            elif arg:
                send_telegram_message(
                    f"Không tìm thấy cặp `{arg}`. Các cặp hợp lệ: "
                    + ", ".join(f"`{pid}`" for pid in PAIRS_BY_ID),
                    chat_id=chat_id,
                )
            else:
                sections = []
                for pair in active_pairs():
                    result = evaluate_signal(pair)
                    sections.append(build_check_message(pair, result))
                hidden = [p["id"] for p in PAIRS if p["id"] not in {x["id"] for x in active_pairs()}]
                tail = ""
                if hidden:
                    tail = "\n\n\nĐang ẩn: " + ", ".join(f"`{i}`" for i in hidden) + ". `/on all` để hiện lại."
                msg = (
                    "*[CHECK] PAIRS STATUS*\n\n"
                    + "\n\n".join(sections)
                    + tail
                    + "\nGõ /check để xem giá hiện tại"
                    + "\n\n\n[Click xem dữ liệu real-time!](https://spread-desk-realtime.vercel.app/)"
                )
                send_telegram_message(msg, chat_id=chat_id)
        elif command:
            send_telegram_message(
                "Lệnh không hợp lệ. Gõ /on, /check, /check <pair_id> hoặc /entry.",
                chat_id=chat_id,
            )
    except Exception as e:
        send_telegram_message(f"❌ Lỗi: {e}", chat_id=chat_id)

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
