#!/usr/bin/env python3
"""
Crypto signal bot: BUY / SELL / HOLD signals plus market context for BTC, ETH, XRP
(or any Coinbase pair). SIGNALS ONLY. It never places orders and never needs API keys.

Main signal (daily candles, closed candles only):
  BUY   when the fast SMA crosses ABOVE the slow SMA  (default 20 / 50)
  SELL  when the fast SMA crosses BELOW the slow SMA
  HOLD  otherwise

Context indicators shown with every signal:
  Volume       last day's volume vs its 20-day average; a cross on heavy volume (>=1.5x)
               is marked "confirmed", on light volume "weak"
  RSI(14)      > 70 overbought, < 30 oversold
  Long trend   price above / below the 200-day average
  Fear & Greed crypto sentiment index 0-100 (alternative.me), used as a CONTRARIAN input:
               extreme fear (<= 25) leans favorable, extreme greed (>= 75) leans cautious
  Score        sum of trend, long trend, RSI and Fear & Greed votes (-4 .. +4).
               It is a summary of conditions, not a prediction.

Usage:
  python signal_bot.py                        # latest signal for BTC, ETH, XRP
  python signal_bot.py --pairs BTC-USD SOL-USD
  python signal_bot.py --backtest             # test the SMA rules on ~3 years of history
  python signal_bot.py --watch 3600           # re-check every hour, alert on new crosses
  python signal_bot.py --demo                 # offline test with fake data

Optional Telegram alerts: set env vars TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
No third-party packages needed (Python 3.8+).
Data: Coinbase public market-data API (prices, volume) and alternative.me (Fear & Greed).
"""
import argparse
import json
import os
import random
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.exchange.coinbase.com"
FNG_URL = "https://api.alternative.me/fng/?limit=1"
STATE_FILE = "signal_state.json"


# ---------- data ----------
def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "signal-bot/1.1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def fetch_candles(pair, days):
    """Daily (date, close, volume), oldest first, today's unfinished candle removed."""
    end = datetime.now(timezone.utc)
    rows = {}
    remaining = days
    while remaining > 0:
        span = min(300, remaining)  # API returns at most 300 candles per call
        start = end - timedelta(days=span)
        q = urllib.parse.urlencode({
            "granularity": 86400,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        for t, _low, _high, _open, close, vol in http_json(f"{API}/products/{pair}/candles?{q}"):
            rows[t] = (close, vol)
        end = start
        remaining -= span
        time.sleep(0.4)  # be polite to the API
    out = [(datetime.fromtimestamp(t, timezone.utc).date(), rows[t][0], rows[t][1])
           for t in sorted(rows)]
    if out and out[-1][0] == datetime.now(timezone.utc).date():
        out.pop()  # drop the still-open candle so signals don't flicker intraday
    return out


def demo_candles(pair, days):
    """Fake random-walk prices with trending regimes, for offline testing."""
    rng = random.Random(sum(map(ord, pair)))
    price, drift, out = 100.0, 0.0, []
    day = datetime.now(timezone.utc).date() - timedelta(days=days)
    for i in range(days):
        if i % 60 == 0:
            drift = rng.uniform(-0.006, 0.008)
        ret = drift + rng.gauss(0, 0.025)
        price *= 1 + ret
        out.append((day + timedelta(days=i), price, rng.uniform(800, 1200) * (1 + 15 * abs(ret))))
    return out


def fear_greed(demo=False):
    """(value 0-100, label) or None if unavailable. One crypto-wide number, not per coin."""
    if demo:
        return 22, "Extreme Fear"
    try:
        d = http_json(FNG_URL)["data"][0]
        return int(d["value"]), d["value_classification"]
    except Exception as e:
        print(f"(Fear & Greed unavailable: {e})")
        return None


# ---------- indicators ----------
def sma(values, n):
    out, total = [None] * len(values), 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def rsi(values, n=14):
    out = [None] * len(values)
    if len(values) <= n:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gain += max(d, 0)
        loss += max(-d, 0)
    avg_g, avg_l = gain / n, loss / n
    out[n] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def volume_ratio(volumes, i, n=20):
    """Volume on bar i divided by the average of the n bars before it."""
    if i < n:
        return None
    avg = sum(volumes[i - n:i]) / n
    return volumes[i] / avg if avg else None


def cross_signals(closes, fast, slow):
    f, s = sma(closes, fast), sma(closes, slow)
    sig = [None] * len(closes)
    for i in range(1, len(closes)):
        if None in (f[i], s[i], f[i - 1], s[i - 1]):
            continue
        if f[i - 1] <= s[i - 1] and f[i] > s[i]:
            sig[i] = "BUY"
        elif f[i - 1] >= s[i - 1] and f[i] < s[i]:
            sig[i] = "SELL"
    return f, s, sig


# ---------- reporting ----------
def fmt(x):
    return f"{x:,.2f}" if x >= 1 else f"{x:.4f}"


def status(pair, candles, fast, slow, fng):
    dates = [c[0] for c in candles]
    closes = [c[1] for c in candles]
    vols = [c[2] for c in candles]
    f, s, sig = cross_signals(closes, fast, slow)
    r = rsi(closes)
    long_ma = sma(closes, 200)
    i = len(closes) - 1
    if f[i] is None or s[i] is None:
        raise ValueError(f"need at least {slow + 1} daily candles, got {len(closes)}")

    trend_up = f[i] > s[i]
    action = sig[i] or "HOLD"
    last = next((j for j in range(i, -1, -1) if sig[j]), None)
    vr = volume_ratio(vols, i)

    head = f"  SIGNAL : {action}" + ("" if sig[i] else f" ({'uptrend' if trend_up else 'downtrend'})")
    if sig[i] and vr is not None:
        head += "  [volume confirmed]" if vr >= 1.5 else "  [light volume, weaker signal]"
    lines = [f"{pair}  {dates[i]}  close {fmt(closes[i])}", head,
             f"  SMA{fast} {fmt(f[i])} | SMA{slow} {fmt(s[i])} | RSI {r[i]:.1f}"]
    if last is not None:
        lines.append(f"  Last cross: {sig[last]} on {dates[last]} at {fmt(closes[last])} "
                     f"({(dates[i] - dates[last]).days} days ago)")

    # context votes: +1 favorable, -1 cautious
    votes = [1 if trend_up else -1]
    if vr is not None:
        lines.append(f"  Volume : {vr:.2f}x its 20-day average"
                     + (" (heavy)" if vr >= 1.5 else " (light)" if vr <= 0.7 else ""))
    if long_ma[i] is not None:
        above = closes[i] > long_ma[i]
        votes.append(1 if above else -1)
        lines.append(f"  Long trend: price {'above' if above else 'below'} 200-day average ({fmt(long_ma[i])})")
    votes.append(1 if r[i] < 30 else -1 if r[i] > 70 else 0)
    if fng:
        votes.append(1 if fng[0] <= 25 else -1 if fng[0] >= 75 else 0)
        lines.append(f"  Fear & Greed: {fng[0]} ({fng[1]})")
    score = sum(votes)
    label = "favorable" if score >= 3 else "cautious" if score <= -3 else "mixed"
    lines.append(f"  Score  : {score:+d} of +/-{len(votes)}  ({label} conditions)")
    if r[i] > 70:
        lines.append("  Warning: RSI > 70 (overbought). Consider waiting before adding.")
    elif r[i] < 30:
        lines.append("  Note: RSI < 30 (oversold). Selling into weakness has often been late.")
    return "\n".join(lines), action, dates[i]


def backtest(pair, candles, fast, slow, fee):
    closes = [c[1] for c in candles]
    f, s, _ = cross_signals(closes, fast, slow)
    start = next((i for i, v in enumerate(s) if v is not None), None)
    if start is None or start >= len(closes) - 2:
        raise ValueError("not enough history for a backtest")
    eq = bh = peak_e = peak_b = 1.0
    dd_e = dd_b = 0.0
    pos, trades = False, 0
    for i in range(start, len(closes) - 1):
        state = f[i] > s[i]          # decided at the close of day i ...
        if state != pos:
            eq *= 1 - fee
            trades += 1
            pos = state
        ret = closes[i + 1] / closes[i] - 1   # ... applied to day i+1 (no look-ahead)
        if pos:
            eq *= 1 + ret
        bh *= 1 + ret
        peak_e, peak_b = max(peak_e, eq), max(peak_b, bh)
        dd_e, dd_b = min(dd_e, eq / peak_e - 1), min(dd_b, bh / peak_b - 1)
    days = len(closes) - 1 - start
    return (f"{pair}  backtest {candles[start][0]} to {candles[-1][0]} ({days} days, fee {fee:.2%}/trade)\n"
            f"  Signal strategy : {eq - 1:+.1%}   max drawdown {dd_e:.1%}   trades {trades}\n"
            f"  Buy and hold    : {bh - 1:+.1%}   max drawdown {dd_b:.1%}")


# ---------- alerts ----------
def notify(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=15)
    except Exception as e:  # alerts must never crash the bot
        print(f"  (telegram alert failed: {e})")


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh)


# ---------- main ----------
def run_once(args, state):
    fng = None if args.backtest else fear_greed(args.demo)
    for pair in args.pairs:
        try:
            days = args.days if args.backtest else max(args.slow * 3, 260)
            candles = demo_candles(pair, days) if args.demo else fetch_candles(pair, days)
            if args.backtest:
                print(backtest(pair, candles, args.fast, args.slow, args.fee) + "\n")
                continue
            text, action, date = status(pair, candles, args.fast, args.slow, fng)
            print(text + "\n")
            key = f"{pair}:{action}:{date}"
            if action in ("BUY", "SELL") and state.get(pair) != key:
                notify(f"{action} signal\n{text}")
                state[pair] = key
        except Exception as e:
            print(f"{pair}: error: {e}\n")
    save_state(state)


def main():
    p = argparse.ArgumentParser(description="Crypto BUY/SELL signal bot (signals only)")
    p.add_argument("--pairs", nargs="+", default=["BTC-USD", "ETH-USD", "XRP-USD"])
    p.add_argument("--fast", type=int, default=20, help="fast SMA length")
    p.add_argument("--slow", type=int, default=50, help="slow SMA length")
    p.add_argument("--backtest", action="store_true", help="test the SMA rules on history")
    p.add_argument("--days", type=int, default=1000, help="history length for --backtest")
    p.add_argument("--fee", type=float, default=0.002, help="fee per trade in backtest (0.002 = 0.2%%)")
    p.add_argument("--watch", type=int, metavar="SECONDS", help="repeat every N seconds")
    p.add_argument("--demo", action="store_true", help="use fake data (no internet needed)")
    args = p.parse_args()
    if args.fast >= args.slow:
        p.error("--fast must be smaller than --slow")

    print("Signals only, not financial advice. Past performance does not predict future results.\n")
    state = load_state()
    if args.watch:
        while True:
            print(f"--- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
            run_once(args, state)
            time.sleep(max(args.watch, 60))
    else:
        run_once(args, state)


if __name__ == "__main__":
    main()
