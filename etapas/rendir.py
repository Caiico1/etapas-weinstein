"""Tus tokens rindiendo: la alternativa al pool con los MISMOS tokens que pide el rango, cada uno a su mejor
rendimiento simple, sin riesgo de pool.

  - ETH / WETH → como wstETH (staking de Lido): el rendimiento sale de cómo sube el precio wstETH/WETH en el
    pool de Ethereum de MetaProDB (mediana diaria, para no coger las excursiones de una hora). Lleva el riesgo
    de que wstETH cotice con descuento frente a ETH al deshacer.
  - el resto (estables, WBTC, cbBTC, LINK…) → depositado en Aave V3 de la misma red (supply_apy hora a hora de
    MetaProDB). Si Aave no lo tiene, sin rendimiento.

En los escenarios, el rendimiento de los últimos 90 días, fijo; en la validación, el real del plazo. Igual que
«vs mantener», el valor final = los tokens (con lo que han rendido) al precio final + los costes que el pool
habría pagado (cambio y gas), para comparar con el mismo capital de partida.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import backtest as B
from . import simulador as S

DIAS_TIPO = 90
WSTETH_POOL = ("ethereum", "0x109830a1aaad605bbf02a9dfa7b0b92ec2fb7daa")     # wstETH/WETH 0,01 %
ETH = {"WETH", "ETH"}
ALIAS_AAVE = {"ETH": "WETH", "USDC.E": "USDC.e"}


def via(symbol: str) -> str:
    return "staking (wstETH)" if symbol.upper() in ETH else "Aave"


def serie_staking(con) -> pd.Series:
    """wstETH/WETH, mediana diaria (WETH por wstETH)."""
    r = con.execute("""SELECT date_trunc('day', hour) d, median(price) FROM pool_hour_v
                       WHERE chain = ? AND pool_id = ? AND price > 0 GROUP BY 1 ORDER BY 1""", list(WSTETH_POOL)).fetchall()
    return pd.Series([float(x[1]) for x in r], index=pd.DatetimeIndex([x[0] for x in r]))


def serie_aave(con, chain: str, symbol: str) -> pd.Series:
    """supply_apy hora a hora del token en Aave de esa red (vacía si no lo tiene)."""
    sym = ALIAS_AAVE.get(symbol.upper(), symbol)
    r = con.execute("""SELECT hour, avg(supply_apy) FROM aave_reserve_hour_v
                       WHERE chain = ? AND market = ? AND lower(symbol) = lower(?) GROUP BY 1 ORDER BY 1""",
                    [chain, chain, sym]).fetchall()
    return pd.Series([float(x[1] or 0) for x in r], index=pd.DatetimeIndex([x[0] for x in r]), dtype=float)


def series(pool: dict, con=None) -> dict:
    """Las series que hacen falta para los dos tokens del pool: {symbol: ("staking"|"aave", serie)}."""
    own = con is None
    con = con or B._connect()
    try:
        out = {}
        for s in (pool["symbol0"], pool["symbol1"]):
            out[s] = ("staking", serie_staking(con)) if s.upper() in ETH else ("aave", serie_aave(con, pool["chain"], s))
        return out
    finally:
        if own:
            con.close()


def apy_reciente(serie: tuple, hasta: pd.Timestamp, dias: int = DIAS_TIPO) -> float:
    """Rendimiento anual de los últimos `dias` hasta `hasta` (lo que se sabía ese día)."""
    tipo, s = serie
    s = s[s.index <= hasta]
    if s.empty:
        return 0.0
    if tipo == "aave":
        return float(s[s.index > hasta - pd.Timedelta(days=dias)].mean())
    a = s[s.index <= hasta - pd.Timedelta(days=dias)]
    if a.empty:
        return 0.0
    return float((s.iloc[-1] / a.iloc[-1]) ** (365 / dias) - 1)


def crecimiento(serie: tuple, desde: pd.Timestamp, hasta: pd.Timestamp) -> float:
    """Factor real por el que creció el token entre `desde` y `hasta` (1 = nada)."""
    tipo, s = serie
    if s.empty:
        return 1.0
    if tipo == "aave":
        x = s[(s.index >= desde) & (s.index < hasta)]
        return float(np.prod(1 + x.to_numpy() / 8760)) if len(x) else 1.0
    a, b = s[s.index <= desde], s[s.index <= hasta]
    return float(b.iloc[-1] / a.iloc[-1]) if len(a) and len(b) else 1.0


def simulate_rendir(h: S.History, paths: S.Paths, rg: S.Rango, g0, g1, capital: float = 5000.0,
                    gas: float | None = None, abierta: bool = False) -> np.ndarray:
    """Valor final de los tokens que pediría el rango `rg`, creciendo ×g0 y ×g1 (escalares o por escenario),
    más los costes que el pool habría pagado (como `Sim.hodl`). `abierta`: los tokens de una posición que ya
    existe (sin costes de entrada)."""
    base = S.simulate(h, paths, rg, capital, gas, abierta=abierta)
    pool = h.pool
    d1 = 10 ** pool["dec1"]
    sp, u1 = paths.sp, paths.usd1
    sp0, u10 = float(sp[0, 0]), float(u1[0, 0])
    a0u, a1u = B._amounts(sp0, B._sqrt(rg.tl), B._sqrt(rg.tu))
    per_l = (a0u * sp0 ** 2 + a1u) * u10 / d1
    L = (capital - base.costs) / per_l
    se, ue = sp[:, -1], u1[:, -1]
    return (a0u * L * g0 * se ** 2 + a1u * L * g1) * ue / d1 + base.costs


def alternativa(h: S.History, dias: int, paths: S.Paths, rg: S.Rango, capital: float = 5000.0,
                gas: float | None = None, sr: dict | None = None) -> dict:
    """Los tokens del rango rindiendo, con el rendimiento de los últimos 90 días, en los escenarios."""
    sr = sr or series(h.pool)
    s0, s1 = h.pool["symbol0"], h.pool["symbol1"]
    y0, y1 = apy_reciente(sr[s0], h.hour_now), apy_reciente(sr[s1], h.hour_now)
    t = dias / 365
    fv = simulate_rendir(h, paths, rg, (1 + y0) ** t, (1 + y1) ** t, capital, gas)
    lp = S.simulate(h, paths, rg, capital, gas)
    return {"tokens": [{"token": s0, "via": via(s0), "apy": y0}, {"token": s1, "via": via(s1), "apy": y1}],
            "texto": rg.texto, "valor_media": float(fv.mean()), "valor_p10": float(np.percentile(fv, 10)),
            "valor_p90": float(np.percentile(fv, 90)), "vs_mantener": float(np.mean(fv - lp.hodl)),
            "lp_vs_rendir": float(np.mean(lp.final - fv))}
