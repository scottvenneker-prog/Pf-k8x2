"""Haalt koersen + fundamentals (Yahoo Finance via yfinance) voor portfolio.json, schrijft data.json, stuurt ntfy-pushes."""
import json, os, time, datetime as dt
import pandas as pd, requests, yfinance as yf

P = json.load(open("portfolio.json"))
H, WLD = P["posities"], P.get("watchlist", {})   # H: ticker -> {naam, sector, land, weging, rend, ...}; alleen percentages
WL = list(WLD)
META = {**WLD, **H}
SC = json.load(open("scan.json")) if os.path.exists("scan.json") else {}   # wekelijkse scan: kandidaten die aan je regels voldoen
SCT = [x["tk"] for x in SC.get("lijst", []) if x["tk"] not in META]
for x in SC.get("lijst", []):
    if x["tk"] in SCT: META[x["tk"]] = dict(naam=x.get("naam"), sector=x.get("sector"), land=x.get("land"), munt=x.get("munt"))
FALL = {"USD": 1.1143, "GBP": 0.8344, "SEK": 11.025, "CAD": 1.5743}  # reserve-wisselkoersen (per 1 EUR)
FIRST = not os.path.exists("seen.json")
SEEN = set() if FIRST else set(json.load(open("seen.json")))
KEYS = ("acqui","merger","takeover","lawsuit","investigation","probe","bankrupt","recall","delist","short seller",
        "resigns","steps down","profit warning","cuts dividend","dividend cut","fraud","antitrust","export ban")   # alleen echt ingrijpend nieuws


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
    for c in list(FALL) + ["HKD", "CNY", "TWD", "KRW", "INR", "JPY", "SGD", "BRL", "MXN", "ZAR", "DKK", "NOK", "CHF"]:
        try:
            h = yf.Ticker(f"EUR{c}=X").history(period="5d")["Close"].dropna()
            if len(h): out[c] = float(h.iloc[-1])
        except Exception: pass
    return out

FX = fx()

def fcf_rend(f, mc, cur, fcur, same):
    """Kasstroom / beurswaarde. Bij verschillende valuta (bijv. rapportage in USD, notering in CAD) omrekenen via de euro-koers van vandaag."""
    try:
        if f is None or not mc: return None
        if same: return f / mc
        if cur in (None, "GBp") or fcur in (None, "GBp") or cur not in FX or fcur not in FX: return None
        y = (f / FX[fcur]) / (mc / FX[cur])
        return y if 0 < y < 0.3 else None
    except Exception: return None

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

def wk_full(s, n=600):
    """Wekelijkse slotkoersen (week eindigt op zondag), altijd doorlopend tot en met de huidige week.
    Ontbreekt de laatste week (bv. beurs nog dicht), dan nemen we de laatste koers over, zodat
    de app elke reeks op dezelfde weken uitlijnt."""
    w = s.resample("W").last().dropna()
    if not len(w): return w
    try:
        if w.index.tz is not None: w.index = w.index.tz_localize(None)
        end = pd.Timestamp.now("UTC").tz_localize(None).normalize()
        end = end + pd.Timedelta(days=(6 - end.weekday()) % 7)
        full = pd.date_range(w.index[0].normalize(), end, freq="W")
        w = w.reindex(full.union(w.index)).ffill().reindex(full).dropna()
    except Exception as e:
        print("FOUT wk_full", e)
    return w.tail(n)

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

def uitslag(t):
    """Laatste gemelde kwartaalcijfers (alleen als ze van de laatste 7 dagen zijn): gerapporteerde en verwachte winst per aandeel."""
    try:
        df = t.get_earnings_dates(limit=6)
        df = df[df["Reported EPS"].notna()]
        if not len(df): return None
        d = df.index[0]; r = df.iloc[0]
        dd = str(d)[:10]
        if (pd.Timestamp.now().normalize() - pd.Timestamp(dd)).days > 7: return None
        return dict(datum=dd, eps=num(r.get("Reported EPS")), schat=num(r.get("EPS Estimate")), s=num(r.get("Surprise(%)")))
    except Exception: return None

def despike(s):
    """Haalt losse foute koersen weg (bijv. 0,01 in plaats van 0,55 bij Carclo): een punt dat meer dan 10 keer boven of 10 keer onder het lopende midden ligt."""
    try:
        if len(s) < 30: return s
        med = s.rolling(41, center=True, min_periods=10).median()
        return s[(s / med).between(.1, 10) | med.isna()]
    except Exception: return s

def one(tk):
    t = yf.Ticker(tk); info = t.info or {}
    raw = despike(t.history(period="max", auto_adjust=True)["Close"].dropna())
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
    try:   # koersen zonder dividendcorrectie (wel splitsingen) voor waardering in het verleden; de dividenden zitten apart in de kasstromen
        ru = despike(t.history(period="max", auto_adjust=False)["Close"].dropna())
        histu = ru / 100 if info.get("currency") == "GBp" else ru
        if len(histu) < 10: histu = hist
    except Exception: histu = hist
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
                   div_jaren=[str(i) for i in full.index], div_laatste_datum=str(dv.index[-1])[:10], div_laatste_bedrag=round(float(dv.iloc[-1]), 4),
                   div_12m=[[str(i)[:10], round(float(v), 4)] for i, v in dv[dv.index > pd.Timestamp.now(tz=dv.index.tz) - pd.Timedelta(days=400)].items()])
    # omzet, uitkering en kasstroomkwaliteit (gerapporteerde cijfers, in de rapportagevaluta)
    ni = row(inc, "Net Income Common Stockholders", "Net Income")
    rv = rev.dropna() if rev is not None else pd.Series(dtype=float)
    if len(rv) >= 3:   # boekhoudkundige knik in de reeks (bijv. Adyen: bruto omzet tot 2022, netto daarna): alleen het laatste consistente stuk gebruiken
        br = [i for i in range(1, len(rv)) if rv.iloc[i - 1] > 0 and not .33 <= float(rv.iloc[i] / rv.iloc[i - 1]) <= 3]
        if br: rv = rv.iloc[br[-1]:]
    rev_g, rev_gt = (cagr(rv) if len(rv) >= 2 else None), None
    try:
        tr, mq = info.get("totalRevenue"), info.get("mostRecentQuarter")
        if same and tr and mq and len(rv) >= 2 and tr > 0 and float(rv.iloc[-1]) > 0:
            qd = pd.Timestamp(mq, unit="s")
            if .5 <= tr / float(rv.iloc[-1]) <= 3 and qd > rv.index[-1].tz_localize(None) + pd.Timedelta(days=60):
                pr = rv[rv > 0]; yrs = (qd - pr.index[0].tz_localize(None)).days / 365.25
                if yrs >= 1.5: rev_gt = float((tr / float(pr.iloc[0])) ** (1 / yrs) - 1)
    except Exception as e: print("REV", tk, e)
    fl_ = float(fcf.dropna().iloc[-1]) if len(fcf.dropna()) else None
    rl_ = float(rv.iloc[-1]) if len(rv) else None
    nl_ = float(ni.dropna().iloc[-1]) if len(ni.dropna()) else None
    dpd = row(cf, "Cash Dividends Paid", "Common Stock Dividend Paid", "Dividends Paid")
    dl_ = abs(float(dpd.dropna().iloc[-1])) if len(dpd.dropna()) else None
    fcf_marge = fl_ / rl_ if (fl_ is not None and rl_) else None
    fcf_conv = fl_ / nl_ if (fl_ is not None and nl_ and nl_ > 0) else None
    uit_fcf = dl_ / fl_ if (dl_ is not None and fl_ and fl_ > 0 and dl_ > 0) else None
    uit_ni = dl_ / nl_ if (dl_ is not None and nl_ and nl_ > 0 and dl_ > 0) else None
    ind_ = info.get("industry") or ""   # bij banken, verzekeraars en beleggingsmaatschappijen zeggen omzet en vrije kasstroom weinig
    if any(k in ind_ for k in ("Insurance", "Banks", "Asset Management", "Capital Markets")):
        fcf_marge = fcf_conv = uit_fcf = None
        if any(k in ind_ for k in ("Insurance", "Asset Management")): rev_g = rev_gt = None
    rate = 1.0 if cur == "EUR" else FX.get(cur)
    pe, g = info.get("trailingPE"), cagr(eps)
    g_fy, eps_ttm, ttm_d = g, None, None
    try:   # winstgroei t/m de laatste 12 maanden, als die recenter zijn dan het laatste boekjaar (zelfde valuta, redelijke verhouding)
        ttm, mq, s_ = info.get("trailingEps"), info.get("mostRecentQuarter"), eps.dropna()
        if same and ttm and mq and len(s_) >= 2 and ttm > 0 and float(s_.iloc[-1]) > 0:
            qd = pd.Timestamp(mq, unit="s")
            if .25 <= ttm / float(s_.iloc[-1]) <= 4 and qd > s_.index[-1].tz_localize(None) + pd.Timedelta(days=60):
                p_ = s_[s_ > 0]; yrs = (qd - p_.index[0].tz_localize(None)).days / 365.25
                if yrs >= 1.5:
                    g = float((ttm / float(p_.iloc[0])) ** (1 / yrs) - 1); eps_ttm = round(float(ttm), 2); ttm_d = str(qd)[:10]
    except Exception as e: print("TTM", tk, e)
    fin = info.get("sector") == "Financial Services"
    nd, eb, mc = last(row(bs, "Net Debt")), last(ebitda), info.get("marketCap")
    lo, hi = info.get("fiftyTwoWeekLow"), info.get("fiftyTwoWeekHigh")
    div, price = info.get("dividendRate"), info.get("currentPrice") or px
    ins = insiders(t); cut = (dt.date.today() - dt.timedelta(days=180)).isoformat()
    _om = (info.get("longBusinessSummary") or "").replace("\n", " ").strip()
    if len(_om) > 330:
        _k = _om[:330]; _p = _k.rfind(". ")
        _om = (_k[:_p + 1] if _p > 120 else _k.rstrip() + "…")
    out = dict(omschr=_om or None, naam=META.get(tk, {}).get("naam") or info.get("shortName") or tk, t=tk, sector=info.get("sector"), land=info.get("country"),
        px=px, dag=day, pe=pe, fpe=info.get("forwardPE"), pe_hist=sum(pes)/len(pes) if pes else None, pe_n=len(pes),
        peg=(pe / (g * 100)) if pe and g and g > 0 else None, rev_cagr=rev_g, rev_cagr_ttm=rev_gt, rev_reeks=[round(float(x) / 1e6) for x in rv.values], rev_jaren=[str(d.year) for d in rv.index], fin_munt=info.get("financialCurrency") or cur, fcf_marge=fcf_marge, fcf_conv=fcf_conv, uit_fcf=uit_fcf, uit_ni=uit_ni, ni_last=(round(nl_ / 1e6) if nl_ is not None else None), eps_cagr=g, eps_cagr_fy=g_fy, eps_ttm=eps_ttm, eps_ttm_d=ttm_d, fcf_cagr=cagr(fcf), roic=last(roic),
        bm=last(gp) / last(rev) if last(gp) is not None and last(rev) else None,
        om=last(ebit) / last(rev) if last(ebit) is not None and last(rev) else None,
        nd_ebitda=nd / eb if (nd is not None and eb and eb > 0 and not fin) else None,
        fcf_yield=fcf_rend(last(fcf), mc, cur, info.get("financialCurrency"), same),
        pos52=(px - lo) / (hi - lo) if lo is not None and hi and hi > lo else None, mcap=mc,
        yld=dvd["div_ttm"] / hl if hl else 0, koers_eur=(hl / rate) if rate else None, rate=rate, koers_cagr5=pcagr(hist, 5),
        eps=[round(float(x), 2) for x in eps.values], jaren=[str(d.year) for d in eps.index],
        fcf=[round(float(x) / 1e6) for x in fcf.values], fjaren=[str(d.year) for d in fcf.index],
        roic_reeks=[round(float(x), 3) for x in roic.values], rjaren=[str(d.year) for d in roic.index],
        ins_pct=info.get("heldPercentInsiders"), beta=info.get("beta"), ev_ebitda=info.get("enterpriseToEbitda"), roe=info.get("returnOnEquity"), fin=1 if fin else 0,
        px_w=[round(float(v), 2) for v in wk.tail(53)], px_e=in_eur(wk_full(histu, 600), cur), px_d=daily_eur(hist, cur), px_ye=ye_eur(histu, cur), r1m=ret(30), r1y=ret(365), r5y=ret(1826), v50=v50, v200=v200, kd=kd, analisten=info.get("numberOfAnalystOpinions"), eps_fwd=fwd,
        dil=dil, vol=vol, dd5=dd5, om_reeks=[round(float(x), 3) for x in omr.values], omjaren=[str(d.year) for d in omr.index], er=er, uitslag=uitslag(t),
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

_NPUSH = 0
def notify(key, title, msg, url=None):
    global _NPUSH
    if key in SEEN: return
    SEEN.add(key)
    topic = os.environ.get("NTFY_TOPIC")
    if FIRST or not topic: return
    if _NPUSH >= 3: return          # hooguit 3 meldingen per run, de rest verdwijnt in de app
    _NPUSH += 1
    h = {"Title": title.encode("ascii", "ignore").decode()}
    if url and url.startswith("https://"): h["Click"] = url
    requests.post(f"https://ntfy.sh/{topic}", data=msg.encode("utf-8"), headers=h)

# ---- tweede databron: Financial Modeling Prep (optioneel, alleen met FMP_KEY). Weekcache om binnen het gratis aantal aanroepen te blijven.
FMPK = os.environ.get("FMP_KEY")
FC = json.load(open("fmp_cache.json")) if os.path.exists("fmp_cache.json") else {}
ALERTS = json.load(open("alerts.json")) if os.path.exists("alerts.json") else {}
NOTES = json.load(open("notities.json")) if os.path.exists("notities.json") else {}   # {ticker: {w: reden, r: {regel: grens}}}
NREG = dict(eps=("Winstgroei per jaar", "eps_cagr", 100, "onder"), fcf=("Kasstroomgroei per jaar", "fcf_cagr", 100, "onder"),
            roic=("ROIC", "roic", 100, "onder"), om=("Winstmarge", "om", 100, "onder"), nd=("Schuld/EBITDA", "nd_ebitda", 1, "boven"))   # {ticker: instapkoers in euro}, gezet via de app of de chat

def fmp_get(path, tk):
    r = requests.get(f"https://financialmodelingprep.com/stable/{path}", params=dict(symbol=tk, limit=5, apikey=FMPK), timeout=20)
    if r.status_code != 200: raise RuntimeError(f"{path} {r.status_code}")
    j = r.json()
    if not isinstance(j, list) or not j: raise RuntimeError(f"{path} leeg")
    return sorted(j, key=lambda x: x.get("date", ""))

def fmp_cagr(v):
    v = [x for x in v if x is not None]
    if len(v) < 3 or v[0] <= 0 or v[-1] <= 0: return None
    return (v[-1] / v[0]) ** (1 / (len(v) - 1)) - 1

def fmp_data(tk):
    if not FMPK: return None
    c = FC.get(tk); td = dt.date.today()
    if c and (td - dt.date.fromisoformat(c["d"])).days < (30 if c.get("v") is None else 7): return c.get("v")
    try:
        inc, cf = fmp_get("income-statement", tk)[-4:], fmp_get("cash-flow-statement", tk)[-4:]
        rv = inc[-1].get("revenue")
        v = dict(eps_cagr=fmp_cagr([x.get("epsDiluted") for x in inc]), fcf_cagr=fmp_cagr([x.get("freeCashFlow") for x in cf]),
                 om=(inc[-1].get("operatingIncome") / rv) if rv and inc[-1].get("operatingIncome") is not None else None)
    except Exception as e:
        print("FMP", tk, e); v = None
    FC[tk] = dict(d=td.isoformat(), v=v)
    time.sleep(0.3)
    return v

def fmp_check(r, v):
    """Zet de FMP-cijfers naast die van Yahoo en markeer grote verschillen (groei meer dan 10 punten, marge meer dan 8)."""
    if not v: return None
    afw = []
    for k, lim in (("eps_cagr", .10), ("fcf_cagr", .10), ("om", .08)):
        a, b = r.get(k), v.get(k)
        if a is not None and b is not None and abs(a - b) > lim: afw.append(k)
    return dict(v, afw=afw)

rows, today = [], dt.date.today().isoformat()
for tk in list(H) + [w for w in WL if w not in H] + SCT:
    try:
        r = one(tk)
        r["fmp"] = fmp_check(r, fmp_data(tk))
        lv = ALERTS.get(tk)
        if lv:
            r["instap"] = lv
            if r.get("koers_eur") and r["koers_eur"] <= lv:
                notify(f"alert-{tk}-{lv}-{today[:7]}", "Instapkoers bereikt", f'{META.get(tk, {}).get("naam") or tk} staat op € {r["koers_eur"]:.0f}, je instapkoers is € {lv:g}', None)
        nt = NOTES.get(tk)
        if nt:
            r["notitie"] = nt
            for k, g in (nt.get("r") or {}).items():
                if k not in NREG: continue
                lab, fld, mul, richt = NREG[k]; x = r.get(fld)
                if x is None: continue
                x *= mul
                if (x < g) if richt == "onder" else (x > g):
                    notify(f"note-{tk}-{k}-{g}-{today[:7]}", "Koopnotitie: regel geraakt", f'{META.get(tk, {}).get("naam") or tk}: {lab.lower()} is {x:.1f}, je grens is {richt} {g:g}')
        if tk in H:
            n = r["naam"]
            r.update(weging=H[tk].get("weging"), rend=H[tk].get("rend"))   # percentages, geen bedragen
            if abs(r["dag"]) > .10: notify(f"dag-{tk}-{today}", "Grote beweging", f'{n}: {r["dag"]*100:+.1f}% vandaag')
            if r["earnings"]:
                d = (dt.date.fromisoformat(r["earnings"][:10]) - dt.date.today()).days
                if 0 <= d <= 3: notify(f'c-{tk}-{r["earnings"][:10]}', "Cijfers komen eraan", f"{n} rapporteert over {d} dag(en)")
            for nw in r["nieuws"]:
                if any(k in nw["titel"].lower() for k in KEYS) and (n.split()[0].lower().strip(",.") in nw["titel"].lower() or tk.split(".")[0].lower() in nw["titel"].lower()): notify("n-" + nw["id"], f"{n}: nieuws", nw["titel"], nw["url"])
            for i in r["insiders"]:
                if i["type"] != "koop": continue     # verkopen hebben vaak privé redenen
                notify(f'i-{tk}-{i["wie"]}-{i["datum"]}-{i["type"]}-{i["aandelen"]}', f"{n}: insider {i['type']}",
                       f'{i["wie"]} ({i["functie"]}) {i["type"]} {i["aandelen"]:,} aandelen op {i["datum"]}')
        else:
            r["watch"] = True
            if tk in SCT: r["ontdekt"] = True
        m = META.get(tk, {})   # jouw indeling gaat voor op Yahoo
        r.update(sector=m.get("sector") or r.get("sector"), land=m.get("land") or r.get("land"), munt=m.get("munt"),
                 eigenaar=m.get("eigenaar"), ai=m.get("ai"))
        rows.append(r)
    except Exception as e:
        print("FOUT", tk, e)
def _wk(sym, start=None, n=None):
    z = yf.Ticker(sym).history(period="max", auto_adjust=True)["Close"].dropna()
    w = z.resample("W").last().dropna()
    # Is de week van vandaag er nog niet (bv. maandagochtend voor de Amerikaanse opening), dan de laatste koers
    # doorzetten, zodat de index op dezelfde weekas staat als de aandelen (anders schuift hij een week op)
    try:
        t0 = dt.date.today(); eind = t0 + dt.timedelta(days=(6 - t0.weekday()))
        while len(w) and w.index[-1].date() < eind:
            w = pd.concat([w, pd.Series([w.iloc[-1]], index=[w.index[-1] + pd.Timedelta(days=7)])])
    except Exception as e:
        print("FOUT pad", e)
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
        h = t.history(period="max", auto_adjust=False)["Close"].dropna()
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
         "Chuck Akre (Akre Capital)": "1112520", "Li Lu (Himalaya Capital)": "1709323",
         "Bill Ackman (Pershing Square)": "1336528"}
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
                cu = (g("cusip") or "").strip().upper()[:6] or name
                b = best.setdefault(cu, dict(n=name, v=0.0, big=0.0, c=cu)); b["v"] += v
                if v > b["big"]: b["big"] = v; b["n"] = name
        if best: break
    tot = sum(b["v"] for b in best.values()) or 1
    return sorted([dict(n=b["n"], c=b["c"], p=round(b["v"] / tot * 100, 2)) for b in best.values()], key=lambda x: -x["p"])
GRU = {}
for gname, cik in GURUS.items():
    try:
        sub = sec_get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json").json()
        rc = sub["filings"]["recent"]
        fl = [(rc["filingDate"][i], rc["accessionNumber"][i], rc["reportDate"][i]) for i in range(len(rc["form"])) if rc["form"][i] == "13F-HR"]
        fl.sort(reverse=True)
        if not fl: continue
        cur = parse_13f(cik, fl[0][1]); prv = parse_13f(cik, fl[1][1]) if len(fl) > 1 else []
        pm = {x["c"]: x["p"] for x in prv}; cm = {x["c"]: x["p"] for x in cur}; pn = {x["c"]: x["n"] for x in prv}
        for x in cur:
            o = pm.get(x["c"])
            x["o"] = o
            x["d"] = "nieuw" if o is None else "meer" if x["p"] > o * 1.1 else "minder" if x["p"] < o * .9 else "gelijk"
        verk = [dict(n=pn[c], o=p) for c, p in sorted(pm.items(), key=lambda kv: -kv[1]) if c not in cm][:15]
        GRU[gname] = dict(naam=sub.get("name"), periode=fl[0][2], gemeld=fl[0][0], aantal=len(cur), top=cur[:40], verkocht=verk, vorig=(fl[1][2] if len(fl) > 1 else None))
        if "13f:init" in SEEN:
            for x in cur[:30]:
                if x["d"] == "nieuw" and x["p"] >= 1:
                    notify(f"13f-{cik}-{fl[0][2]}-{x['c']}", f"{gname}: nieuwe positie", f'{x["n"]} ({x["p"]}% van de portefeuille)')
    except Exception as e:
        print("FOUT 13F", gname, e)
SEEN.add("13f:init")


# --- uitzonderlijk nieuws: handmatig (uitzonderlijk.json) plus automatisch herkend ---
BK = ("spy", "spion", "scandal", "affair", "arrest", "indict", "raid", "sanction", "fraud", "investigation", "probe", "bankrupt", "resigns", "steps down",
      "recall", "hack", "breach", "short seller", "delist", "criminal", "charged", "subpoena", "ban ")
def _tijd(x):
    try:
        x = str(x)
        if x.isdigit(): return dt.datetime.fromtimestamp(int(x), dt.timezone.utc)
        return dt.datetime.fromisoformat(x.replace("Z", "+00:00"))
    except Exception: return None
def uitzonderlijk(rows):
    out = {}
    try:
        for m in json.load(open("uitzonderlijk.json")): out[m["tk"]] = m
    except Exception: pass
    nu = dt.datetime.now(dt.timezone.utc)
    for r in rows:
        try:
            if r["tk"] in out or r.get("watch"): continue
            nm = (r.get("naam") or "").split()[0].lower().strip(",."); tk0 = r["tk"].split(".")[0].lower()
            hits = []
            for nw in r.get("nieuws") or []:
                t = _tijd(nw.get("tijd")); ti = (nw.get("titel") or "").lower()
                if t and (nu - t).total_seconds() <= 48 * 3600 and (nm in ti or tk0 in ti) and any(k in ti for k in BK): hits.append(nw)
            dag = r.get("dag") or 0
            if len(hits) >= 3 or (hits and dag <= -.08):
                out[r["tk"]] = dict(tk=r["tk"], naam=r.get("naam"), datum=nu.date().isoformat(), ernst="hoog", auto=True,
                    reden=f"{len(hits)} berichten in 48 uur" + (f" en koers {dag*100:+.1f}% vandaag" if dag <= -.08 else ""),
                    titel=hits[0]["titel"], tekst="Meerdere nieuwsberichten in korte tijd over een ingrijpend onderwerp. Lees de bronnen zelf voor de details.",
                    bronnen=[dict(naam=h.get("bron") or "bron", url=h.get("url")) for h in hits[:3] if h.get("url")])
                notify(f"uit-{r['tk']}-{nu.date()}", f"{r.get('naam')}: uitzonderlijk nieuws", hits[0]["titel"], hits[0].get("url"))
        except Exception as e: print("uitzonderlijk", r.get("tk"), e)
    return list(out.values())
UIT = uitzonderlijk(rows)
json.dump(clean(dict(bijgewerkt=dt.datetime.now(dt.timezone.utc).isoformat(), versie=2, doel_cagr=P.get("doel_cagr", 15),
          grenzen=P.get("grenzen"), clusters=P.get("clusters"), vastgezet=P.get("vastgezet", []), overig=P.get("overig"), rows=rows, indices=INDICES, verkocht=VERK, gurus=GRU, scan=SC, uitzonderlijk=UIT)),
          open("data.json", "w"), default=str)
json.dump(sorted(SEEN)[-4000:], open("seen.json", "w"))
if FMPK: json.dump(FC, open("fmp_cache.json", "w"))
