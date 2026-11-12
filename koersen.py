"""Lichte koersupdate: haalt alleen de actuele koersen van je posities en kandidaten op (een paar seconden)
en schrijft ze naar koersen.json. De app legt die bovenop data.json. Bedrijfscijfers blijven van de grote update."""
import json, datetime as dt
import yfinance as yf

D = json.load(open("data.json"))
rows = {r["t"]: r for r in D["rows"]}
fxc = {}

def fx(cur, oud):
    if cur == "EUR": return 1.0
    if cur not in fxc:
        try:
            v = float(yf.Ticker(f"EUR{cur}=X").fast_info["last_price"])
            fxc[cur] = v if v > 0 else oud
        except Exception: fxc[cur] = oud
    return fxc[cur] or oud

q = {}
for t, r in rows.items():
    try:
        fi = yf.Ticker(t).fast_info
        p, prev, valuta = fi["last_price"], fi["previous_close"], fi.get("currency")
        if not p or p <= 0: continue
        pence = valuta == "GBp"
        p = p / 100 if pence else p
        prev = prev / 100 if pence and prev else prev
        cur = r.get("munt") or ("GBP" if pence else valuta) or "EUR"
        e = p / fx(cur, r.get("rate") or 1.0)
        oud = r.get("koers_eur")
        if oud and not (0.6 < e / oud < 1.6):   # onwaarschijnlijke sprong: negeren, de grote update beoordeelt dat
            print("overgeslagen", t, oud, e); continue
        q[t] = dict(e=round(e, 4), d=round(p / prev - 1, 5) if prev and prev > 0 else None)
    except Exception as ex:
        print("FOUT", t, ex)

if len(q) >= len(rows) // 2:
    json.dump(dict(bijgewerkt=dt.datetime.now(dt.timezone.utc).isoformat(), q=q), open("koersen.json", "w"), separators=(",", ":"))
    print("koersen", len(q), "van", len(rows))
else:
    raise SystemExit(f"te weinig koersen ({len(q)}), niets geschreven")
