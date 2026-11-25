"""Cubrir una posición de liquidez con un corto en un perpetuo (Bybit), como alternativa a la cobertura
con préstamo de Aave (que hoy no se puede abrir: USDC y USDT tienen LTV 0 desde el 29-oct-2026).

Con el capital C en un pool activo/estable: una parte V va al pool y otra, M, de margen aislado en USDT a un
corto del activo por una parte c de lo que el pool tiene en activo al abrir (N = c·s·V). El margen se elige
para que el corto aguante la mayor subida del activo en un tramo del plazo del histórico (+10 puntos), como
el HF de Aave: M = N · ((r − 1) + MMR·r), con r = esa subida + 0,1. Así V = C / (1 + c·s·m).

  - el corto gana lo que el activo baja (y pierde lo que sube): compensa la pérdida del pool;
  - cobra el funding si es positivo (lo normal) y lo paga si es negativo; en los escenarios, el funding
    medio de los últimos 90 días, fijo; en la validación, el real hora a hora;
  - comisión de taker al abrir y al cerrar el corto; si el margen no aguanta, liquidación: se pierde el margen
    y el corto desaparece.

El histórico de funding se descarga de la API pública de Bybit (sin clave) y se guarda en out/ (local).
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest as B
from . import simulador as S

OUT = Path(__file__).resolve().parent.parent / "out"
BYBIT = "https://api.bybit.com/v5/market/funding/history"
TAKER = 0.00055                    # comisión de taker de Bybit en perpetuos USDT
MMR = 0.005                        # margen de mantenimiento (primer tramo de ETH y BTC)
FUNDING_DIAS = 90
DESDE_MS = 1_727_740_800_000       # 1-oct-2024 (MetaProDB empieza el 17-oct-2024)
PERPS = {"WETH": "ETHUSDT", "ETH": "ETHUSDT", "WBTC": "BTCUSDT", "CBBTC": "BTCUSDT", "TBTC": "BTCUSDT",
         "BTC": "BTCUSDT", "SOL": "SOLUSDT", "LINK": "LINKUSDT", "UNI": "UNIUSDT", "AAVE": "AAVEUSDT",
         "ARB": "ARBUSDT", "OP": "OPUSDT"}


def simbolo(pool: dict) -> tuple[str, bool] | None:
    """(perpetuo, activo es token0) si el par es activo/estable y el activo tiene perpetuo en Bybit."""
    s0, s1 = pool["symbol0"], pool["symbol1"]
    e0, e1 = s0 in B._STABLE, s1 in B._STABLE
    if e0 == e1:
        return None
    activo = s1 if e0 else s0
    p = PERPS.get(activo.upper())
    return (p, not e0) if p else None


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "etapas-simulador/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def funding(sym: str, cache_h: float = 12, path: Path | None = None, get=_get) -> pd.Series:
    """Funding de cada 8 h (tanto por uno) indexado por la hora, desde oct-2024. Caché local en out/."""
    path = path or OUT / f"funding_{sym}.json"
    try:
        c = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - c["t"] < cache_h * 3600:
            return _serie(c["filas"])
    except (OSError, ValueError, KeyError):
        c = {"filas": []}
    filas = {int(t): float(r) for t, r in c.get("filas", [])}
    fin = int(time.time() * 1000)
    while True:
        d = get(f"{BYBIT}?category=linear&symbol={sym}&endTime={fin}&limit=200")
        lst = (d.get("result") or {}).get("list") or []
        if not lst:
            break
        nuevos = 0
        for x in lst:
            t = int(x["fundingRateTimestamp"])
            nuevos += t not in filas
            filas[t] = float(x["fundingRate"])
        fin = min(int(x["fundingRateTimestamp"]) for x in lst) - 1
        if fin < DESDE_MS or nuevos == 0:
            break
    rows = sorted(filas.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"t": time.time(), "filas": rows}), encoding="utf-8")
    return _serie(rows)


def _serie(rows) -> pd.Series:
    s = pd.Series([r for _, r in rows], index=pd.to_datetime([t for t, _ in rows], unit="ms").floor("h"))
    return s[~s.index.duplicated()].sort_index()


def apr_reciente(f: pd.Series, hasta: pd.Timestamp, dias: int = FUNDING_DIAS) -> float:
    """Funding anualizado medio de los últimos `dias` hasta `hasta` (positivo: el corto cobra)."""
    x = f[(f.index > hasta - pd.Timedelta(days=dias)) & (f.index <= hasta)]
    if x.empty:
        return 0.0
    por_dia = len(x) / dias
    return float(x.mean() * por_dia * 365)


def funding_horas(f: pd.Series, horas: pd.Series) -> np.ndarray:
    """Funding real de cada hora de `horas` (el de la liquidación de esa hora; 0 en las demás)."""
    return f.reindex(pd.to_datetime(horas.to_numpy()).floor("h")).fillna(0.0).to_numpy()[1:]


def margen(subida: float) -> float:
    """Margen por $ de corto para aguantar una subida `subida` (×) más 10 puntos, con el MMR."""
    r = subida + 0.1
    return (r - 1) + MMR * r


def cubierta(h: S.History, paths: S.Paths, rg: S.Rango, c: float, capital: float, gas_tx: float,
             asset0: bool, m: float, apr: float = 0.0, funding_h: np.ndarray | None = None) -> dict:
    """Pool + corto en cada camino. `funding_h` (tamaño H): funding real por hora; si no, `apr` fijo."""
    S_, H = paths.sp.shape[0], paths.sp.shape[1] - 1
    pa = S._precio_activo(h, paths.sp, paths.usd1, asset0)
    sp0, u10 = float(paths.sp[0, 0]), float(paths.usd1[0, 0])
    a0u, a1u = B._amounts(sp0, B._sqrt(rg.tl), B._sqrt(rg.tu))
    v0, v1 = a0u * sp0 ** 2 * u10, a1u * u10
    s = (v0 if asset0 else v1) / (v0 + v1)
    V = capital / (1 + c * s * m)
    N = c * s * V
    M = m * N
    lp = S.simulate(h, paths, rg, V, gas_tx)
    if N <= 0:
        return {"final": lp.final, "V": V, "M": 0.0, "N": 0.0, "s": s, "lp": lp, "liquidada": np.zeros(S_, bool),
                "funding": np.zeros(S_), "comisiones_perp": 0.0, "margen_min": np.full(S_, np.inf)}
    u = N / pa[:, :1]                                            # unidades del activo en corto
    fr = np.broadcast_to(funding_h, (S_, H)) if funding_h is not None else np.full((S_, H), apr / 8760)
    fund = np.concatenate([np.zeros((S_, 1)), np.cumsum(u * pa[:, 1:] * fr, axis=1)], axis=1)
    pnl = u * (pa[:, :1] - pa)
    eq = M - N * TAKER + pnl + fund
    mant = MMR * u * pa
    malo = eq <= mant
    liq = malo.any(axis=1)
    ratio = eq / np.where(u * pa > 0, u * pa, 1)
    fin = np.where(liq, 0.0, eq[:, -1] - u[:, 0] * pa[:, -1] * TAKER)
    return {"final": lp.final + fin, "V": V, "M": M, "N": N, "s": s, "lp": lp, "liquidada": liq,
            "funding": np.where(liq, 0.0, fund[:, -1]), "comisiones_perp": N * TAKER * 2,
            "margen_min": ratio.min(axis=1)}


def cobertura(h: S.History, dias: int, rangos: list[S.Rango], paths: S.Paths, capital: float = 5000,
              gas: float | None = None, est_paths: S.Paths | None = None, est_info: list[dict] | None = None,
              coberturas: tuple = S.COBERTURAS, f: pd.Series | None = None) -> dict | None:
    """Como `simulador.cobertura`, con un corto en Bybit en vez del préstamo. None si no aplica;
    {"error": …} si no se puede descargar el funding."""
    sim = simbolo(h.pool)
    if sim is None:
        return None
    sym, asset0 = sim
    try:
        f = funding(sym) if f is None else f
    except OSError as e:
        return {"error": f"no se pudo leer el funding de {sym} en Bybit ({type(e).__name__})"}
    apr = apr_reciente(f, h.hour_now)
    subida = S.hf_historico(h, dias, asset0)
    m = margen(subida)
    gas_tx = S.GAS.get(h.pool["chain"], 0.05) if gas is None else gas
    out = []
    for rg in rangos:
        filas, base = [], None
        for c in coberturas:
            r = cubierta(h, paths, rg, c, capital, gas_tx, asset0, m, apr)
            fv = r["final"]
            base = fv if base is None else base
            fila = {"cobertura": c, "valor_media": float(np.mean(fv)), "valor_mediana": float(np.median(fv)),
                    "valor_p10": float(np.percentile(fv, 10)), "valor_p90": float(np.percentile(fv, 90)),
                    "vs_sin_cubrir": float(np.mean(fv - base)), "prob_perdida": float(np.mean(fv < capital)),
                    "en_pool": r["V"], "margen": r["M"], "corto": r["N"], "comisiones": float(np.mean(r["lp"].fees)),
                    "funding": float(np.mean(r["funding"])), "comisiones_perp": r["comisiones_perp"],
                    "prob_liquidacion": float(np.mean(r["liquidada"]))}
            if est_paths is not None:
                e = cubierta(h, est_paths, rg, c, capital, gas_tx, asset0, m, apr)
                fila["estres"] = [{"caso": inf["caso"], "resultado": float(e["final"][j] - capital),
                                   "liquidada": bool(e["liquidada"][j])} for j, inf in enumerate(est_info or [])]
            filas.append(fila)
        out.append({"texto": rg.texto, "filas": filas})
    return {"perpetuo": sym, "apr_funding": apr, "subida_historica": subida, "margen": m,
            "apalancamiento": 1 / m, "fuente": "Bybit", "rangos": out}
