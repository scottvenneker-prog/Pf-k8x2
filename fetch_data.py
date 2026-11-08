"""Haalt koersen + fundamentals (Yahoo Finance via yfinance) voor portfolio.json, schrijft data.json, stuurt ntfy-pushes."""
import json, os, time, datetime as dt
import pandas as pd, requests, yfinance as yf

P = json.load(open("portfolio.json"))
H, WLD = P["posities"], P.get("watchlist", {})   # H: ticker -> {naam, sector, land, weging, rend, ...}; alleen percentages
WL = list(WLD)
META = {**WLD, **H}
FALL = {"USD": 1.1143, "GBP": 0.8344, "SEK": 11.025, "CAD": 1.5743}  # reserve-wisselkoersen (per 1 EUR)
FIRST = not os.path.exists("seen.json")
SEEN = set() if FIRST else set(json.load(open("seen.json")))
KEYS = ("earnings","guidance","outlook","acqui","merger","takeover","offer","downgrade","upgrade","ceo","cfo","chair",
        "lawsuit","investigation","probe","buyback","dividend","warning","raises","cuts","bankrupt","recall","fda",
        "approval","delist","short seller","resigns","steps down")

def num(x):
    try: return None if pd.isna(x) else float(x)
    except Exception: return None

def clean(o):
    if isinstance(o, float): return None if o != o or o in (float("inf"), float("-inf")) else o
    if isinstance(o, dict): return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list): return [clean(v) for v in o]
    return o

def row(df, *keys):
    for k in keys:
        if df is not None and not df.empty and k in df.index:
            return df.loc[k].dropna().sort_index()
    return pd.Series(dtype=float)

def last(s): return float(s.iloc[-1]) if len(s) else None

def cagr(s, years=None):
    s = s.dropna()
    if years is None and len(s) >= 2 and s.iloc[0] <= 0:
        p = s[s > 0]                       # startjaar negatief: reken vanaf het eerste positieve jaar
        if len(p) >= 2: s = s.loc[p.index[0]:]
    if len(s) < 2 or s.iloc[0] <= 0 or s.iloc[-1] <= 0: return None
    return float((s.iloc[-1] / s.iloc[0]) ** (1 / (years or (len(s) - 1))) - 1)

def pcagr(h, y):
    h = h.dropna()
    if len(h) < 50: return None
    yrs = (h.index[-1] - h.index[0]).days / 365.25
    return cagr(h.iloc[[0, -1]], yrs) if yrs >= y - 0.3 else None

def news(t):
    out = []
    for n in (t.news or [])[:10]:
        c = n.get("content", n)
        u = (c.get("canonicalUrl") or c.get("clickThroughUrl") or {}).get("url") or c.get("link")
        pr = c.get("provider")
        out.append(dict(id=str(n.get("id") or u), titel=c.get("title"), tijd=str(c.get("pubDate") or c.get("providerPublishTime") or ""),
                        bron=pr.get("displayName") if isinstance(pr, dict) else c.get("publisher"), url=u))
    return [o for o in out if o["titel"]]

def insiders(t):
    try: df = t.insider_transactions
    except Exception: return []
    if df is None or df.empty: return []
    out = []
    for _, r in df.head(40).iterrows():
        txt = (str(r.get("Text", "")) + " " + str(r.get("Transaction", ""))).lower()
        kind = "koop" if ("purchase" in txt or "buy" in txt) else "verkoop" if ("sale" in txt or "sell" in txt) else None
        if not kind: continue
        out.append(dict(wie=str(r.get("Insider")), functie=str(r.get("Position") or ""), type=kind,
                        aandelen=int(num(r.get("Shares")) or 0), waarde=num(r.get("Value")), datum=str(r.get("Start Date"))[:10]))
    return sorted(out, key=lambda i: i["datum"], reverse=True)

def fx():
    out = dict(FALL)
    for c in FALL:
        try:
            h = yf.Ticker(f"EUR{c}=X").history(period="5d")["Close"].dropna()
            if len(h): out[c] = float(h.iloc[-1])
        except Exception: pass
    return out

FX = fx()

FXW = {}
def fxw(c):
    """Wekelijkse reeks 'eenheden vreemde valuta per euro' (EURxxx=X)."""
    if c in (None, "EUR"): return None
    if c not in FXW:
        try:
            z = yf.Ticker(f"EUR{c}=X").history(period="max")["Close"].dropna()
            z.index = z.index.tz_localize(None) if z.index.tz is not None else z.index
            FXW[c] = z
        except Exception as e:
            print("FOUT fx", c, e); FXW[c] = None
    return FXW[c]

def in_eur(series, c):
    """Zet een wekelijkse koersreeks om naar euro's, met de wisselkoers van dezelfde week."""
    try:
        f = fxw(c)
        if f is None: return [round(float(v), 2) for v in series]
        out = []
        for d, v in series.items():
            d0 = d.tz_localize(None) if d.tzinfo is not None else d
            out.append(round(float(v) / float(f.asof(d0)), 2))
        return out
    except Exception as e:
        print("FOUT in_eur", c, e); return None

def daily_eur(h, c, n=30):
    try:
        z = h.tail(n)
        v = in_eur(z, c)
        return [[str(d)[:10], x] for d, x in zip(z.index, v)]
    except Exception as e:
        print("FOUT daily", e); return None

def ye_eur(h, c):
    """Slotkoers (in euro) op de laatste handelsdag van het vorige jaar, voor de YTD-berekening."""
    try:
        yr = dt.date.today().year - 1
        z = h[h.index.strftime("%Y-%m-%d") <= f"{yr}-12-31"].tail(1)
        v = in_eur(z, c)
        return v[0] if v else None
    except Exception as e:
        print("FOUT ye", e); return None

def surprises(t, hist):
    out = []
    try:
        df = t.get_earnings_dates(limit=10)
        df = df[df["Reported EPS"].notna()].head(4)
        for d, r in df.iterrows():
            try:
                d = d.tz_convert(hist.index.tz) if hist.index.tz else d.tz_localize(None)
                a, b = hist[hist.index < d], hist[hist.index >= d]
                k = float(b.iloc[min(1, len(b) - 1)] / a.iloc[-1] - 1) if len(a) and len(b) else None
            except Exception: k = None
            out.append(dict(datum=str(d)[:10], s=num(r.get("Surprise(%)")), k=k))
    except Exception: pass
    return out

def one(tk):
    t = yf.Ticker(tk); info = t.info or {}
    raw = t.history(period="max", auto_adjust=True)["Close"]
    px = float(raw.iloc[-1]); day = float(px / raw.iloc[-2] - 1)
    cur = "GBP" if info.get("currency") == "GBp" else info.get("currency")
    hist = raw / 100 if info.get("currency") == "GBp" else raw          # pence -> ponden
    same = not info.get("financialCurrency") or info["financialCurrency"] == cur  # winst in zelfde valuta als koers?
    inc, cf, bs = t.income_stmt, t.cashflow, t.balance_sheet
    eps, fcf = row(inc, "Diluted EPS", "Basic EPS"), row(cf, "Free Cash Flow")
    rev, gp, ebitda = row(inc, "Total Revenue"), row(inc, "Gross Profit"), row(inc, "EBITDA")
    ebit = row(inc, "EBIT", "Operating Income")
    tax = row(inc, "Tax Rate For Calcs").reindex(ebit.index).fillna(.2)
    roic = ((ebit * (1 - tax)) / row(bs, "Invested Capital")).dropna()
    pes = []
    if same:
        for d, e in eps.items():
            if e > 0:
                try:
                    d = d.tz_localize(hist.index.tz) if hist.index.tz else d
                    pes.append(float(hist.asof(d) / e))
                except Exception: pass
    hl = float(hist.iloc[-1])
    fwd = None
    try:
        g = t.earnings_estimate.loc["+1y", "growth"]       # verwachte winstgroei komend jaar (analisten)
        fwd = float(g) if pd.notna(g) else None
    except Exception: pass
    sh = row(bs, "Ordinary Shares Number", "Share Issued")
    dil = cagr(sh) if len(sh) >= 3 else None            # aandelen per jaar: min = terugkopen
    wk = hist.resample("W").last().dropna().tail(260)
    rt = wk.pct_change().dropna()
    vol = float(rt.std() * 52 ** .5) if len(rt) > 50 else None
    dd5 = float((wk / wk.cummax() - 1).min()) if len(wk) > 50 else None
    omr = (ebit / rev).dropna()
    er = surprises(t, hist)
    def ret(d):
        try:
            v = hist.asof(hist.index[-1] - pd.Timedelta(days=d))
            return float(hl / v - 1) if v == v and v > 0 else None
        except Exception: return None
    v50 = float(hl / hist.tail(50).mean() - 1) if len(hist) >= 50 else None
    v200 = float(hl / hist.tail(200).mean() - 1) if len(hist) >= 200 else None
    tgt = info.get("targetMeanPrice"); kd = float(tgt / (info.get("currentPrice") or px) - 1) if tgt and (info.get("currentPrice") or px) else None
    dv = t.dividends; dv = dv.dropna() if dv is not None else pd.Series(dtype=float); dvd = dict(div_ttm=0.0)
    if len(dv):
        if info.get("currency") == "GBp": dv = dv / 100
        yr = dv.groupby(dv.index.year).sum(); full = yr[yr.index < dt.date.today().year].tail(6)
        ttm = float(dv[dv.index > pd.Timestamp.now(tz=dv.index.tz) - pd.Timedelta(days=365)].sum())
        dvd = dict(div_ttm=ttm, div_cagr=cagr(full) if len(full) >= 3 else None, div_reeks=[round(float(x), 3) for x in full.values],
                   div_jaren=[str(i) for i in full.index], div_laatste_datum=str(dv.index[-1])[:10], div_laatste_bedrag=round(float(dv.iloc[-1]), 4))
    rate = 1.0 if cur == "EUR" else FX.get(cur)
    pe, g = info.get("trailingPE"), cagr(eps)
    fin = info.get("sector") == "Financial Services"
    nd, eb, mc = last(row(bs, "Net Debt")), last(ebitda), info.get("marketCap")
    lo, hi = info.get("fiftyTwoWeekLow"), info.get("fiftyTwoWeekHigh")
    div, price = info.get("dividendRate"), info.get("currentPrice") or px
    ins = insiders(t); cut = (dt.date.today() - dt.timedelta(days=180)).isoformat()
    out = dict(naam=META.get(tk, {}).get("naam") or info.get("shortName") or tk, t=tk, sector=info.get("sector"), land=info.get("country"),
        px=px, dag=day, pe=pe, fpe=info.get("forwardPE"), pe_hist=sum(pes)/len(pes) if pes else None,
        peg=(pe / (g * 100)) if pe and g and g > 0 else None, eps_cagr=g, fcf_cagr=cagr(fcf), roic=last(roic),
        bm=last(gp) / last(rev) if last(gp) is not None and last(rev) else None,
        om=last(ebit) / last(rev) if last(ebit) is not None and last(rev) else None,
        nd_ebitda=nd / eb if (nd is not None and eb and eb > 0 and not fin) else None,
        fcf_yield=last(fcf) / mc if (same and mc and last(fcf) is not None) else None,
        pos52=(px - lo) / (hi - lo) if lo is not None and hi and hi > lo else None, mcap=mc,
        yld=dvd["div_ttm"] / hl if hl else 0, koers_eur=(hl / rate) if rate else None, rate=rate, koers_cagr5=pcagr(hist, 5),
        eps=[round(float(x), 2) for x in eps.values], jaren=[str(d.year) for d in eps.index],
        fcf=[round(float(x) / 1e6) for x in fcf.values], fjaren=[str(d.year) for d in fcf.index],
        roic_reeks=[round(float(x), 3) for x in roic.values], rjaren=[str(d.year) for d in roic.index],
        ins_pct=info.get("heldPercentInsiders"), beta=info.get("beta"), ev_ebitda=info.get("enterpriseToEbitda"),
        px_w=[round(float(v), 2) for v in wk.tail(53)], px_e=in_eur(hist.resample("W").last().dropna().tail(600), cur), px_d=daily_eur(hist, cur), px_ye=ye_eur(hist, cur), r1m=ret(30), r1y=ret(365), r5y=ret(1826), v50=v50, v200=v200, kd=kd, analisten=info.get("numberOfAnalystOpinions"), eps_fwd=fwd,
        dil=dil, vol=vol, dd5=dd5, om_reeks=[round(float(x), 3) for x in omr.values], omjaren=[str(d.year) for d in omr.index], er=er,
        nieuws=news(t), insiders=ins[:8],
        ins_koop=sum(i["type"] == "koop" and i["datum"] >= cut for i in ins),
        ins_verkoop=sum(i["type"] == "verkoop" and i["datum"] >= cut for i in ins))
    out.update(dvd)
    try:
        cal = t.calendar or {}
        e = cal.get("Earnings Date"); out["earnings"] = str(e[0]) if e else None
        dp, ex = cal.get("Dividend Date"), cal.get("Ex-Dividend Date")
        out["div_volgende"] = str(dp) if dp else None; out["ex_div"] = str(ex) if ex else None
    except Exception: out.setdefault("earnings", None)
    return out

def notify(key, title, msg, url=None):
    if key in SEEN: return
    SEEN.add(key)
    topic = os.environ.get("NTFY_TOPIC")
    if FIRST or not topic: return
    h = {"Title": title.encode("ascii", "ignore").decode()}
    if url and url.startswith("https://"): h["Click"] = url
    requests.post(f"https://ntfy.sh/{topic}", data=msg.encode("utf-8"), headers=h)

rows, today = [], dt.date.today().isoformat()
for tk in list(H) + [w for w in WL if w not in H]:
    try:
        r = one(tk)
        if tk in H:
            n = r["naam"]
            r.update(weging=H[tk].get("weging"), rend=H[tk].get("rend"))   # percentages, geen bedragen
            if abs(r["dag"]) > .05: notify(f"dag-{tk}-{today}", "Grote beweging", f'{n}: {r["dag"]*100:+.1f}% vandaag')
            if r["earnings"]:
                d = (dt.date.fromisoformat(r["earnings"][:10]) - dt.date.today()).days
                if 0 <= d <= 3: notify(f'c-{tk}-{r["earnings"][:10]}', "Cijfers komen eraan", f"{n} rapporteert over {d} dag(en)")
            for nw in r["nieuws"]:
                if any(k in nw["titel"].lower() for k in KEYS): notify("n-" + nw["id"], f"{n}: nieuws", nw["titel"], nw["url"])
            for i in r["insiders"]:
                notify(f'i-{tk}-{i["wie"]}-{i["datum"]}-{i["type"]}-{i["aandelen"]}', f"{n}: insider {i['type']}",
                       f'{i["wie"]} ({i["functie"]}) {i["type"]} {i["aandelen"]:,} aandelen op {i["datum"]}')
        else:
            r["watch"] = True
        m = META.get(tk, {})   # jouw indeling gaat voor op Yahoo
        r.update(sector=m.get("sector") or r.get("sector"), land=m.get("land") or r.get("land"), munt=m.get("munt"),
                 eigenaar=m.get("eigenaar"), ai=m.get("ai"))
        rows.append(r)
    except Exception as e:
        print("FOUT", tk, e)
def _wk(sym, start=None, n=None):
    z = yf.Ticker(sym).history(period="max", auto_adjust=True)["Close"].dropna()
    w = z.resample("W").last().dropna()
    if start: w = w[w.index >= start]
    if n: w = w.tail(n)
    return w

def idx_series(sym, cur, start=None, n=None):
    try:
        w = _wk(sym, start, n)
        return in_eur(w, cur) if cur and cur != "EUR" else [round(float(v), 2) for v in w], (w.index[0].date().isoformat() if len(w) else None)
    except Exception as e:
        print("FOUT index", sym, e); return None, None

def idx_dag(sym):
    try:
        z = yf.Ticker(sym).history(period="10d")["Close"].dropna()
        return float(z.iloc[-1] / z.iloc[-2] - 1)
    except Exception as e:
        print("FOUT dag", sym, e); return None

sp5, _ = idx_series("^GSPC", "USD", n=261)
wr5, _ = idx_series("IWDA.AS", "EUR", n=261)
spL, d0 = idx_series("^GSPC", "USD", start="2016-01-01")
wrL, d1 = idx_series("IWDA.AS", "EUR", start="2016-01-01")
vix, _ = idx_series("^VIX", "EUR", n=53)
def idx_daily(sym, cur):
    try:
        z = yf.Ticker(sym).history(period="3mo")["Close"].dropna()
        return daily_eur(z, cur)
    except Exception as e:
        print("FOUT idx_daily", sym, e); return None

sp5u, _ = idx_series("^GSPC", "EUR", n=261)
def idx_ye(sym, cur):
    try:
        z = yf.Ticker(sym).history(period="2y")["Close"].dropna()
        return ye_eur(z, cur)
    except Exception as e:
        print("FOUT idx_ye", sym, e); return None
YE = dict(sp500=idx_ye("^GSPC", "USD"), sp500_usd=idx_ye("^GSPC", "EUR"), wereld=idx_ye("IWDA.AS", "EUR"))
INDICES = dict(sp500=sp5, wereld=wr5, vix=vix, sp500_usd=sp5u, ye=YE, dagen=dict(sp500=idx_daily("^GSPC","USD"), sp500_usd=idx_daily("^GSPC","EUR"), wereld=idx_daily("IWDA.AS","EUR")), dag=dict(sp500=idx_dag("^GSPC"), wereld=idx_dag("IWDA.AS")),
               lang=dict(start=d0, sp500=spL, wereld=wrL))

# Koersen van reeds verkochte aandelen (alleen ISIN's staan in verkocht.json; geen bedragen of aantallen)
SOLD = json.load(open("verkocht.json")) if os.path.exists("verkocht.json") else []
SMAP = json.load(open("verkocht_map.json")) if os.path.exists("verkocht_map.json") else {}
VERK = {}
for isin in SOLD:
    try:
        sym = SMAP.get(isin)
        if sym is None:
            q = yf.Search(isin, max_results=3).quotes
            sym = q[0]["symbol"] if q else ""
            SMAP[isin] = sym
        if not sym: continue
        t = yf.Ticker(sym)
        h = t.history(period="max", auto_adjust=True)["Close"].dropna()
        if len(h) < 10: continue
        c = (t.history_metadata or {}).get("currency")
        w = h.resample("W").last().ffill().tail(600)
        if c == "GBp": w, c = w / 100, "GBP"
        v = in_eur(w, c)
        if v: VERK[isin] = dict(s=str(w.index[0])[:10], v=v)
    except Exception as e:
        print("FOUT verkocht", isin, e)
json.dump(SMAP, open("verkocht_map.json", "w"))


# --- compounder-score (zelfde regels als de app) en push bij nieuwe kwalificatie
SERIAL = ["CSU.TO","TOI.V","LMN.V","CHG.DE","TEQ.ST","ROKO-B.ST","LIFCO-B.ST"]
def cscore(r):
    n = lambda k: r.get(k) if isinstance(r.get(k), (int, float)) else None
    eps, fcf, roic, nd, peg = n("eps_cagr"), n("fcf_cagr"), n("roic"), n("nd_ebitda"), n("peg")
    ins, dil = n("ins_pct"), n("dil")
    if r.get("t") in SERIAL and fcf is not None and (eps is None or eps < fcf): eps = fcf
    s = 0
    if eps is not None: s += 2 if eps >= .15 else 1 if eps >= .12 else 0
    if fcf is not None: s += 2 if fcf >= .15 else 1 if fcf >= .10 else 0
    s += 2 if nd is None or nd <= 1 else 1 if nd <= 2 else 0
    s += 1 if roic is None else 2 if roic >= .15 else 1 if roic >= .10 else 0
    s += (1 if (ins or 0) >= .05 else 0) + (1 if dil is not None and dil <= .01 else 0)
    if peg is not None: s += 2 if peg <= 1.5 else 1 if peg <= 2 else 0
    return s
try:
    first_c = "comp:init" not in SEEN
    SEEN.add("comp:init")
    for r in rows:
        sc = cscore(r)
        if sc >= 9:
            lab = "Verborgen compounder" if (r.get("mcap") or 0) < 25e9 else "Gevestigde compounder"
            if first_c: SEEN.add("comp:" + r["t"])
            else: notify("comp:" + r["t"], "Nieuwe compounder", f'{r.get("naam") or r["t"]}: {lab}, score {sc} van 12')
except Exception as e:
    print("FOUT compounder", e)


# --- grote beleggers: 13F-meldingen bij de SEC (publiek, per kwartaal)
GURUS = {"Berkshire Hathaway": "1067983", "Stanley Druckenmiller (Duquesne)": "1536411",
         "Mohnish Pabrai (Dalal Street)": "1549575", "Chris Hohn (TCI)": "1647251",
         "Chuck Akre (Akre Capital)": "1112520", "Li Lu (Himalaya Capital)": "1709323"}
SEC_H = {"User-Agent": "PortefeuilleCockpit rofite9480@boffweb.com", "Accept-Encoding": "gzip, deflate"}
def sec_get(u):
    time.sleep(0.4)
    r = requests.get(u, headers=SEC_H, timeout=30); r.raise_for_status(); return r
def parse_13f(cik, acc):
    import xml.etree.ElementTree as ET
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"
    items = sec_get(base + "index.json").json()["directory"]["item"]
    xs = [i["name"] for i in items if i["name"].lower().endswith(".xml") and "primary_doc" not in i["name"].lower()]
    best = {}
    for nm in xs:
        root = ET.fromstring(sec_get(base + nm).content)
        for el in root.iter():
            if el.tag.endswith("infoTable"):
                g = lambda t: next((c.text for c in el.iter() if c.tag.endswith(t) and c.text), None)
                try: v = float(g("value") or 0)
                except Exception: continue
                name = (g("nameOfIssuer") or "").strip()
                if not name: continue
                b = best.setdefault(name, dict(n=name, v=0.0)); b["v"] += v
        if best: break
    tot = sum(b["v"] for b in best.values()) or 1
    return sorted([dict(n=b["n"], p=round(b["v"] / tot * 100, 2)) for b in best.values()], key=lambda x: -x["p"])
GRU = {}
for gname, cik in GURUS.items():
    try:
        sub = sec_get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json").json()
        rc = sub["filings"]["recent"]
        fl = [(rc["filingDate"][i], rc["accessionNumber"][i], rc["reportDate"][i]) for i in range(len(rc["form"])) if rc["form"][i] == "13F-HR"]
        fl.sort(reverse=True)
        if not fl: continue
        cur = parse_13f(cik, fl[0][1]); prv = parse_13f(cik, fl[1][1]) if len(fl) > 1 else []
        pm = {x["n"]: x["p"] for x in prv}; cm = {x["n"]: x["p"] for x in cur}
        for x in cur:
            o = pm.get(x["n"])
            x["o"] = o
            x["d"] = "nieuw" if o is None else "meer" if x["p"] > o * 1.1 else "minder" if x["p"] < o * .9 else "gelijk"
        verk = [dict(n=n, o=p) for n, p in sorted(pm.items(), key=lambda kv: -kv[1]) if n not in cm][:15]
        GRU[gname] = dict(naam=sub.get("name"), periode=fl[0][2], gemeld=fl[0][0], aantal=len(cur), top=cur[:40], verkocht=verk, vorig=(fl[1][2] if len(fl) > 1 else None))
        if "13f:init" in SEEN:
            for x in cur[:30]:
                if x["d"] == "nieuw" and x["p"] >= 1:
                    notify(f'13f-{cik}-{fl[0][2]}-{x["n"]}', f"{gname}: nieuwe positie", f'{x["n"]} ({x["p"]}% van de portefeuille)')
    except Exception as e:
        print("FOUT 13F", gname, e)
SEEN.add("13f:init")

json.dump(clean(dict(bijgewerkt=dt.datetime.now(dt.timezone.utc).isoformat(), versie=2, doel_cagr=P.get("doel_cagr", 15),
          grenzen=P.get("grenzen"), clusters=P.get("clusters"), vastgezet=P.get("vastgezet", []), overig=P.get("overig"), rows=rows, indices=INDICES, verkocht=VERK, gurus=GRU)),
          open("data.json", "w"), default=str)
json.dump(sorted(SEEN)[-4000:], open("seen.json", "w"))
