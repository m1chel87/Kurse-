"""
Holt die Kurse der Wertpapiere und schreibt sie nach data/quotes.json.
Läuft alle 30 Minuten per GitHub Actions (.github/workflows/kurse.yml).

Quellen: 1) Yahoo Finance (per Symbol), 2) onvista (per ISIN) als Fallback.
Wertpapiere hinzufügen/entfernen: einfach die Liste ASSETS anpassen.
"""
import json, os, time, datetime as dt
import requests

ASSETS = [
    {"isin": "NL0010273215", "name": "ASML",                        "yahoo": ["ASML.AS"]},
    {"isin": "FR0000131104", "name": "BNP Paribas",                 "yahoo": ["BNP.PA"]},
    {"isin": "CA21751T1030", "name": "Copper One Resources",        "yahoo": ["CEXY.CN"]},
    {"isin": "US84615Q1031", "name": "SpaceX",                      "yahoo": ["SPCX"]},
    {"isin": "CA8468111072", "name": "Spartan Metals",              "yahoo": ["W.V"]},
    {"isin": "US5949728530", "name": "Strategy STRC (Vorzugsaktie)", "yahoo": ["STRC"]},
    {"isin": "LI1100044299", "name": "Incrementum Crypto Gold Fund", "yahoo": []},
    {"isin": "IE00B3WJKG14", "name": "iShares S&P 500 IT (ETF)",    "yahoo": ["QDVE.DE"]},
    {"isin": "CH1528107811", "name": "21Shares Strategy Yield ETP", "yahoo": ["STRC.AS"]},
]

OUT = "data/quotes.json"
HIST_DAYS = 7
UA = {"User-Agent": "Mozilla/5.0 (kurs-dashboard)", "Accept": "application/json"}


def log(*a):
    print(*a, flush=True)


# ---------- Wechselkurse (1 EUR = x Fremdwährung) ----------
def get_fx():
    for url in ("https://api.frankfurter.dev/v1/latest?base=EUR",
                "https://api.frankfurter.app/latest?from=EUR"):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            rates = r.json()["rates"]
            rates["EUR"] = 1.0
            return rates
        except Exception as e:
            log("FX-Fehler", url, e)
    return None


# ---------- Yahoo ----------
def from_yahoo(symbol):
    import yfinance as yf
    t = yf.Ticker(symbol)
    fi = t.fast_info
    price = fi.last_price
    if not price or price != price:  # None / NaN
        raise ValueError("kein Kurs")
    prev = fi.previous_close
    cur = (fi.currency or "").upper()
    seed = []
    try:
        h = t.history(period="7d", interval="1h")
        seed = [[int(ts.timestamp()), round(float(v), 6)] for ts, v in h["Close"].dropna().items()]
    except Exception as e:
        log("  Yahoo-Historie fehlt", symbol, e)
    return {"price": float(price), "prev": float(prev) if prev else None,
            "currency": cur, "time": int(time.time()), "source": f"Yahoo {symbol}", "seed": seed}


# ---------- onvista (per ISIN) ----------
TYPE_PATH = {"STOCK": "stocks", "FUND": "funds", "ETF": "etfs",
             "DERIVATIVE": "derivatives", "BOND": "bonds", "INDEX": "indices"}


def _find_quote(obj):
    """Sucht rekursiv das erste Objekt mit einem numerischen 'last'."""
    if isinstance(obj, dict):
        if isinstance(obj.get("last"), (int, float)):
            return obj
        for k in ("quote", "quoteList"):
            if k in obj:
                q = _find_quote(obj[k])
                if q:
                    return q
        for v in obj.values():
            q = _find_quote(v)
            if q:
                return q
    elif isinstance(obj, list):
        for v in obj:
            q = _find_quote(v)
            if q:
                return q
    return None


def from_onvista(isin):
    r = requests.get("https://api.onvista.de/api/v1/instruments/query",
                     params={"searchValue": isin}, headers=UA, timeout=20)
    r.raise_for_status()
    lst = r.json().get("list") or []
    ent = next((e for e in lst if e.get("isin") == isin), lst[0] if lst else None)
    if not ent:
        raise ValueError("ISIN nicht gefunden")
    etype = (ent.get("entityType") or "").upper()
    paths = [TYPE_PATH.get(etype)] if etype in TYPE_PATH else []
    paths += [p for p in ("stocks", "funds", "etfs", "derivatives") if p not in paths]
    last_err = None
    for p in paths:
        try:
            s = requests.get(f"https://api.onvista.de/api/v1/{p}/ISIN:{isin}/snapshot",
                             headers=UA, timeout=20)
            s.raise_for_status()
            q = _find_quote(s.json())
            if q:
                ts = q.get("datetimeLast") or q.get("datetimePrice")
                try:
                    ts = int(dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                except Exception:
                    ts = int(time.time())
                prev = q.get("previousLast")
                return {"price": float(q["last"]),
                        "prev": float(prev) if isinstance(prev, (int, float)) else None,
                        "currency": (q.get("isoCurrency") or "EUR").upper(),
                        "time": ts, "source": "onvista " + ((q.get("market") or {}).get("name") or ""),
                        "seed": [], "name": ent.get("name")}
        except Exception as e:
            last_err = e
    raise ValueError(f"kein Kurs ({last_err})")


def fetch(asset):
    for sym in asset.get("yahoo", []):
        try:
            return from_yahoo(sym)
        except Exception as e:
            log("  Yahoo fehlgeschlagen", sym, e)
    return from_onvista(asset["isin"])


def main():
    old = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            old = {}
    old_items = {i["isin"]: i for i in old.get("items", [])}

    fx = get_fx() or old.get("fx")
    now = int(time.time())
    cutoff = now - HIST_DAYS * 86400
    items, changed = [], False

    for a in ASSETS:
        log("→", a["isin"], a["name"])
        prev_item = old_items.get(a["isin"], {})
        try:
            q = fetch(a)
        except Exception as e:
            log("  FEHLER:", e)
            if prev_item:
                prev_item["stale"] = True
                items.append(prev_item)
            else:
                items.append({"isin": a["isin"], "name": a["name"], "error": str(e)})
            continue

        hist = [p for p in prev_item.get("history", []) if p[0] >= cutoff]
        if len(hist) < 10 and q["seed"]:
            hist = [p for p in q["seed"] if p[0] >= cutoff]
        if not hist or hist[-1][1] != round(q["price"], 6):
            hist.append([now, round(q["price"], 6)])
        if prev_item.get("price") != q["price"]:
            changed = True

        items.append({"isin": a["isin"], "name": a["name"], "price": q["price"],
                      "prev": q["prev"], "currency": q["currency"], "time": q["time"],
                      "source": q["source"].strip(), "history": hist})
        log(f"  {q['price']} {q['currency']} ({q['source']})")

    if not changed and old and set(old_items) == {a["isin"] for a in ASSETS}:
        log("Keine Kursänderung – nichts zu schreiben.")
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"updated": now, "fx": fx, "items": items},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    log("geschrieben:", OUT)


if __name__ == "__main__":
    main()
