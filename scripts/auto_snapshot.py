import json
import os
from datetime import datetime, timezone, timedelta

import firebase_admin
import yfinance as yf
from firebase_admin import credentials, firestore

ICT = timezone(timedelta(hours=7))  # Asia/Bangkok, no DST

SERVICE_ACCOUNT_JSON = os.environ["FIREBASE_SERVICE_ACCOUNT"]
UID = os.environ["FIREBASE_UID"]

cred = credentials.Certificate(json.loads(SERVICE_ACCOUNT_JSON))
firebase_admin.initialize_app(cred)
db = firestore.client()

doc_ref = db.collection("portfolios").document(UID)
doc = doc_ref.get()
if not doc.exists:
    raise SystemExit(f"No portfolio document found for uid={UID}. Sign in on the web app at least once first.")

data = doc.to_dict()

# Backward compatibility: older docs (before multi-portfolio support) stored
# transactions/snapshots/etc directly on the document instead of inside a
# "portfolios" array. Wrap it the same way the frontend's migration does.
if "portfolios" not in data or not data["portfolios"]:
    data = {
        "portfolios": [{
            "id": "p1",
            "name": "พอร์ตหลัก",
            "transactions": data.get("transactions", []),
            "dividendEntries": data.get("dividendEntries", []),
            "cashFlows": data.get("cashFlows", []),
            "currentPrices": data.get("currentPrices", {}),
            "snapshots": data.get("snapshots", []),
            "benchmarkComponents": data.get("benchmarkComponents") or [{"id": "b1", "name": "", "weight": 100}],
            "counters": data.get("counters", {}),
        }],
        "activePortfolioId": "p1",
        "portfolioIdCounter": 1,
    }

today = datetime.now(ICT).strftime("%Y-%m-%d")
price_cache = {}  # avoid refetching the same symbol twice across portfolios


def get_price(ticker_symbol):
    if ticker_symbol in price_cache:
        return price_cache[ticker_symbol]
    price = None
    try:
        hist = yf.Ticker(ticker_symbol).history(period="5d")
        if not hist.empty:
            price = float(hist["Close"].iloc[-1])
    except Exception as exc:  # noqa: BLE001
        print(f"price fetch failed for {ticker_symbol}: {exc}")
    price_cache[ticker_symbol] = price
    return price


updated_portfolios = []
for portfolio in data["portfolios"]:
    transactions = portfolio.get("transactions", [])
    benchmark_components = portfolio.get("benchmarkComponents", [])

    # recompute current holdings from the full transaction history
    holdings = {}
    for t in transactions:
        sym = t["symbol"]
        h = holdings.setdefault(sym, {"qty": 0.0, "cost": 0.0})
        if t["type"] == "buy":
            h["cost"] += t["qty"] * t["price"]
            h["qty"] += t["qty"]
        else:
            avg = h["cost"] / h["qty"] if h["qty"] > 0 else 0
            h["cost"] -= avg * t["qty"]
            h["qty"] -= t["qty"]

    current_prices = dict(portfolio.get("currentPrices", {}))
    total_value = 0.0
    for sym, h in holdings.items():
        if h["qty"] <= 0.0001:
            continue
        fallback_price = h["cost"] / h["qty"]
        price = get_price(sym + ".BK") or fallback_price
        current_prices[sym] = round(price, 2)
        total_value += h["qty"] * price

    bench_values = {}
    for b in benchmark_components:
        symbol = b.get("symbol")
        if not symbol:
            continue
        price = get_price(symbol)
        if price is not None:
            bench_values[b["id"]] = round(price, 4)

    snapshots = [s for s in portfolio.get("snapshots", []) if s.get("date") != today]  # avoid duplicate same-day
    counters = dict(portfolio.get("counters", {}))
    next_snap_id = counters.get("snapId", 0) + 1
    snapshots.append({
        "id": f"snap{next_snap_id}",
        "date": today,
        "value": round(total_value, 2),
        "benchValues": bench_values,
    })
    counters["snapId"] = next_snap_id

    portfolio["snapshots"] = snapshots
    portfolio["currentPrices"] = current_prices
    portfolio["counters"] = counters
    updated_portfolios.append(portfolio)

    print(f"[{portfolio.get('name')}] snapshot added for {today}: value = {total_value:.2f}, benchmarks = {bench_values}")

doc_ref.update({
    "portfolios": updated_portfolios,
    "updatedAt": datetime.now(ICT).isoformat(),
})

print(f"Done. Updated {len(updated_portfolios)} portfolio(s).")
