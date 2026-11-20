"""¿Abrir un rango LP cerca de la SMA20 rinde mejor que lejos? Mismo método de rango que /pool.

    python tools/estudio_sma20.py [salida.csv]

Calibra los umbrales de etapas/sesamo.py (Barrio Sésamo). Resultado nov-2026 en su docstring."""
import math, sys
import numpy as np, pandas as pd
sys.path.insert(0, r"C:\Users\javie\Proyectos\etapas-weinstein")
from etapas.analysis import analyze_symbol
from etapas.data import fetch_pair_candles
from etapas.config import TIMEFRAMES, LP_QUANTILE
from etapas.indicators import expected_range, sma
from etapas.liquidez import divergence

PAIRS = [("BTC","USDT"),("ETH","USDT"),("ETH","BTC"),("SOL","USDT"),("LINK","USDT")]
rows = []
for b,q in PAIRS:
    a = analyze_symbol(b, fetch=lambda t,cfg,b=b,q=q: fetch_pair_candles(b,q,cfg))
    for key in ["1d","1w","1M"]:
        r = a.timeframes[key]
        if r.status!="ok": continue
        df, atr = r.candles, r.history["atr"]
        s20 = sma(df["close"],20)
        cfg = TIMEFRAMES[key]
        start = max(60, len(df)-1-(1500 if key=="1d" else 400))
        for t in range(start, len(df)-1):
            if pd.isna(s20.iloc[t]) or pd.isna(atr.iloc[t]): continue
            lo,hi = expected_range(df.iloc[:t+1], atr.iloc[:t+1], LP_QUANTILE, cfg.range_lookback)
            if pd.isna(lo) or pd.isna(hi) or lo<=0: continue
            c, nxt = df["close"].iloc[t], df.iloc[t+1]
            half = math.log(hi/lo)/2
            d = (c - s20.iloc[t])/atr.iloc[t]
            rows.append(dict(par=f"{b}/{q}", tf=key, d=d, dpct=c/s20.iloc[t]-1,
                             inside=nxt["low"]>=lo and nxt["high"]<=hi,
                             res=divergence(c,nxt["close"],lo,hi), scaled=divergence(c,nxt["close"],lo,hi)/half))
df = pd.DataFrame(rows)
df["absd"]=df.d.abs()
df["bucket"]=pd.cut(df.absd,[0,0.5,1,1.5,2,3,99],right=False)
pd.set_option("display.width",200)
out = df.groupby(["tf","bucket"],observed=True).agg(n=("res","size"),dentro=("inside","mean"),
      res_med=("res","mean"),res_p10=("res",lambda x: x.quantile(.1)),scaled=("scaled","mean"))
print(out.round(4))
print()
df["lado"]=np.where(df.d>=0,"arriba","abajo")
print(df.groupby(["tf","lado",pd.cut(df.absd,[0,1,2,99],right=False)],observed=True).agg(n=("res","size"),dentro=("inside","mean"),res=("res","mean")).round(4))
print()
print(df.groupby(["tf","par"]).absd.describe(percentiles=[.25,.5,.75]).round(2))
df.to_csv(sys.argv[1] if len(sys.argv)>1 else "estudio.csv", index=False)
