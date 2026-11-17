"""Doble chequeo de Power4 contra capturas de MetaPro: añade cada captura nueva a CAPTURAS (hora UTC y etapas
mensual/semanal/diario/6H) y ejecuta: python tools/validar_power4.py"""
import sys
sys.path.insert(0, r"C:\Users\javie\Proyectos\etapas-weinstein")
import ccxt
import pandas as pd
from etapas import power4

ex = ccxt.binance({"enableRateLimit": True})
K = {"1M": 2, "1w": 3, "1d": 5, "6h": 3}
DUR = {"1w": pd.Timedelta(days=7), "1d": pd.Timedelta(days=1), "6h": pd.Timedelta(hours=6)}
CAPTURAS = [
    # (momento UTC, {moneda: etapas mensual/semanal/diario/6H}); 21-oct sin 6H
    (pd.Timestamp("2026-10-21 19:00"), {"ETH": "424-", "BTC": "124-"}),
    (pd.Timestamp("2026-11-17 17:17"), {"BTC": "4344", "ETH": "4244", "SOL": "1241",
                                       "UNI": "4244", "JUP": "4241", "AAVE": "4344"}),
]
cache = {}


def get(sym, tf):
    if (sym, tf) not in cache:
        rows = ex.fetch_ohlcv(f"{sym}/USDT", tf, limit=1000)
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df.pop("ts"), unit="ms")
        cache[sym, tf] = df
    return cache[sym, tf]


def stage(sym, tf, cut):
    if tf == "1M":
        month = cut.normalize().replace(day=1)
        m = get(sym, "1M")
        m = m[m.index < month]
        d = get(sym, "1d")
        d = d[(d.index >= month) & (d.index + pd.Timedelta(days=1) <= cut)]
        cur = None if d.empty else pd.Series({"open": d.open.iloc[0], "high": d.high.max(), "low": d.low.min(),
                                              "close": d.close.iloc[-1], "volume": d.volume.sum()}, name=month)
        return power4.latest(m, K[tf], cur)[0]
    df = get(sym, tf)
    return power4.latest(df[df.index + DUR[tf] <= cut], K[tf])[0]


total = hits = 0
for cut, mp in CAPTURAS:
    print(f"Captura {cut} UTC")
    for sym, exp in mp.items():
        got = []
        for tf, e in zip(["1M", "1w", "1d", "6h"], exp):
            if e == "-":
                got.append("-")
                continue
            g = stage(sym, tf, cut)
            got.append(str(g))
            total += 1
            hits += str(g) == e
        mark = "OK" if "".join(got) == exp else "<-- difiere"
        print(f"  {sym:5} MetaPro {exp}  Power4 {''.join(got)}  {mark}")
print(f"Coinciden {hits} de {total}")
