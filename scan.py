#!/usr/bin/env python3
"""Wekelijkse scan: loopt S&P 500, AEX en AMX langs op de regels uit portfolio.json
en schrijft scan.json (lijst met kandidaten + logboek van wat erbij kwam of eraf ging)."""
import io, json, os, time, datetime as dt
import pandas as pd, requests, yfinance as yf

P = json.load(open("portfolio.json"))
G = P.get("grenzen", {})
HELD = set(P["posities"]) | set(P.get("watchlist", {}))
DUBBEL = {"GOOG": "GOOGL", "GOOGL": "GOOG", "FOX": "FOXA", "FOXA": "FOX", "NWS": "NWSA", "NWSA": "NWS", "UA": "UAA", "UAA": "UA", "BF-A": "BF-B", "BF-B": "BF-A"}   # twee aandelensoorten van hetzelfde bedrijf
HELD |= {DUBBEL[x] for x in HELD if x in DUBBEL}
UA = {"User-Agent": "Mozilla/5.0 (portefeuille-cockpit wekelijkse scan)"}
SKIP_IND = ("airline", "drug manufacturer", "biotechnology", "pharmac", "auto manufacturer")
MAXN = 10                                            # zoveel bedrijven nemen we volledig mee in de dagelijkse data
EPSMAX = .40                                                 # boven 60% per jaar is meestal een basiseffect (herstel vanaf bijna geen winst)
RULE = dict(eps=.15, roic=.20, peg=1.5, nd=1.0, rmin=.15)   # toevoegen
STAY = dict(eps=.10, roic=.15, peg=2.0, nd=1.5, rmin=.10)             # blijven staan (marge, tegen heen-en-weer)
MINCAP = 2e9                                                  # in dollars: ook middelgrote bedrijven, daar zitten de verborgen parels
FXUSD = {"USD": 1, "EUR": 1.1, "GBP": 1.3, "SEK": .095, "DKK": .147, "NOK": .092, "CHF": 1.12, "CAD": .73}
SKIP_SEC = ("Financial Services", "Real Estate")             # ROIC en schuld/EBITDA zeggen daar weinig
SECMAP = {"Technology": "Technologie", "Financial Services": "Financieel", "Industrials": "Industrie", "Consumer Cyclical": "Consument",
          "Consumer Defensive": "Consument", "Healthcare": "Zorg", "Communication Services": "Communicatie", "Energy": "Energie",
          "Basic Materials": "Materialen", "Utilities": "Nutsbedrijven", "Real Estate": "Vastgoed"}
LAND = {"United States": "VS", "Netherlands": "NL", "Canada": "CA", "Sweden": "SE", "United Kingdom": "VK", "Germany": "DE", "France": "FR",
        "Italy": "IT", "Ireland": "IE", "Switzerland": "CH", "Denmark": "DK", "Spain": "ES", "Belgium": "BE", "Taiwan": "TW", "Japan": "JP",
        "Brazil": "BR", "Argentina": "AR", "Israel": "IL", "China": "CN", "Singapore": "SG", "Bermuda": "BM", "Luxembourg": "LU"}
today = dt.date.today()
MND = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
dnl = f"{today.day} {MND[today.month - 1]}"


def row(df, *keys):
    for k in keys:
        if df is not None and not df.empty and k in df.index:
            return df.loc[k].dropna().sort_index()
    return pd.Series(dtype=float)


def cagr(s):
    s = s.dropna()
    if len(s) >= 2 and s.iloc[0] <= 0:
        p = s[s > 0]
        if len(p) >= 2: s = s.loc[p.index[0]:]
    if len(s) < 2 or s.iloc[0] <= 0 or s.iloc[-1] <= 0: return None
    return float((s.iloc[-1] / s.iloc[0]) ** (1 / (len(s) - 1)) - 1)


def clean(o):
    if isinstance(o, float): return None if o != o or o in (float("inf"), float("-inf")) else o
    if isinstance(o, dict): return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list): return [clean(v) for v in o]
    return o


def wiki(url, kolommen, suffix=""):
    r = requests.get(url, headers=UA, timeout=40); r.raise_for_status()
    uit = []
    try: tabs = pd.read_html(io.StringIO(r.text))
    except Exception: tabs = []
    try: tabs += pd.read_html(io.StringIO(r.text), match="Ticker")
    except Exception: pass
    for df in tabs:
        if len(df) >= 10: print("tabel", len(df), [str(c)[:20] for c in df.columns][:6])
        for c in df.columns:
            cn = " ".join(map(str, c)).strip().lower() if isinstance(c, tuple) else str(c).strip().lower()
            if cn in kolommen or any(k in cn for k in kolommen):
                uit = [str(x).strip() for x in df[c].dropna()]
                break
        if len(uit) >= 10: break
    res = []
    for x in uit:
        x = x.split(":")[-1].strip().replace(".", "-") if not suffix else x.split(":")[-1].strip()
        x = "".join(ch for ch in x if ch.isalnum() or ch in "-.")
        if x and not x.endswith(suffix): x += suffix
        if x and x not in res: res.append(x)
    return res


def universe():
    U, per = {}, {}
    bronnen = [("S&P 500", "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", ("symbol", "ticker"), ""),
               ("S&P 400", "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", ("symbol", "ticker"), ""),
               ("AEX", "https://en.wikipedia.org/wiki/AEX_index", ("ticker symbol", "ticker", "symbol"), ".AS"),
               ("AMX", "https://en.wikipedia.org/wiki/AMX_index", ("ticker symbol", "ticker", "symbol"), ".AS")]
    for naam, url, kol, suf in bronnen:
        try:
            l = wiki(url, kol, suf)
            per[naam] = len(l)
            for x in l: U.setdefault(x, naam)
        except Exception as e:
            print("FOUT universum", naam, e); per[naam] = 0
    # Europese beurzen via de Yahoo-screener (alles boven MINCAP): Stockholm, Parijs, Frankfurt, Londen, Milaan, Madrid, Kopenhagen, Helsinki, Oslo, Zurich, Brussel
    for code, naam in (("STO", "Stockholm"), ("PAR", "Parijs"), ("GER", "Frankfurt"), ("LSE", "Londen"), ("MIL", "Milaan"), ("MCE", "Madrid"),
                       ("CPH", "Kopenhagen"), ("HEL", "Helsinki"), ("OSL", "Oslo"), ("EBS", "Zurich"), ("BRU", "Brussel")):
        try:
            q = yf.EquityQuery("and", [yf.EquityQuery("eq", ["exchange", code]), yf.EquityQuery("gt", ["intradaymarketcap", MINCAP])])
            l, off = [], 0
            while off < 750:
                r = yf.screen(q, offset=off, size=250, sortField="intradaymarketcap", sortAsc=False)
                qs = (r or {}).get("quotes") or []
                l += [x["symbol"] for x in qs if x.get("symbol")]
                if len(qs) < 250: break
                off += 250
            per[naam] = len(l)
            for x in l: U.setdefault(x, naam)
        except Exception as e:
            print("FOUT screener", naam, e); per[naam] = 0
    return U, per


def metrics(tk):
    t = yf.Ticker(tk); info = t.info or {}
    mc = info.get("marketCap"); sec = info.get("sector")
    ind0 = (info.get("industry") or "").lower()
    if any(w in ind0 for w in SKIP_IND): return "skip"   # geen airlines, farma of autofabrikanten
    cu0 = info.get("financialCurrency") or info.get("currency")
    if cu0 == "GBp": cu0 = "GBP"
    if not mc or mc * FXUSD.get(cu0, 1) < MINCAP or not sec or sec in SKIP_SEC: return "skip"
    roe = info.get("returnOnEquity")
    if roe is not None and roe == roe and roe < .08: return "skip"
    cur, fcur = info.get("currency"), info.get("financialCurrency")
    if fcur and cur and fcur != cur and not (cur == "GBp" and fcur == "GBP"): return "skip"
    inc = t.income_stmt
    eps = row(inc, "Diluted EPS", "Basic EPS")
    if len(eps) < 3: return "skip"
    g = cagr(eps)
    pe = info.get("trailingPE")
    if g is None or not pe or pe != pe or pe <= 0: return "skip"
    bs = t.balance_sheet
    ebit = row(inc, "EBIT", "Operating Income")
    tax = row(inc, "Tax Rate For Calcs").reindex(ebit.index).fillna(.2)
    ic = row(bs, "Invested Capital")
    roic_s = ((ebit * (1 - tax)) / ic).dropna()
    roic = float(roic_s.iloc[-1]) if len(roic_s) else None
    rmin = float(roic_s.iloc[-3:].min()) if len(roic_s) >= 2 else None   # stabiel: ook de jaren ervoor hoog
    nd, eb = row(bs, "Net Debt"), row(inc, "EBITDA")
    ndv = float(nd.iloc[-1] / eb.iloc[-1]) if len(nd) and len(eb) and eb.iloc[-1] > 0 else None
    ind = info.get("industry") or ""
    sector = "Halfgeleiders" if "Semiconductor" in ind else SECMAP.get(sec, sec)
    return dict(tk=tk, naam=info.get("shortName") or info.get("longName") or tk, sector=sector, land=LAND.get(info.get("country"), info.get("country")),
                munt="GBP" if cur == "GBp" else cur, eps=g, roic=roic, rmin=rmin, peg=pe / (g * 100) if g > 0 else None, nd=ndv, pe=pe)


def ok(m, R):
    return (m["eps"] is not None and R["eps"] <= m["eps"] <= EPSMAX and m["roic"] is not None and m["roic"] >= R["roic"] and m.get("rmin") is not None and m["rmin"] >= R["rmin"]
            and m["peg"] is not None and 0 < m["peg"] < R["peg"] and m["nd"] is not None and m["nd"] <= R["nd"])


def waarden(m):
    return f"winstgroei {m['eps']*100:.0f}%, ROIC {m['roic']*100:.0f}%, PEG {m['peg']:.1f}".replace(".", ",")


def rang(m):
    return min(m["roic"], .4) / .15 + min(m["eps"], .4) / .12 + (2 - m["peg"]) + min(max(-m["nd"], 0), 1) - max(0, m["nd"] - .5) * .5   # netto cash telt mee, schuld kost punten


if __name__ == "__main__":
    OLD = json.load(open("scan.json")) if os.path.exists("scan.json") else None
    U, per = universe()
    cand, gezien = [], set()
    for x in U:
        if x in HELD or DUBBEL.get(x) in gezien: continue
        cand.append(x); gezien.add(x)
    print("universum", len(U), per, "te scannen", len(cand))
    res, gescand, fout = {}, 0, 0
    for i, tk in enumerate(cand):
        try:
            m = metrics(tk)
            if m != "skip": res[tk] = m
            gescand += 1
        except Exception as e:
            fout += 1
            if "Too Many" in str(e) or "429" in str(e): time.sleep(20)
        time.sleep(.2)
        if i % 50 == 0: print(i, len(res), fout, flush=True)
    # bewaakt: als Yahoo of Wikipedia grotendeels faalt, de oude lijst niet overschrijven
    if len(cand) < 200 or gescand < .7 * len(cand):
        print("TE WEINIG DATA, scan.json blijft ongewijzigd", len(cand), gescand); raise SystemExit(1)
    voldoet = sorted([m for m in res.values() if ok(m, RULE)], key=rang, reverse=True)
    oud = {x["tk"]: x for x in (OLD or {}).get("lijst", [])}
    fails = dict((OLD or {}).get("fails", {}))
    log = list((OLD or {}).get("log", []))
    nieuw, log_nw = {}, []
    for tk in [t for t in oud if t in HELD]:           # staat inmiddels al in je portefeuille of lijst (bijv. andere aandelensoort)
        x = oud.pop(tk); fails.pop(tk, None)
        log_nw.append(dict(t="rm", n=x["naam"], tk=tk, d=dnl, iso=today.isoformat(), m="Zit al in je portefeuille of lijst, onder een andere aandelensoort."))
    for tk, x in oud.items():                          # bestaande leden: blijven zolang ze de ruimere grens halen
        m = res.get(tk)
        if m is None:
            nieuw[tk] = x; continue                    # niet kunnen ophalen: laten staan
        if ok(m, STAY):
            fails.pop(tk, None); nieuw[tk] = dict(x, **{k: m[k] for k in ("eps", "roic", "peg", "nd", "pe")})
        else:
            fails[tk] = fails.get(tk, 0) + 1
            if fails[tk] >= G.get("sl_wk", 2):
                log_nw.append(dict(t="rm", n=x["naam"], tk=tk, d=dnl, iso=today.isoformat(), m=f"Viel {fails[tk]} weken achter elkaar onder je grenzen: {waarden(m)}."))
                fails.pop(tk, None)
            else: nieuw[tk] = dict(x, **{k: m[k] for k in ("eps", "roic", "peg", "nd", "pe")})
    for m in voldoet:                                  # nieuwe kandidaten, beste eerst
        if len(nieuw) >= MAXN: break
        if m["tk"] in nieuw or m["tk"] in oud: continue
        nieuw[m["tk"]] = dict(m, sinds=today.isoformat(), bron=U.get(m["tk"]))
        if OLD: log_nw.append(dict(t="add", n=m["naam"], tk=m["tk"], d=dnl, iso=today.isoformat(), m=f"Voldoet aan al je regels: {waarden(m)}."))
    if not OLD:
        log_nw.append(dict(t="info", n="Eerste scan", tk=None, d=dnl, iso=today.isoformat(), m=f"{len(nieuw)} bedrijven voldoen aan je regels en staan op de lijst."))
    log = (log_nw + log)[:80]
    out = dict(datum=today.isoformat(), universum=len(U), per_index=per, gescand=gescand, voldoen=len(voldoet), max=MAXN,
               regels=dict(eps=RULE["eps"], roic=RULE["roic"], peg=RULE["peg"], nd=RULE["nd"], rmin=RULE["rmin"], stay=STAY), lijst=list(nieuw.values()), fails=fails, log=log)
    json.dump(clean(out), open("scan.json", "w"), ensure_ascii=False, indent=1)
    print("klaar", len(nieuw), "lijst,", len(log_nw), "wijzigingen")
    topic = os.environ.get("NTFY_TOPIC")
    if topic and OLD:
        for e in log_nw:
            if e["t"] in ("add", "rm"):
                requests.post(f"https://ntfy.sh/{topic}", data=e["m"].encode("utf-8"),
                              headers={"Title": ("Scan: " + ("nieuwe kandidaat " if e["t"] == "add" else "valt af ") + e["n"]).encode("ascii", "ignore").decode(),
                                       "Click": "https://scottvenneker-prog.github.io/Pf-k8x2/"})
