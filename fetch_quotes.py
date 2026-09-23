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
CHARTS = "data/charts.json"
HIST_DAYS = 7            # volle Auflösung (30 Min.) für eigene Historie
HIST_KEEP_DAYS = 370     # ältere Punkte: 1 pro Tag
# Chart-Zeiträume: Schlüssel -> (Yahoo-Periode, Intervall, behalten [s], max. Alter vor Neuabruf [s])
CHART_SPECS = {
    "d": ("5d", "5m", 86400, 0),
    "w": ("1mo", "30m", 7 * 86400, 0),
    "m": ("3mo", "1h", 31 * 86400, 6 * 3600),
    "y": ("1y", "1d", None, 6 * 3600),
}
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
def _series(t, period, interval, keep):
    h = t.history(period=period, interval=interval)
    pts = [[int(ts.timestamp()), float(f"{float(v):.6g}")] for ts, v in h["Close"].dropna().items()]
    if keep and pts:
        cut = pts[-1][0] - keep
        pts = [p for p in pts if p[0] >= cut]
    return pts


def from_yahoo(symbol, old_chart=None):
    import yfinance as yf
    t = yf.Ticker(symbol)
    fi = t.fast_info
    price = fi.last_price
    if not price or price != price:  # None / NaN
        raise ValueError("kein Kurs")
    prev = fi.previous_close
    cur = (fi.currency or "").upper()
    now = int(time.time())
    old_chart = old_chart if (old_chart or {}).get("src") == symbol else {}
    fetched = dict(old_chart.get("fetched", {}))
    chart = {"src": symbol, "fetched": fetched}
    for k, (period, interval, keep, max_age) in CHART_SPECS.items():
        if old_chart.get(k) and now - fetched.get(k, 0) < max_age:
            chart[k] = old_chart[k]
            continue
        try:
            chart[k] = _series(t, period, interval, keep)
            fetched[k] = now
        except Exception as e:
            log("  Chart fehlt", symbol, k, e)
            if old_chart.get(k):
                chart[k] = old_chart[k]
    return {"price": float(price), "prev": float(prev) if prev else None,
            "currency": cur, "time": now, "source": f"Yahoo {symbol}",
            "seed": chart.get("w", []), "chart": chart}


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
                        "seed": [], "chart": None, "name": ent.get("name")}
        except Exception as e:
            last_err = e
    raise ValueError(f"kein Kurs ({last_err})")


def fetch(asset, old_chart=None):
    for sym in asset.get("yahoo", []):
        try:
            return from_yahoo(sym, old_chart)
        except Exception as e:
            log("  Yahoo fehlgeschlagen", sym, e)
    return from_onvista(asset["isin"])


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def thin(hist, now):
    """Letzte HIST_DAYS Tage voll, davor 1 Punkt pro Tag, max. HIST_KEEP_DAYS."""
    recent_cut, keep_cut = now - HIST_DAYS * 86400, now - HIST_KEEP_DAYS * 86400
    daily = {}
    for p in hist:
        if keep_cut <= p[0] < recent_cut:
            daily[p[0] // 86400] = p
    return sorted(daily.values()) + [p for p in hist if p[0] >= recent_cut]


def own_chart(hist):
    if not hist:
        return {"src": "own"}
    last = hist[-1][0]
    return {"src": "own",
            "d": [p for p in hist if p[0] >= last - 86400],
            "w": [p for p in hist if p[0] >= last - 7 * 86400],
            "m": [p for p in hist if p[0] >= last - 31 * 86400],
            "y": hist}


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    log("geschrieben:", path)


def main():
    old = load_json(OUT)
    old_items = {i["isin"]: i for i in old.get("items", [])}
    old_charts = load_json(CHARTS).get("assets", {})

    fx = get_fx() or old.get("fx")
    now = int(time.time())
    items, charts, changed = [], {}, False

    for a in ASSETS:
        isin = a["isin"]
        log("→", isin, a["name"])
        prev_item = old_items.get(isin, {})
        try:
            q = fetch(a, old_charts.get(isin))
        except Exception as e:
            log("  FEHLER:", e)
            if prev_item:
                prev_item["stale"] = True
                items.append(prev_item)
            else:
                items.append({"isin": isin, "name": a["name"], "error": str(e)})
            if isin in old_charts:
                charts[isin] = old_charts[isin]
            continue

        hist = list(prev_item.get("history", []))
        if len(hist) < 10 and q["seed"]:
            hist = [p for p in q["seed"] if p[0] >= now - HIST_DAYS * 86400]
        if not hist or hist[-1][1] != round(q["price"], 6):
            hist.append([now, round(q["price"], 6)])
        hist = thin(hist, now)
        if prev_item.get("price") != q["price"]:
            changed = True

        items.append({"isin": isin, "name": a["name"], "price": q["price"],
                      "prev": q["prev"], "currency": q["currency"], "time": q["time"],
                      "source": q["source"].strip(), "history": hist})
        charts[isin] = q["chart"] or own_chart(hist)
        log(f"  {q['price']} {q['currency']} ({q['source']})")

    new_charts = {"assets": charts}
    if new_charts["assets"] != old_charts:
        write_json(CHARTS, new_charts)
    if not changed and old and set(old_items) == {a["isin"] for a in ASSETS}:
        log("Keine Kursänderung – quotes.json unverändert.")
        return
    write_json(OUT, {"updated": now, "fx": fx, "items": items})


if __name__ == "__main__":
    main()
