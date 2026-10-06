#!/usr/bin/env python3
"""Parse an IBKR Activity Statement CSV into a structured data.json for the
investment tracker web app. No PII (name/address/account number) is kept."""
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime

IN_PATH = "activity.csv"
OUT_PATH = "data.json"


def scrub(text, account_name):
    """Strip the account holder's name out of free-text description fields
    (IBKR embeds it in some transaction descriptions, e.g. disbursements)."""
    if not text or not account_name:
        return text
    return text.replace(account_name, "account holder").strip()


def load_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.reader(fh))


def main():
    rows = load_rows(IN_PATH)

    # section -> last seen header row (list of column names, 0-indexed same as row)
    headers = {}
    # section -> list of Data rows (each as dict by header name)
    data = defaultdict(list)
    # Trades needs ordering preserved with SubTotal/Total markers, so keep raw too
    trades_raw = []
    fin_instruments = {}  # Description -> dict(Underlying, Expiry, Type, Strike)

    for row in rows:
        if not row:
            continue
        section = row[0]
        kind = row[1] if len(row) > 1 else ""
        if kind == "Header":
            headers[section] = row
            continue
        if section == "Trades":
            trades_raw.append(row)
            continue
        if section == "Financial Instrument Information" and kind == "Data":
            # columns: 0 section,1 Data,2 AssetCategory,3 Symbol,4 Description,5 Conid,
            # 6 Underlying,7 Exch,8 Multiplier,9 Expiry,10 DeliveryMonth,11 Type,12 Strike,13 Code
            if len(row) >= 13:
                desc = row[4]
                fin_instruments[desc] = {
                    "assetCategory": row[2],
                    "underlying": row[6],
                    "expiry": row[9] if len(row) > 9 else None,
                    "type": row[11] if len(row) > 11 else None,
                    "strike": row[12] if len(row) > 12 else None,
                }
            continue
        hdr = headers.get(section)
        if hdr and kind == "Data":
            d = {}
            for i, name in enumerate(hdr):
                d[name] = row[i] if i < len(row) else ""
            data[section].append(d)

    def f(x, default=0.0):
        try:
            return float(x)
        except (ValueError, TypeError):
            return default

    # ---------- Open Positions (current holdings) ----------
    holdings = []
    for d in data.get("Open Positions", []):
        if d.get("DataDiscriminator") != "Summary":
            continue
        holdings.append({
            "symbol": d.get("Symbol"),
            "assetCategory": d.get("Asset Category"),
            "currency": d.get("Currency"),
            "quantity": f(d.get("Quantity")),
            "costPrice": f(d.get("Cost Price")),
            "costBasis": f(d.get("Cost Basis")),
            "closePrice": f(d.get("Close Price")),
            "value": f(d.get("Value")),
            "unrealizedPL": f(d.get("Unrealized P/L")),
        })

    # ---------- Change in NAV (bridge) ----------
    nav_bridge = {}
    for d in data.get("Change in NAV", []):
        nav_bridge[d.get("Field Name")] = f(d.get("Field Value"))

    nav_summary = {}
    for d in data.get("Net Asset Value", []):
        pass  # handled below via raw scan (Net Asset Value has irregular rows)

    # Net Asset Value section has a few different row shapes; re-scan raw rows for it.
    twr = None
    nav_total_current = None
    for row in rows:
        if row and row[0] == "Net Asset Value" and row[1] == "Data":
            if len(row) >= 4 and row[2] == "Total":
                try:
                    nav_total_current = float(row[4])
                except (ValueError, IndexError):
                    pass
            if len(row) == 3 and "%" in row[2]:
                twr = row[2]

    # ---------- Account holder name (used only to scrub PII, never output) ----------
    account_name = None
    for d in data.get("Account Information", []):
        if d.get("Field Name") == "Name":
            account_name = d.get("Field Value")
            break

    # ---------- Dividends ----------
    dividends = []
    for d in data.get("Dividends", []):
        if d.get("Currency") == "Total":
            continue
        dividends.append({
            "date": d.get("Date"),
            "description": scrub(d.get("Description"), account_name),
            "amount": f(d.get("Amount")),
            "currency": d.get("Currency"),
        })

    # ---------- Withholding Tax ----------
    withholding_tax = []
    for d in data.get("Withholding Tax", []):
        if d.get("Date") in (None, "", "Total"):
            continue
        withholding_tax.append({
            "date": d.get("Date"),
            "description": scrub(d.get("Description"), account_name),
            "amount": f(d.get("Amount")),
            "currency": d.get("Currency"),
        })

    # ---------- Deposits & Withdrawals ----------
    cash_flows = []
    for d in data.get("Deposits & Withdrawals", []):
        if d.get("Settle Date") in (None, "", "Total"):
            continue
        cash_flows.append({
            "date": d.get("Settle Date"),
            "description": scrub(d.get("Description"), account_name),
            "amount": f(d.get("Amount")),
            "currency": d.get("Currency"),
        })

    # ---------- Interest ----------
    interest = []
    for d in data.get("Interest", []):
        date = d.get("Date")
        if not date:
            continue
        interest.append({
            "date": date,
            "description": scrub(d.get("Description"), account_name),
            "amount": f(d.get("Amount")),
            "currency": d.get("Currency"),
        })

    # ---------- Fees ----------
    fees = []
    for d in data.get("Fees", []):
        date = d.get("Date")
        if not date:
            continue
        fees.append({
            "date": date,
            "subtitle": d.get("Subtitle"),
            "description": scrub(d.get("Description"), account_name),
            "amount": f(d.get("Amount")),
            "currency": d.get("Currency"),
        })

    # ---------- Trades: split stocks vs options, pair options into round trips ----------
    option_categories = {"Equity and Index Options", "Options On Futures"}
    stock_trades_count = 0
    option_blocks = []  # list of dict: category,currency,symbol -> rows + subtotal
    current_block = None

    for row in trades_raw:
        kind = row[1]
        if kind == "Header":
            continue
        category = row[3] if len(row) > 3 else ""
        currency = row[4] if len(row) > 4 else ""
        symbol = row[5] if len(row) > 5 else ""
        if kind == "Data":
            if category == "Stocks":
                stock_trades_count += 1
                continue
            if category not in option_categories:
                continue  # Forex etc.
            if current_block is None or current_block["symbol"] != symbol:
                current_block = {"category": category, "currency": currency, "symbol": symbol, "rows": []}
                option_blocks.append(current_block)
            current_block["rows"].append(row)
        elif kind == "SubTotal":
            if category not in option_categories:
                current_block = None
                continue
            if current_block and current_block["symbol"] == symbol:
                # row layout: 0 Trades,1 SubTotal,2 '',3 Cat,4 Ccy,5 Symbol,6 '',7 Qty,
                # 8 '',9 '',10 Proceeds,11 Comm,12 Basis,13 RealizedPL,14 MTM_PL,15 Code
                current_block["subtotal"] = {
                    "proceeds": f(row[10] if len(row) > 10 else 0),
                    "commFee": f(row[11] if len(row) > 11 else 0),
                    "realizedPL": f(row[13] if len(row) > 13 else 0),
                }
            current_block = None

    past_options = []
    for blk in option_blocks:
        info = fin_instruments.get(blk["symbol"], {})
        data_rows = blk["rows"]
        if not data_rows:
            continue

        def parse_dt(r):
            raw = r[6]
            try:
                return datetime.strptime(raw.split(",")[0].strip(), "%Y-%m-%d")
            except Exception:
                return None

        dated = sorted(data_rows, key=lambda r: (parse_dt(r) or datetime.min))
        open_rows = [r for r in dated if "O" in r[15].split(";")]
        close_rows = [r for r in dated if "C" in r[15].split(";") and "O" not in r[15].split(";")]
        if not open_rows:
            open_rows = [dated[0]]
        if not close_rows and len(dated) > 1:
            close_rows = [dated[-1]]

        open_date = parse_dt(open_rows[0])
        close_date = parse_dt(close_rows[-1]) if close_rows else None
        open_qty = sum(f(r[7]) for r in open_rows)
        close_qty = sum(f(r[7]) for r in close_rows)
        direction = "Short (sold to open)" if open_qty < 0 else "Long (bought to open)"
        subtotal = blk.get("subtotal", {})
        status = "Closed" if close_rows else "Expired/Assigned (no closing trade)"

        past_options.append({
            "symbol": blk["symbol"],
            "underlying": info.get("underlying"),
            "optionType": info.get("type"),
            "strike": f(info.get("strike"), None) if info.get("strike") else None,
            "expiry": info.get("expiry"),
            "assetCategory": blk["category"],
            "direction": direction,
            "contracts": abs(open_qty),
            "openDate": open_date.strftime("%Y-%m-%d") if open_date else None,
            "closeDate": close_date.strftime("%Y-%m-%d") if close_date else None,
            "daysHeld": (close_date - open_date).days if (open_date and close_date) else None,
            "realizedPL": round(subtotal.get("realizedPL", 0), 2),
            "commissions": round(subtotal.get("commFee", 0), 2),
            "status": status,
        })

    past_options.sort(key=lambda o: o["openDate"] or "")

    options_summary = {
        "totalTrades": len(past_options),
        "totalRealizedPL": round(sum(o["realizedPL"] for o in past_options), 2),
        "totalCommissions": round(sum(o["commissions"] for o in past_options), 2),
        "wins": sum(1 for o in past_options if o["realizedPL"] > 0),
        "losses": sum(1 for o in past_options if o["realizedPL"] < 0),
        "flat": sum(1 for o in past_options if o["realizedPL"] == 0),
    }

    # ---------- Account/meta (no PII) ----------
    statement = {}
    for d in data.get("Statement", []):
        statement[d.get("Field Name")] = d.get("Field Value")

    meta = {
        "baseCurrency": "USD",
        "period": statement.get("Period"),
        "generatedFrom": statement.get("WhenGenerated"),
        "lastImported": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }

    def by_currency(rows):
        agg = defaultdict(float)
        for r in rows:
            agg[r["currency"]] += r["amount"]
        return {k: round(v, 2) for k, v in agg.items()}

    cash_flow_summary = {
        "byCurrency": by_currency(cash_flows),
        "deposits": round(sum(c["amount"] for c in cash_flows if c["amount"] > 0), 2),
        "withdrawals": round(sum(c["amount"] for c in cash_flows if c["amount"] < 0), 2),
    }
    income_summary = {
        "dividendsByCurrency": by_currency(dividends),
        "withholdingTaxByCurrency": by_currency(withholding_tax),
        "interestByCurrency": by_currency(interest),
        "feesByCurrency": by_currency(fees),
    }

    out = {
        "meta": meta,
        "nav": {
            "current": nav_total_current,
            "timeWeightedReturn": twr,
            "bridge": nav_bridge,
        },
        "holdings": holdings,
        "dividends": dividends,
        "withholdingTax": withholding_tax,
        "cashFlows": cash_flows,
        "cashFlowSummary": cash_flow_summary,
        "interest": interest,
        "fees": fees,
        "incomeSummary": income_summary,
        "pastOptions": past_options,
        "optionsSummary": options_summary,
    }

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)

    print(f"Holdings: {len(holdings)}")
    print(f"Dividends: {len(dividends)}")
    print(f"Cash flows: {len(cash_flows)}")
    print(f"Interest rows: {len(interest)}")
    print(f"Fees rows: {len(fees)}")
    print(f"Stock trade rows (not stored, cost-basis already in holdings): {stock_trades_count}")
    print(f"Past options (paired round trips): {len(past_options)}")
    print(f"Options summary: {json.dumps(options_summary, indent=2)}")
    print(f"NAV current: {nav_total_current}, TWR: {twr}")
    print(f"NAV bridge: {json.dumps(nav_bridge, indent=2)}")


if __name__ == "__main__":
    main()
