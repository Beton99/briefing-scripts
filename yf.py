#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Yahoo 取数层 (yf_fetch) —— 盘前/盘后简报共用。【批量·加固·历史区间修正版】
- 当前行情：v7 quote + crumb（批量，双主机重试）
- 历史区间：v8 spark range=3y interval=1d（批量，分块），算 1个月/3个月/1年/2年(往前滚动累计)
- spark 为扁平结构 {sym:{timestamp,close}}，本版已修正时间戳解析
- 美股交易日：盘后取指数最近收盘的美东日期；盘前取美东当前日期
"""
import sys, time, random, datetime, urllib.request, urllib.error, urllib.parse, http.cookiejar, json
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

def _http(url, cj=None, timeout=25, retries=4):
    req = urllib.request.Request(url, headers=UA)
    op = (urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
          if cj is not None else urllib.request.build_opener())
    last = None
    for a in range(retries):
        try:
            with op.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e.code
            if e.code in (429,500,502,503,999) and a < retries-1:
                time.sleep(2.5*(a+1)+random.random()); continue
            return e.code, ""
        except Exception as e:
            last = f"ERR:{type(e).__name__}"
            if a < retries-1: time.sleep(1.5*(a+1)); continue
            return last, ""
    return last, ""

def _chunks(lst, n):
    for i in range(0, len(lst), n): yield lst[i:i+n]

def _bj(ts):
    try: return (datetime.datetime.utcfromtimestamp(int(ts))+datetime.timedelta(hours=8)).strftime("%m-%d %H:%M")
    except Exception: return "?"

def _et_date(ts):
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.fromtimestamp(int(ts), ZoneInfo("America/New_York")).date()
    except Exception:
        u = datetime.datetime.utcfromtimestamp(int(ts))
        return (u - datetime.timedelta(hours=4 if 3<=u.month<=11 else 5)).date()

def _next_session_date(et_dt):
    """盘前用:返回下一个将开盘的美股交易日(已过当日9:30ET或周末则进位,跳过周末;不含节假日)。"""
    d = et_dt.date()
    if et_dt.time() >= datetime.time(9,30) or d.weekday() >= 5:
        d = d + datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d = d + datetime.timedelta(days=1)
    return d

_STATE2SESS = {"PRE":"盘前","PREPRE":"盘前","REGULAR":"盘中","POST":"盘后","POSTPOST":"盘后","CLOSED":"收盘"}

def _session():
    cj = http.cookiejar.CookieJar()
    for u in ("https://fc.yahoo.com","https://finance.yahoo.com"): _http(u, cj, retries=2)
    for h in ("query1","query2"):
        c, b = _http(f"https://{h}.finance.yahoo.com/v1/test/getcrumb", cj)
        if c == 200 and b and len(b) < 40: return cj, b, h
    return cj, None, None

def _fetch_v7(symbols, cj, crumb, host):
    if not crumb: return None
    hosts = list(dict.fromkeys([host,"query1","query2"]))
    out = {}
    for bi, batch in enumerate(_chunks(symbols, 50), 1):
        syms = ",".join(urllib.parse.quote(s) for s in batch); ok=False
        _F = "symbol,shortName,currency,marketState,regularMarketPrice,regularMarketChangePercent,regularMarketPreviousClose,regularMarketTime,preMarketPrice,preMarketChangePercent,postMarketPrice,postMarketChangePercent"
        for h in hosts:
            url = f"https://{h}.finance.yahoo.com/v7/finance/quote?symbols={syms}&fields={_F}&crumb={urllib.parse.quote(crumb)}"
            c, b = _http(url, cj, retries=6)
            print(f"[诊断] v7 批次{bi} {h}: HTTP={c}")
            if c==200 and b:
                try:
                    for q in json.loads(b)["quoteResponse"]["result"]:
                        s, st = q.get("symbol"), q.get("marketState","")
                        pre_p, post_p = q.get("preMarketPrice"), q.get("postMarketPrice")
                        ext=extp=None
                        if st in ("PRE","PREPRE"): ext,extp=pre_p,q.get("preMarketChangePercent")
                        elif st in ("POST","POSTPOST","CLOSED"): ext,extp=post_p,q.get("postMarketChangePercent")
                        if ext is None:  # marketState 未命中但响应里有盘前/盘后价时兜底取
                            if pre_p is not None: ext,extp=pre_p,q.get("preMarketChangePercent")
                            elif post_p is not None: ext,extp=post_p,q.get("postMarketChangePercent")
                        out[s]={"symbol":s,"name":q.get("shortName") or s,
                                "regular":q.get("regularMarketPrice"),"reg_pct":q.get("regularMarketChangePercent"),
                                "prev":q.get("regularMarketPreviousClose"),"ext":ext,"ext_pct":extp,
                                "session":_STATE2SESS.get(st,st or "?"),"rmt":q.get("regularMarketTime"),
                                "time_bj":_bj(q.get("regularMarketTime")),"ccy":q.get("currency","")}
                    ok=True; break
                except Exception as e: print(f"[诊断] v7 批次{bi} 解析异常 {type(e).__name__}")
        if not ok: print(f"[诊断] v7 批次{bi} 全主机失败")
        time.sleep(3.0)
    return out or None

def _fetch_history(symbols, cj=None):
    """spark range=3y interval=1d 分块(带cookie)。spark 为扁平结构 {sym:{timestamp,close}}。返回 {sym:[(date,close)...]}。"""
    out = {}
    for bi, batch in enumerate(_chunks(symbols, 20), 1):  # Yahoo spark 上限=20
        syms = ",".join(urllib.parse.quote(s) for s in batch)
        for h in ("query1","query2"):
            url = f"https://{h}.finance.yahoo.com/v8/finance/spark?symbols={syms}&range=3y&interval=1d"
            c, b = _http(url, cj, retries=3)
            print(f"[诊断] 历史 批次{bi} {h}: HTTP={c}")
            if c==200 and b:
                try:
                    j = json.loads(b)
                    src = j.get("spark",{}).get("result") if isinstance(j.get("spark"),dict) else None
                    items = {r.get("symbol"):r for r in src} if src else j
                    before = len(out); sample_keys = None
                    for s, r in items.items():
                        if not isinstance(r, dict): continue
                        if sample_keys is None: sample_keys = list(r.keys())
                        ts = r.get("timestamp") or []
                        cl = r.get("close") or []
                        if (not ts or not cl) and isinstance(r.get("response"), list):
                            resp = r["response"][0]
                            ts = resp.get("timestamp") or ts
                            cl = (resp.get("indicators",{}).get("quote",[{}])[0] or {}).get("close") or cl
                        pairs = [(_et_date(t), c2) for t, c2 in zip(ts, cl) if c2 is not None]
                        if pairs: out[s] = pairs
                    if len(out) == before:
                        print(f"[诊断] 历史 批次{bi} 解析0条，样本字段={sample_keys}")
                    break
                except Exception as e: print(f"[诊断] 历史 批次{bi} 解析异常 {type(e).__name__}")
        time.sleep(3.5+random.random()*1.5)
    print(f"[诊断] 历史成功 {len(out)}/{len(symbols)} 个标的有区间数据")
    return out

def _pret(last, pairs):
    """往前滚动累计涨跌：1个月=30天,3个月=90天,1年=365天,2年=730天。"""
    R = {"m1":None,"m3":None,"y1":None,"y2":None,"prev_pct":None}
    if not last or not pairs: return R
    if len(pairs) >= 3:
        p1, p2 = pairs[-2][1], pairs[-3][1]
        if p1 and p2: R["prev_pct"] = (p1/p2-1)*100
    today = pairs[-1][0]
    def onbefore(d):
        v=None
        for dt,c in pairs:
            if dt<=d: v=c
            else: break
        return v
    for key,days in (("m1",30),("m3",90),("y1",365),("y2",730)):
        c = onbefore(today - datetime.timedelta(days=days))
        if c: R[key] = (last/c-1)*100
    return R

def _fetch_chart_hist_one(sym, cj):
    """v8/chart 逐个取3年日线历史(spark对部分外盘指数不返回长历史时的兜底)。"""
    for h in ("query1","query2"):
        url = f"https://{h}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=3y&interval=1d"
        c, b = _http(url, cj, retries=3)
        if c == 200 and b:
            try:
                r = json.loads(b).get("chart",{}).get("result")
                if r:
                    res = r[0]; ts = res.get("timestamp") or []
                    cl = (res.get("indicators",{}).get("quote",[{}])[0] or {}).get("close") or []
                    pairs = [(_et_date(t), c2) for t, c2 in zip(ts, cl) if c2 is not None]
                    if pairs: return pairs
            except Exception: pass
    return None

def fetch_all(symbols):
    cj, crumb, host = _session()
    print(f"[诊断] getcrumb: {'OK@'+host if crumb else '失败'}")
    q = _fetch_v7(symbols, cj, crumb, host) or {}
    hist = _fetch_history(symbols, cj)
    # 二次重试：首轮缺现价的标的，停顿后只重打这几个(化解瞬时限流抖动)
    missing = [s for s in symbols if s not in q or q[s].get("regular") is None]
    if missing:
        print(f"[诊断] 首轮v7缺 {len(missing)} 只，停顿8s后二次重试")
        time.sleep(8)
        q2 = _fetch_v7(missing, cj, crumb, host) or {}
        for k, v in q2.items():
            if v.get("regular") is not None: q[k] = v
        still = len([s for s in missing if s not in q or q[s].get("regular") is None])
        print(f"[诊断] 二次重试后仍缺 {still} 只(将用历史末值兜底)")
    # v7 缺失(撞限流)的标的，用历史末值补现价/前收，避免整只未获取
    filled = 0
    for s in symbols:
        if s not in q or q[s].get("regular") is None:
            p = hist.get(s)
            if p and len(p) >= 2:
                q[s] = {"symbol":s,"name":s,"regular":p[-1][1],"prev":p[-2][1],
                        "reg_pct":(p[-1][1]/p[-2][1]-1)*100 if p[-2][1] else None,
                        "ext":None,"ext_pct":None,"session":"历史","rmt":None,"time_bj":"?","ccy":""}
                filled += 1
    if filled: print(f"[诊断] v7缺失用历史补 {filled} 只")
    out = {}
    for s in symbols:
        d = q.get(s)
        if d and d.get("regular") is not None:
            d = dict(d); d["ret"] = _pret(d["regular"], hist.get(s)); out[s] = d
        else:
            out[s] = {"symbol":s,"error":"未获取(批量未返回)"}
    # 区间历史全空的标的(如部分外盘指数 spark 不给长历史)，改用 v8/chart 逐个补
    need = [s for s in out if not out[s].get("error")
            and not any(v is not None for v in (out[s].get("ret") or {}).values())]
    need = need[:60]  # 安全上限，避免逐个打过多
    if need:
        print(f"[诊断] {len(need)} 只无区间历史，改用chart逐个补: {','.join(need)}")
        fixed = 0
        for s in need:
            ph = _fetch_chart_hist_one(s, cj)
            if ph:
                out[s]["ret"] = _pret(out[s]["regular"], ph); fixed += 1
            time.sleep(0.5)
        print(f"[诊断] chart补历史成功 {fixed}/{len(need)} 只")
    return out

# ---------- 标的 ----------
INDEX_FUT = ["ES=F","NQ=F","YM=F"]
INDEX = ["^GSPC","^IXIC","^DJI","^SOX","^VIX"]
GLOBAL = ["000001.SS","399001.SZ","^N225","^KS11","^TWII","^AXJO","^GDAXI","^STOXX"]
MACRO = ["CL=F","GC=F","DX-Y.NYB","BTC-USD","CNH=X","AUDUSD=X","CAD=X"]
WATCHLIST = ["GOOGL","AMZN","SKM","SPMO","AVGO","NBIS","ORCL","SMCI","DRAM","MU","IREN",
             "HOOD","NVDA","AIQ","SMH","BE","META","MRVL","RKLB","MDB","INTC","SPCX","ASML",
             "AAPL","AAOI","COHR","GLW","LITE","LLY","KLAC",
             "MPWR","VST","DELL","ARM","005930.KS","2DG.F",
             "VRTX","CBRS","INOD","REGN",
             "000660.KS","2330.TW",
             "1530.HK","6990.HK","1801.HK","3692.HK","1276.HK",
             "002938.SZ","002384.SZ","600584.SS","688525.SS","603629.SS"]
SECTORS = [("信息技术","XLK"),("通信服务","XLC"),("可选消费","XLY"),("必需消费","XLP"),
           ("能源","XLE"),("金融","XLF"),("医疗保健","XLV"),("工业","XLI"),
           ("材料","XLB"),("房地产","XLRE"),("公用事业","XLU")]
SECTOR_SYMS = [e for _, e in SECTORS]
# AI产业链瓶颈地图：14环节，每环节2-3纯标的/代表ETF(排他，反映环节而非个股)
AICHAIN = [("1.EDA/IP",        ["SNPS","CDNS","ARM"]),
           ("2.算力芯片",      ["NVDA","AVGO","AMD"]),
           ("3.晶圆制造",      ["TSM","INTC","GFS"]),
           ("4.半导体设备",    ["ASML","AMAT","LRCX"]),
           ("5.存储HBM",       ["MU","000660.KS","005930.KS"]),
           ("6.先进封装",      ["AMKR","ASX"]),
           ("7.光模块光互联",  ["COHR","CIEN","FN"]),
           ("8.电气互连",      ["CRDO","ALAB"]),
           ("9.数据中心",      ["EQIX","APLD","CRWV"]),
           ("10.散热",          ["VRT","TT","MOD"]),
           ("11.电力设备",      ["GEV","POWL","GRID"]),
           ("12.电网建设",      ["PWR","MYRG","MTZ"]),
           ("13.核能与铀",      ["CCJ","CEG","URNM"]),
           ("14.物理AI机器人",  ["ROK","BOTZ","ROBO"])]
# 瓶颈地图代表标的中、当前自选股universe尚未包含的(取价+历史，不做个股新闻扫描)
AICHAIN_EXTRA = ["SNPS","CDNS","AMD","TSM","GFS","AMAT","LRCX",
                 "AMKR","ASX","CIEN","FN","CRDO","ALAB","EQIX","APLD","CRWV",
                 "VRT","TT","MOD","GEV","POWL","GRID","PWR","MYRG","MTZ",
                 "CCJ","CEG","URNM","ROK","BOTZ","ROBO"]

def _p(v, pct=False):
    if v is None: return "—"
    try: return f"{v:+.2f}%" if pct else (f"{v:.2f}" if abs(v)<100000 else f"{v:.0f}")
    except Exception: return str(v)

def _hdr_periods(): return f"{'涨跌幅':>8}{'1个月':>8}{'3个月':>8}{'1年':>8}{'2年':>8}"
def _row_periods(d):
    r=d.get("ret",{})
    return f"{_p(d.get('reg_pct'),1):>8}{_p(r.get('m1'),1):>8}{_p(r.get('m3'),1):>8}{_p(r.get('y1'),1):>8}{_p(r.get('y2'),1):>8}"

def _print_group(title, data, order, with_hist=True):
    print(f"\n── {title} " + "─"*max(0,28-len(title)))
    if with_hist:
        print(f"{'代码':<10}{'收盘':>11}{_hdr_periods()}{'前收':>10}{'前收涨跌':>8}{'延时价':>11}{'延时':>8} 时段")
    else:
        print(f"{'代码':<10}{'收盘':>11}{'涨跌幅':>8}{'前收':>10}{'前收涨跌':>8}{'延时价':>11}{'延时':>8} 时段")
    for s in order:
        d = data.get(s, {})
        if d.get("error"): print(f"{s:<10}{'未获取':>11}  ({d['error'][:22]})"); continue
        prev_pct = (d.get("ret") or {}).get("prev_pct")
        if with_hist:
            print(f"{d['symbol']:<10}{_p(d.get('regular')):>11}{_row_periods(d)}{_p(d.get('prev')):>10}{_p(prev_pct,1):>8}{_p(d.get('ext')):>11}{_p(d.get('ext_pct'),1):>8} {d.get('session','')}")
        else:
            print(f"{d['symbol']:<10}{_p(d.get('regular')):>11}{_p(d.get('reg_pct'),1):>8}{_p(d.get('prev')):>10}{_p(prev_pct,1):>8}{_p(d.get('ext')):>11}{_p(d.get('ext_pct'),1):>8} {d.get('session','')}")

if __name__ == "__main__":
    now_bj = datetime.datetime.utcnow()+datetime.timedelta(hours=8)
    all_syms = INDEX_FUT+INDEX+GLOBAL+MACRO+WATCHLIST+SECTOR_SYMS+AICHAIN_EXTRA
    t0=time.time(); data=fetch_all(all_syms)
    gspc=data.get("^GSPC",{}); us_close=_et_date(gspc["rmt"]) if gspc.get("rmt") else None
    try:
        from zoneinfo import ZoneInfo; et_now=datetime.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        u=datetime.datetime.utcnow(); et_now=(u-datetime.timedelta(hours=4 if 3<=u.month<=11 else 5))
    us_next=_next_session_date(et_now)
    print(f"美股交易日(盘后用·指数最近收盘) {us_close}")
    print(f"美股日期(盘前用·下一交易日) {us_next}")
    print(f"报告生成 {now_bj.strftime('%Y-%m-%d %H:%M')} 北京时间")
    ok=sum(1 for v in data.values() if not v.get("error"))
    print(f"数据源 Yahoo 批量 | 成功 {ok}/{len(all_syms)} | 耗时 {time.time()-t0:.1f}s")
    if ok < len(all_syms)*0.5:
        print("!!! 取数失败：成功率不足50%。简报顶部标【行情取数失败，缺数版本，勿据此判断】，留未获取，禁止联网补价。")
    for title,g in [("指数期货",INDEX_FUT),("指数+SOX+VIX",INDEX),("全球指数",GLOBAL),("自选股",WATCHLIST)]:
        _print_group(title, data, g)
    _print_group("大宗/汇率/加密", data, MACRO, with_hist=False)
    print(f"\n── S&P500 行业(全11) ──")
    print(f"{'行业':<8}{'代码':<7}{_hdr_periods()}{'前收涨跌':>8}")
    for name,etf in SECTORS:
        d=data.get(etf,{})
        if d.get("error"): print(f"{name:<8}{etf:<7}{'未获取':>8}")
        else: print(f"{name:<8}{etf:<7}{_row_periods(d)}{_p((d.get('ret') or {}).get('prev_pct'),1):>8}")
    print(f"\n── AI产业链(代表标的) ──")
    print(f"{'环节':<14}{'代表':<8}{'收盘':>11}{_hdr_periods()}{'前收涨跌':>8}{'延时价':>11}{'延时':>8}")
    for seg,reps in AICHAIN:
        for i,sym in enumerate(reps):
            d=data.get(sym,{}); lab=seg if i==0 else ""
            if d.get("error"): print(f"{lab:<14}{sym:<8}{'未获取':>11}")
            else: print(f"{lab:<14}{sym:<8}{_p(d.get('regular')):>11}{_row_periods(d)}{_p((d.get('ret') or {}).get('prev_pct'),1):>8}{_p(d.get('ext')):>11}{_p(d.get('ext_pct'),1):>8}")
    print("\n说明: 涨跌幅=当日;1个月/3个月/1年/2年=对比往前30/90/365/730天的累计涨跌(非年化)。延时=盘前/盘后涨跌。")
    print("说明: 现金指数盘前不交易,盘前阶段收盘列为上一交易日收盘,看期货定方向。S&P行业用对应SPDR ETF。")
    print("说明: 抬头/文件名用美股交易日(盘后)或美股日期(盘前);生成时间用北京时间。本脚本是行情唯一来源,未获取禁止新闻回填。")
