"""
Murento AI Trader — standalone scanner for GitHub Actions.

Replicates the exact cascade strategy from the browser dashboard:
  1) H4 EMA60 bias                              (30 pts)
  2) Daily/4H POI (SBR/RBS zone) in range        (20 pts)
  3) Rejection candle on confirmation timeframe  (25 pts)   4H if Daily POI, 1H if 4H POI
  4) Structure shift + order block on entry TF   (25 pts)   30M if Daily POI, 15M if 4H POI
  Total 100. Fires (sends alert) at 80+.

TP1 = next real HTF zone/liquidity level ahead (falls back to 1:10 R:R if none).
TP2 = always a fixed 1:10 R:R target.

Runs once per invocation — GitHub Actions calls this on a schedule (see
.github/workflows/scan.yml). State (which setups have already been alerted)
persists in state.json, committed back to the repo by the workflow, so it
survives between runs on GitHub's ephemeral runners.
"""

import os
import sys
import json
import time
import requests

TWELVEDATA_KEY = os.environ["TWELVEDATA_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_FILE = "state.json"

PAIRS = [
    {"symbol": "XAU/USD", "label": "XAUUSD"},
    {"symbol": "BTC/USD", "label": "BTCUSD"},
    {"symbol": "EUR/USD", "label": "EURUSD"},
]

TF_MAP = {
    "daily": "1day",
    "H4": "4h",
    "60min": "1h",
    "30min": "30min",
    "15min": "15min",
}

# --- Rate limiting: TwelveData free tier allows ~8 credits/min. Stay under it. ---
MIN_SECONDS_BETWEEN_CALLS = 8.5  # ~7 calls/min, safety margin under the real 8/min cap


def fmt(n):
    return f"{n:.2f}"


def fetch_candles(symbol, tf_key):
    """Fetch OHLC candles for one symbol/timeframe. Returns list of dicts, oldest first."""
    interval = TF_MAP[tf_key]
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 100,
        "apikey": TWELVEDATA_KEY,
    }
    resp = requests.get(url, params=params, timeout=20)
    data = resp.json()

    if isinstance(data, dict) and (data.get("status") == "error" or (isinstance(data.get("code"), int) and data["code"] >= 400)):
        print(f"  [error] {symbol} {tf_key}: {data.get('message', data)}")
        return None

    values = data.get("values")
    if not values or not isinstance(values, list):
        print(f"  [error] {symbol} {tf_key}: unexpected response shape: {str(data)[:200]}")
        return None

    candles = [
        {
            "time": v["datetime"],
            "open": float(v["open"]),
            "high": float(v["high"]),
            "low": float(v["low"]),
            "close": float(v["close"]),
        }
        for v in reversed(values)
    ]
    return candles


def ema(values, period):
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def find_swings(candles, lookback=3):
    swing_highs, swing_lows = [], []
    n = len(candles)
    for i in range(lookback, n - lookback):
        c = candles[i]
        is_high, is_low = True, True
        for j in range(i - lookback, i + lookback + 1):
            if j == i:
                continue
            if candles[j]["high"] > c["high"]:
                is_high = False
            if candles[j]["low"] < c["low"]:
                is_low = False
        if is_high:
            swing_highs.append({"index": i, "price": c["high"]})
        if is_low:
            swing_lows.append({"index": i, "price": c["low"]})
    return swing_highs, swing_lows


def compute_htf_structure(candles):
    closes = [c["close"] for c in candles]
    last_close = closes[-1]
    e60 = ema(closes, 60)[-1]
    bullish = last_close > e60
    swing_highs, swing_lows = find_swings(candles, 3)

    MAX_ZONES_PER_TYPE = 2
    rbs_zones = [
        {"type": "RBS", "price": sh["price"], "index": sh["index"]}
        for sh in swing_highs
        if any(c["close"] > sh["price"] for c in candles[sh["index"] + 1:])
    ][-MAX_ZONES_PER_TYPE:]
    sbr_zones = [
        {"type": "SBR", "price": sl["price"], "index": sl["index"]}
        for sl in swing_lows
        if any(c["close"] < sl["price"] for c in candles[sl["index"] + 1:])
    ][-MAX_ZONES_PER_TYPE:]
    zones = rbs_zones + sbr_zones

    unbroken_highs = [
        sh["price"] for sh in swing_highs
        if not any(c["close"] > sh["price"] for c in candles[sh["index"] + 1:])
    ]
    unbroken_lows = [
        sl["price"] for sl in swing_lows
        if not any(c["close"] < sl["price"] for c in candles[sl["index"] + 1:])
    ]

    return {
        "bullish": bullish,
        "ema60": e60,
        "last_close": last_close,
        "zones": zones,
        "unbroken_highs": unbroken_highs,
        "unbroken_lows": unbroken_lows,
    }


def is_rejection_candle(c, bullish):
    body = abs(c["close"] - c["open"])
    rng = c["high"] - c["low"]
    if rng <= 0:
        return False
    if bullish:
        lower_wick = min(c["open"], c["close"]) - c["low"]
        return lower_wick > body * 1.3 and lower_wick > rng * 0.4 and c["close"] > c["open"]
    else:
        upper_wick = c["high"] - max(c["open"], c["close"])
        return upper_wick > body * 1.3 and upper_wick > rng * 0.4 and c["close"] < c["open"]


def find_rejection_at_zone(candles, zone_price, tolerance, bullish, lookback=8):
    if not candles or len(candles) < lookback:
        return None
    recent = candles[-lookback:]
    for c in reversed(recent):
        touched = (
            (c["low"] <= zone_price + tolerance and c["low"] >= zone_price - tolerance * 4)
            if bullish else
            (c["high"] >= zone_price - tolerance and c["high"] <= zone_price + tolerance * 4)
        )
        if touched and is_rejection_candle(c, bullish):
            return c
    return None


def find_mss_and_order_block(candles, bullish):
    if not candles or len(candles) < 20:
        return None
    swing_highs, swing_lows = find_swings(candles, 3)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None
    closes = [c["close"] for c in candles]
    last_close = closes[-1]

    bos_index = None
    if bullish:
        last_sh = swing_highs[-1]
        if last_close > last_sh["price"]:
            bos_index = last_sh["index"]
    else:
        last_sl = swing_lows[-1]
        if last_close < last_sl["price"]:
            bos_index = last_sl["index"]
    if bos_index is None:
        return None

    order_block = None
    for i in range(bos_index, -1, -1):
        c = candles[i]
        is_opposite = c["close"] < c["open"] if bullish else c["close"] > c["open"]
        if is_opposite:
            order_block = c
            break
    return {"order_block": order_block} if order_block else None


def compute_signal(candles, htf4h, htfd, cascade4h, cascade1h, cascade30m, cascade15m):
    if not candles or len(candles) < 70:
        return {"direction": "WAIT", "confidence": 0}

    closes = [c["close"] for c in candles]
    last_close = closes[-1]
    e60 = ema(closes, 60)[-1]

    using_htf = htf4h["bullish"] is not None
    bullish = htf4h["bullish"] if using_htf else (last_close > e60)

    score = 0
    breakdown = []

    recent14 = candles[-14:]
    atr_early = sum(c["high"] - c["low"] for c in recent14) / len(recent14) if recent14 else last_close * 0.001
    zone_tolerance = atr_early * 0.6

    # 1) EMA60 HTF bias — 30 pts
    score += 30
    breakdown.append("EMA60 HTF bias: " + ("bullish" if bullish else "bearish"))

    swing_highs, swing_lows = find_swings(candles, 3)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"direction": "WAIT", "confidence": 30}

    # 2) POI — 20 pts
    want_type = "RBS" if bullish else "SBR"
    poi = None
    daily_candidates = [z for z in htfd["zones"] if z["type"] == want_type]
    h4_candidates = [z for z in htf4h["zones"] if z["type"] == want_type]
    daily_zone = min(daily_candidates, key=lambda z: abs(last_close - z["price"]), default=None)
    h4_zone = min(h4_candidates, key=lambda z: abs(last_close - z["price"]), default=None)

    if daily_zone and abs(last_close - daily_zone["price"]) < zone_tolerance * 3:
        poi = {"tf": "Daily", "price": daily_zone["price"]}
    elif h4_zone and abs(last_close - h4_zone["price"]) < zone_tolerance * 3:
        poi = {"tf": "4H", "price": h4_zone["price"]}

    if poi:
        score += 20
        breakdown.append(f"POI: {poi['tf']} {want_type} zone @ {fmt(poi['price'])}")
    else:
        breakdown.append("POI: none in range")
        return {"direction": "WAIT", "confidence": score}

    # 3) Rejection candle — 25 pts
    confirm_tf = "4H" if poi["tf"] == "Daily" else "1H"
    source_candles = cascade4h if poi["tf"] == "Daily" else cascade1h
    rejection = find_rejection_at_zone(source_candles, poi["price"], zone_tolerance, bullish)
    rejection_hit = rejection is not None
    if rejection_hit:
        score += 25
        breakdown.append(f"Rejection confirmed on {confirm_tf}")
    else:
        breakdown.append(f"Rejection NOT yet confirmed on {confirm_tf}")
        return {"direction": "WAIT", "confidence": score, "poi": poi, "confirm_tf": confirm_tf}

    # 4) Structure shift + order block — 25 pts
    entry_tf = "30M" if poi["tf"] == "Daily" else "15M"
    entry_candles = cascade30m if poi["tf"] == "Daily" else cascade15m
    mss_result = find_mss_and_order_block(entry_candles, bullish)
    mss_hit = mss_result is not None
    if mss_hit:
        score += 25
        breakdown.append(f"MSS + order block confirmed on {entry_tf}")
    else:
        breakdown.append(f"MSS NOT yet confirmed on {entry_tf}")

    direction = "WAIT"
    if score >= 90:
        direction = "BUY" if bullish else "SELL"

    trade_type = f"Limit Entry ({entry_tf} order block)" if mss_hit else f"Awaiting {confirm_tf} rejection / {entry_tf} shift"

    atr = sum(c["high"] - c["low"] for c in recent14) / len(recent14) if recent14 else 2
    if mss_hit:
        ob = mss_result["order_block"]
        entry = max(ob["open"], ob["close"]) if bullish else min(ob["open"], ob["close"])
        sl = ob["low"] - atr * 0.3 if bullish else ob["high"] + atr * 0.3
    else:
        entry = last_close
        sl = entry - atr * 1.5 if bullish else entry + atr * 1.5

    risk_dist = abs(entry - sl) or atr

    target_type = "SBR" if bullish else "RBS"
    zone_targets = [z["price"] for z in (htf4h["zones"] + htfd["zones"]) if z["type"] == target_type]
    liquidity_targets = (htf4h["unbroken_highs"] + htfd["unbroken_highs"]) if bullish else (htf4h["unbroken_lows"] + htfd["unbroken_lows"])
    all_targets = sorted(
        set(p for p in (zone_targets + liquidity_targets) if (p > entry if bullish else p < entry)),
        reverse=not bullish,
    )
    fixed_1_10 = entry + risk_dist * 10 if bullish else entry - risk_dist * 10
    tp1 = all_targets[0] if all_targets else fixed_1_10
    tp2 = fixed_1_10
    rr = abs(tp1 - entry) / risk_dist

    return {
        "direction": direction,
        "confidence": score,
        "bias": "Bullish" if bullish else "Bearish",
        "poi": poi,
        "confirm_tf": confirm_tf,
        "rejection_hit": rejection_hit,
        "mss_hit": mss_hit,
        "entry_tf": entry_tf,
        "trade_type": trade_type,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "used_key_level_tp": len(all_targets) > 0,
        "rr": rr,
    }


def build_alert_message(pair_label, s):
    tp_note = "next POI/liquidity level" if s["used_key_level_tp"] else "1:10 R:R — no HTF level found ahead yet"
    return (
        f"Murento AI Trader\u2122 Alert\n"
        f"{pair_label} \u2014 {s['confidence']}% confidence ({s['direction']})\n"
        f"Bias: {s['bias']}\n"
        f"POI Type: {s['poi']['tf']} zone @ {fmt(s['poi']['price'])}\n"
        f"Rejection: Confirmed on {s['confirm_tf']}\n"
        f"MSS: {'Confirmed on ' + s['entry_tf'] + ' — order block entry' if s['mss_hit'] else 'Not yet confirmed'}\n"
        f"Trade Type: {'BUY' if s['bias']=='Bullish' else 'SELL'} LIMIT\n"
        f"Entry: {fmt(s['entry'])}\n"
        f"Stop Loss: {fmt(s['sl'])}\n"
        f"TP1: {fmt(s['tp1'])} ({tp_note})  TP2: {fmt(s['tp2'])} (1:10 R:R)\n"
        f"R:R  1:{s['rr']:.2f}"
    )


def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=20)
        data = resp.json()
        if data.get("ok"):
            print("  [telegram] alert sent")
        else:
            print(f"  [telegram] error: {data.get('description')}")
    except Exception as e:
        print(f"  [telegram] send failed: {e}")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def rate_limited_fetch(symbol, tf_key, last_call_time):
    elapsed = time.time() - last_call_time[0]
    if elapsed < MIN_SECONDS_BETWEEN_CALLS:
        time.sleep(MIN_SECONDS_BETWEEN_CALLS - elapsed)
    result = fetch_candles(symbol, tf_key)
    last_call_time[0] = time.time()
    return result


def main():
    state = load_state()
    last_call_time = [0.0]

    for pair in PAIRS:
        symbol, label = pair["symbol"], pair["label"]
        print(f"Scanning {label}...")

        daily = rate_limited_fetch(symbol, "daily", last_call_time)
        h4 = rate_limited_fetch(symbol, "H4", last_call_time)
        h1 = rate_limited_fetch(symbol, "60min", last_call_time)
        m30 = rate_limited_fetch(symbol, "30min", last_call_time)
        m15 = rate_limited_fetch(symbol, "15min", last_call_time)

        if not daily or not h4 or len(daily) < 65 or len(h4) < 65:
            print(f"  skipping {label} — insufficient data")
            continue

        htf4h = compute_htf_structure(h4)
        htfd = compute_htf_structure(daily)

        s = compute_signal(h4, htf4h, htfd, h4, h1 or [], m30 or [], m15 or [])
        confidence = s.get("confidence", 0)
        print(f"  {label}: {confidence}% confidence")

        if confidence < 80:
            state.pop(symbol, None)
            continue

        setup_key = f"{s['bias']}-{fmt(s['entry'])}-{fmt(s['sl'])}"
        if state.get(symbol) == setup_key:
            print(f"  {label}: already alerted this setup, skipping")
            continue

        state[symbol] = setup_key
        message = build_alert_message(label, s)
        print(message)
        send_telegram_alert(message)

    save_state(state)


if __name__ == "__main__":
    main()
