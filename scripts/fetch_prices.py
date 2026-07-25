import json
from datetime import datetime, timezone, timedelta

import yfinance as yf

ICT = timezone(timedelta(hours=7))  # Asia/Bangkok, no DST

with open("symbols.json", encoding="utf-8") as f:
    cfg = json.load(f)

prices = {}
for sym in cfg.get("stocks", []):
    ticker = sym + ".BK"
    try:
        data = yf.Ticker(ticker).history(period="5d")
        if not data.empty:
            prices[sym] = round(float(data["Close"].iloc[-1]), 2)
    except Exception as exc:  # noqa: BLE001
        print(f"failed to fetch {ticker}: {exc}")

benchmarks = {}
for b in cfg.get("benchmarks", []):
    symbol = b.get("symbol")
    if not symbol:
        continue
    try:
        data = yf.Ticker(symbol).history(period="5d")
        if not data.empty:
            benchmarks[symbol] = round(float(data["Close"].iloc[-1]), 4)
    except Exception as exc:  # noqa: BLE001
        print(f"failed to fetch {symbol}: {exc}")

output = {
    "updatedAt": datetime.now(ICT).isoformat(),
    "prices": prices,
    "benchmarks": benchmarks,
}

with open("prices.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(json.dumps(output, ensure_ascii=False, indent=2))
