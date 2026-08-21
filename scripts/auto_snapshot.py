import json
import os
import time
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


def get_price(ticker_symbol, retries=3):
    if ticker_symbol in price_cache:
        return price_cache[ticker_symbol]
    price = None
    for attempt in range(retries):
        try:
            hist = yf.Ticker(ticker_symbol).history(period="5d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
                break
        except Exception as exc:  # noqa: BLE001
            print(f"price fetch failed for {ticker_symbol} (attempt {attempt + 1}/{retries}): {exc}")
        if price is None and attempt < retries - 1:
            time.sleep(60)
    price_cache[ticker_symbol] = price
    return price


def get_symbol_currency(transactions, sym, overrides=None):
    """Mirror the frontend's getSymbolCurrency: manual override first, then most recent transaction."""
    if overrides and overrides.get(sym):
        return overrides[sym]
    latest = None
    for t in transactions:
        if t.get("symbol") == sym and (latest is None or t.get("date", "") >= latest.get("date", "")):
            latest = t
    return "USD" if latest and latest.get("currency") == "USD" else "THB"


def compute_cash_balance(transactions, cash_flows):
    """Mirror the frontend's computeCashBalanceAsOf (as of today, since this script always
    values the portfolio as of 'now'). Dividends are intentionally excluded — they're already
    treated as leaving the tracked value immediately for TWRR purposes, so including them here
    too would double-count."""
    balance = 0.0
    for c in cash_flows:
        balance += c["amount"] if c["type"] == "deposit" else -c["amount"]
    for t in transactions:
        balance += (-1 if t["type"] == "buy" else 1) * t["qty"] * t["price"]
    return balance


updated_portfolios = []
for portfolio in data["portfolios"]:
    transactions = portfolio.get("transactions", [])
    cash_flows = portfolio.get("cashFlows", [])
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
    current_prices_foreign = dict(portfolio.get("currentPricesForeign", {}))
    fx_rate = None
    total_value = 0.0
    for sym, h in holdings.items():
        if h["qty"] <= 0.0001:
            continue
        fallback_price = h["cost"] / h["qty"]
        if get_symbol_currency(transactions, sym, portfolio.get("currencyOverrides")) == "USD":
            if fx_rate is None:
                fx_rate = get_price("THB=X")
            price_usd = get_price(sym)
            if price_usd is not None and fx_rate:
                price = price_usd * fx_rate
                current_prices_foreign[sym] = {"priceUSD": round(price_usd, 2), "fxRate": round(fx_rate, 4)}
            else:
                price = fallback_price
        else:
            price = get_price(sym + ".BK") or fallback_price
        current_prices[sym] = round(price, 2)
        total_value += h["qty"] * price

    total_value += compute_cash_balance(transactions, cash_flows)

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
    if fx_rate is None:
        fx_rate = get_price("THB=X")  # ensure we always have it for the snapshot's USD view, even if no USD holdings triggered a fetch above
    if fx_rate is None:
        # Last resort: Yahoo Finance failed even after retries — carry forward the most recent
        # snapshot's fxRateUsd instead of silently leaving the USD view blank for this run.
        prior_with_fx = [s for s in portfolio.get("snapshots", []) if s.get("fxRateUsd")]
        if prior_with_fx:
            fx_rate = sorted(prior_with_fx, key=lambda s: s["date"])[-1]["fxRateUsd"]
            print(f"[{portfolio.get('name')}] THB=X fetch failed after retries; carrying forward previous fxRateUsd={fx_rate}")
    new_snapshot = {
        "id": f"snap{next_snap_id}",
        "date": today,
        "value": round(total_value, 2),
        "benchValues": bench_values,
        "source": "auto",
        "createdAt": datetime.now(ICT).isoformat(),
    }
    if fx_rate:
        new_snapshot["fxRateUsd"] = round(fx_rate, 4)
    snapshots.append(new_snapshot)
    counters["snapId"] = next_snap_id

    portfolio["snapshots"] = snapshots
    portfolio["currentPrices"] = current_prices
    portfolio["currentPricesForeign"] = current_prices_foreign
    portfolio["counters"] = counters
    updated_portfolios.append(portfolio)

    print(f"[{portfolio.get('name')}] snapshot added for {today}: value = {total_value:.2f}, benchmarks = {bench_values}")

doc_ref.update({
    "portfolios": updated_portfolios,
    "updatedAt": datetime.now(ICT).isoformat(),
})

print(f"Done. Updated {len(updated_portfolios)} portfolio(s).")
