"""Simulador de una posición de liquidez concentrada (rango fijo) HACIA EL FUTURO, con escenarios
construidos a partir del histórico real del pool en MetaProDB.

Cómo se construye cada escenario (bootstrap por bloques):
  1. Del histórico hora a hora del pool (últimos `historia` días) se sacan, para cada hora:
       - el rendimiento logarítmico del precio del pool y del precio en USD del token1,
       - lo que cobró cada unidad de liquidez en rango (fee_growth de la cadena), en forma
         invariante a la escala del precio (g0·√P y g1/√P): así una hora de comisiones de cuando
         ETH valía 3.000 $ se puede reutilizar con ETH a 1.700 $.
  2. Se eligen al azar bloques de `bloque` horas CONSECUTIVAS (por defecto 7 días) y se encadenan
     hasta el plazo. Los bloques mantienen juntos precio y comisiones (horas movidas = más volumen)
     y las rachas de volatilidad.
  3. Por defecto se quita la tendencia media del histórico (`tendencia="neutral"`): el simulador no
     apuesta por que el precio suba o baje, solo por cuánto se mueve.

En cada escenario se abre la posición HOY (último precio de MetaProDB) con la matemática de
Uniswap v3/v4 (la misma que `backtest.simulate`, rango ajustado hacia fuera a los ticks), se cobra
L × fee_growth en las horas en rango (la mitad si entra o sale dentro de la hora), diluido por la
propia liquidez frente a la activa del pool, y se valora al final del plazo.

Límites: supone que el futuro se parece al pasado reciente (volatilidad, volumen y liquidez del
pool); no modela MEV/JIT ni cambios bruscos de comisiones o de liquidez; con rangos muy estrechos
el reparto horario sobrestima. Es un análisis de escenarios, no una predicción.

    python -m etapas.simulador "WETH/USDC 0.05%" --red base --capital 5000 --dias 30 --rangos 1500-1900,±10%,-5/+15
    python -m etapas.simulador "WETH/USDC 0.05%" --red base --calibrar --dias 30 --ancho 10
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest as B

GAS = {"ethereum": 5.0}            # $ por transacción; L2: 0,05 (igual que el backtest)
PCTS = (5, 10, 25, 50, 75, 90, 95)
# Nivel de comisiones: los escenarios conservan la forma del histórico (semanas tranquilas y movidas)
# pero se reescalan al nivel de los últimos NIVEL_DIAS (media de comisiones por unidad de liquidez,
# invariante a la escala, frente a la media del histórico) y por un factor común AJUSTE_COMISIONES.
# Validado fuera de muestra (22-nov-2026, 23 pools, aperturas de dos años, factor calibrado con un
# año y aplicado al otro): sesgo 0,94–1,07 y 83–87 % de los pools dentro de ±20 %, frente a
# 0,86–1,05 y 52–70 % con un factor fijo de 0,85 sin nivel reciente. El factor por pool empeoraba.
NIVEL_DIAS = 90
# Días de histórico de los escenarios: con 730 la horquilla P10–P90 recoge mejor los años movidos
# (aperturas del último año: 68–77 % dentro frente a 64–75 % con 365) sin cambiar las comisiones.
HISTORIA_DIAS = 730
# Factor común a 30 días y corrección por plazo: comisiones × AJUSTE × (plazo / 30) ^ −EXPONENTE_PLAZO.
# Con un factor constante (0,88) las comisiones salían bajas a 7 días (real ÷ simulado 1,12–1,16) y
# altas a 90–180 días (0,64–0,79): el nivel reciente se arrastra todo el plazo. Validado el 22-nov-2026
# en 17 pools y dos años de aperturas (6.273), ajustando con un año y midiendo en el otro: el exponente
# sale 0,15 con cualquiera de los dos. El factor común óptimo con los dos años juntos es 0,82, pero el
# resultado apenas cambia entre 0,82 y 0,86; con 0,85 los dos años quedan en 0,87–1,04 de 7 a 180 días
# y el año más reciente a 30 días no se queda corto (calibración real de aperturas del último año:
# 7 d 0,93 · 30 d 1,09 · 90 d 0,96 · 180 d 0,88, frente a 1,12 · 1,05 · 0,79 · 0,64 con 0,88 fijo).
# Volver a la media del histórico empeoraba (las comisiones no rebotan) y prolongar su tendencia no
# aportaba. El 23-nov-2026, con la volatilidad reciente (abajo), se recuadraron juntos: 0,97 y 0,05.
AJUSTE_COMISIONES = 0.97
EXPONENTE_PLAZO = 0.05
# Volatilidad de los escenarios (23-nov-2026). Los movimientos del histórico se escalan por
#   (volatilidad de los últimos VOL_DIAS / la del histórico) × (plazo / 7) ^ EXPONENTE_VOL.
# Con el histórico tal cual, «vs mantener» salía optimista en años movidos y pesimista en los
# tranquilos (a 30 días, ±10 %: real − simulado −107 $ y +26 $ por 5.000 $; precio dentro del
# P10–P90 63 % y 82 %): la volatilidad va por rachas, y al encadenar semanas al azar se pierden las
# tendencias de varias semanas. Probadas 18 variantes (escala fija, bloques de 14 y 30 días,
# volatilidad de 30 o 90 días, ensanche por plazo) en 17 pools y 3.587 aperturas de dos años. La
# volatilidad reciente se mide con rendimientos de 4 horas (hora a hora, cada pool añade su ruido). Esta
# deja −5/−1 $ a 7 días, −30/+21 $ a 30 y −24/+155 $ a 90 (prudente en el año tranquilo), con el
# precio dentro del P10–P90 el 75/79 % a 30 días; la de 30 días tenía un error medio parecido pero
# peor cobertura. Al ensanchar, el precio pasa menos tiempo en rango: las comisiones se recuadraron
# con AJUSTE_COMISIONES 0,97 y EXPONENTE_PLAZO 0,05. El ensanche por plazo se limita a 90 días.
VOL_DIAS = 90
EXPONENTE_VOL = 0.1


def escala_vol(h: "History", dias: float, vol_dias: int | None = VOL_DIAS,
               exponente: float = EXPONENTE_VOL) -> float:
    """Factor por el que se amplían (o encogen) los movimientos del histórico en los escenarios."""
    reciente = h.vol_reciente(vol_dias) if vol_dias else 1.0
    return float(np.clip(reciente, 0.5, 2.0)) * (min(max(dias, 7), 90) / 7) ** exponente   # validado de 7 a 90 d


def factor_plazo(dias: float, exponente: float = EXPONENTE_PLAZO) -> float:
    """Corrección de las comisiones por plazo (1 a 30 días; validada entre 7 y 180)."""
    return (min(max(dias, 7), 365) / 30) ** -exponente


def historia_para(dias: int) -> int:
    """Días de histórico por defecto para un plazo. Medido el 22-nov-2026 (aperturas comunes, 17–20
    pools): 730 iguala o supera a 365 y a 1.095 en todos los plazos (P10–P90 a 7 d 83 % frente a 82 %;
    60 d 71 % frente a 68 %; 90 d 89 % frente a 85 %; 180 d 83 % frente a 78 %), así que no cambia."""
    return HISTORIA_DIAS


# --- histórico → rasgos por hora ----------------------------------------------------------------

@dataclass
class History:
    pool: dict
    df: pd.DataFrame
    ret_p: np.ndarray        # log(P[i+1]/P[i]) del precio del pool (token1 por token0, unidades mínimas)
    ret_u1: np.ndarray       # log(usd1[i+1]/usd1[i])
    q0: np.ndarray           # fee_growth0 × √P[i+1]  (invariante a la escala)
    q1: np.ndarray           # fee_growth1 / √P[i+1]
    sp_now: float
    usd1_now: float
    liquidity_now: float     # mediana de la liquidez activa de los últimos 7 días
    swap_fee: float          # comisión del pool (fracción) para el cambio al abrir
    hour_now: pd.Timestamp
    tvl_now: float = 0.0     # TVL de la última hora con dato ($)

    def vol_reciente(self, dias: int = 90, paso: int = 4) -> float:
        """Volatilidad de los últimos `dias` frente a la de todo el histórico, con rendimientos de `paso`
        horas: hora a hora, cada pool añade su propio ruido de precio (en Ethereum, ×1,00 frente a ×0,80
        en Base con el mismo ETH); a 4 horas todos coinciden."""
        lp = np.log(self.df["sqrt_price"].to_numpy(float) ** 2)[::-1][::paso][::-1]   # anclado en la última hora
        r = np.diff(lp)
        n = int(dias * 24 / paso)
        tot = float(np.std(r))
        return float(np.std(r[-n:])) / tot if len(r) > n >= 6 and tot > 0 else 1.0

    @property
    def dias_historia(self) -> float:
        return (self.df["hour"].iloc[-1] - self.df["hour"].iloc[0]) / pd.Timedelta(days=1)

    def nivel(self, dias: int | None = NIVEL_DIAS) -> float:
        """Comisiones por unidad de liquidez de los últimos `dias` frente a la media del histórico."""
        y = self.q0 + self.q1
        if not dias or not len(y) or y.mean() <= 0:
            return 1.0
        hrs = self.df["hour"].iloc[:-1].to_numpy()
        recent = y[hrs > hrs[-1] - np.timedelta64(dias, "D")]
        return float(recent.mean() / y.mean()) if len(recent) else 1.0


def features(pool: dict, df: pd.DataFrame) -> History:
    sp = df["sqrt_price"].to_numpy(float)
    u1 = df["usd1"].to_numpy(float)
    g0 = df["fee_growth0"].to_numpy(float)[:-1]
    g1 = df["fee_growth1"].to_numpy(float)[:-1]
    liq = df["liquidity"].to_numpy(float) if "liquidity" in df else np.full(len(df), np.nan)
    recent = liq[-24 * 7:]
    recent = recent[np.isfinite(recent) & (recent > 0)]
    lp_fee = df["lp_fee"].dropna()
    fee = (float(lp_fee.iloc[-1]) if len(lp_fee) else pool["fee"]) / 1e6
    if fee > 0.1:                                  # comisión dinámica sin dato: la del pool nominal no sirve
        fee = 0.003
    tvl = df["tvl_usd"].dropna() if "tvl_usd" in df else pd.Series(dtype=float)
    return History(pool, df, 2 * np.log(sp[1:] / sp[:-1]), np.log(u1[1:] / u1[:-1]),
                   g0 * sp[1:], g1 / sp[1:], float(sp[-1]), float(u1[-1]),
                   float(np.median(recent)) if len(recent) else 0.0, fee, df["hour"].iloc[-1],
                   float(tvl.iloc[-1]) if len(tvl) else 0.0)


def load(key: str, chain: str | None = None, historia: int = HISTORIA_DIAS, hasta: str | None = None) -> History:
    end = pd.Timestamp(hasta) if hasta else None
    start = ((end or pd.Timestamp.now(tz=None).floor("h")) - pd.Timedelta(days=historia)).strftime("%Y-%m-%d %H:00")
    pool, df = B.load_hours(key, chain, start, end.strftime("%Y-%m-%d %H:00") if end is not None else None)
    return features(pool, df)


# --- escenarios -------------------------------------------------------------------------------

@dataclass
class Paths:
    sp: np.ndarray           # (S, H+1) √precio del pool en unidades mínimas
    usd1: np.ndarray         # (S, H+1)
    q0: np.ndarray           # (S, H)
    q1: np.ndarray           # (S, H)


def bootstrap(h: History, horas: int, escenarios: int = 1000, bloque: int = 168,
              tendencia: str = "neutral", seed: int | None = 7, ajuste: float = AJUSTE_COMISIONES,
              nivel_dias: int | None = NIVEL_DIAS, exponente: float = EXPONENTE_PLAZO,
              escala: float | None = None) -> Paths:
    """`escala` None = la automática (`escala_vol`); 1 = los movimientos del histórico tal cual."""
    m = len(h.ret_p)
    if m < 48:
        raise B.BacktestError("histórico demasiado corto para construir escenarios")
    bloque = max(1, min(bloque, m))
    rng = np.random.default_rng(seed)
    nb = math.ceil(horas / bloque)
    starts = rng.integers(0, m - bloque + 1, size=(escenarios, nb))
    idx = (starts[:, :, None] + np.arange(bloque)[None, None, :]).reshape(escenarios, -1)[:, :horas]
    rp, ru = h.ret_p[idx], h.ret_u1[idx]
    escala = escala_vol(h, horas / 24) if escala is None else escala
    mp, mu = h.ret_p.mean(), h.ret_u1.mean()
    keep = 0.0 if tendencia == "neutral" else 1.0           # neutral: sin la tendencia media del histórico
    rp = keep * mp + escala * (rp - mp)                     # escala: amplía los movimientos alrededor de ella
    ru = keep * mu + escala * (ru - mu)
    zero = np.zeros((escenarios, 1))
    sp = h.sp_now * np.exp(np.concatenate([zero, np.cumsum(rp, axis=1)], axis=1) / 2)
    u1 = h.usd1_now * np.exp(np.concatenate([zero, np.cumsum(ru, axis=1)], axis=1))
    k = ajuste * h.nivel(nivel_dias) * factor_plazo(horas / 24, exponente)
    return Paths(sp, u1, h.q0[idx] * k, h.q1[idx] * k)


def replay(h: History) -> Paths:
    """El histórico tal cual, como un único escenario (para la paridad con el backtest y la calibración)."""
    sp = h.df["sqrt_price"].to_numpy(float)[None, :]
    u1 = h.df["usd1"].to_numpy(float)[None, :]
    return Paths(sp, u1, h.q0[None, :], h.q1[None, :])


# --- rangos -----------------------------------------------------------------------------------

@dataclass
class Rango:
    texto: str               # lo que escribió el usuario
    tl: int
    tu: int
    lo: float                # precios mostrados (cotización por activo), ya ajustados a ticks
    hi: float

    @property
    def nombre(self) -> str:
        return f"Fijo {_fmt_price(self.lo)}–{_fmt_price(self.hi)}"


def _fmt_price(x: float) -> str:
    d = 2 if x >= 1 else 6
    return f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def price_now(h: History) -> float:
    return _display(h.pool, h.sp_now ** 2)


def _display(pool: dict, p_raw: float) -> float:
    """Precio mostrado (cotización por activo, como /pool) a partir del precio en unidades mínimas."""
    human = p_raw * 10 ** (pool["dec0"] - pool["dec1"])            # token1 por token0
    _, _, asset0 = B.pair_of(pool)
    return human if asset0 else 1 / human


def _raw(pool: dict, shown: float) -> float:
    _, _, asset0 = B.pair_of(pool)
    human = shown if asset0 else 1 / shown
    return human / 10 ** (pool["dec0"] - pool["dec1"])


_PCT_RANGO = re.compile(r"^P(\d{1,2})[-–/]P(\d{1,2})$", re.I)


def percentiles_finales(h: History, paths: Paths, a: float, b: float) -> tuple[float, float]:
    """Precios mostrados en los percentiles `a` y `b` del precio al final del plazo de los escenarios."""
    shown = _display_arr(h.pool, paths.sp[:, -1] ** 2)
    return float(np.percentile(shown, a)), float(np.percentile(shown, b))


def rango_de_ticks(h: History, tl: int, tu: int, texto: str | None = None) -> Rango:
    """El rango de una posición que ya existe, tal cual (sus ticks), en precios mostrados."""
    lo, hi = sorted(_display(h.pool, B.TICK_BASE ** t) for t in (tl, tu))
    return Rango(texto or f"{_fmt_price(lo)}-{_fmt_price(hi)}", tl, tu, lo, hi)


def parse_rango(texto: str, h: History, paths: Paths | None = None) -> Rango:
    """«1500-1900» (precios), «±10%» o «10%» (simétrico), «-5/+15» (% abajo / % arriba) o
    «P10-P90» (entre esos percentiles del precio al final del plazo; necesita los escenarios)."""
    t = texto.strip().replace(" ", "").replace(",", ".").replace("%", "").replace("±", "")
    p = price_now(h)
    m = _PCT_RANGO.match(t)
    if m:
        a, b = sorted((int(m.group(1)), int(m.group(2))))
        if paths is None or not 0 < a < b < 100:
            raise ValueError(f"rango no válido: «{texto}» (percentiles entre 1 y 99, p. ej. P10-P90)")
        lo, hi = percentiles_finales(h, paths, a, b)
    elif "/" in t:
        a, b = t.split("/")
        lo, hi = p * (1 - abs(float(a)) / 100), p * (1 + abs(float(b)) / 100)
    elif "-" in t[1:]:
        a, b = t.split("-", 1)
        lo, hi = sorted((float(a), float(b)))
    else:
        w = float(t) / 100
        lo, hi = p * (1 - w), p * (1 + w)
    if not (0 < lo < hi):
        raise ValueError(f"rango no válido: «{texto}»")
    spacing = h.pool["tick_spacing"]
    ta, tb = sorted(math.log(_raw(h.pool, x)) / math.log(B.TICK_BASE) for x in (lo, hi))
    tl = max(math.floor(ta / spacing) * spacing, math.ceil(B.MIN_TICK / spacing) * spacing)
    tu = min(math.ceil(tb / spacing) * spacing, math.floor(B.MAX_TICK / spacing) * spacing)
    tu = max(tu, tl + spacing)
    shown = sorted(_display(h.pool, B.TICK_BASE ** t) for t in (tl, tu))
    return Rango(texto, tl, tu, shown[0], shown[1])


# --- simulación vectorizada -------------------------------------------------------------------

@dataclass
class Sim:
    """Resultado por escenario (arrays de tamaño S) de un rango."""
    rango: Rango
    capital: float
    dias: float
    costs: float
    final: np.ndarray
    fees: np.ndarray
    hodl: np.ndarray
    in_range: np.ndarray
    side: np.ndarray         # -1 acaba por debajo, 0 dentro, +1 por encima (en precio mostrado)
    dilution: float
    precio0: float = 0.0     # precio mostrado al abrir


def simulate(h: History, paths: Paths, rango: Rango, capital: float = 5000.0, gas: float | None = None,
             diluir: bool = True, abierta: bool = False) -> Sim:
    """`abierta`: la posición ya existe (vale `capital` hoy): sin gas ni cambio inicial."""
    pool = h.pool
    d1 = 10 ** pool["dec1"]
    gas = 0.0 if abierta else GAS.get(pool["chain"], 0.05) if gas is None else gas
    sa, sb = B._sqrt(rango.tl), B._sqrt(rango.tu)
    sp, u1 = paths.sp, paths.usd1
    sp0, u10 = float(sp[0, 0]), float(u1[0, 0])

    def val(a0, a1, s, u):                     # USD de (a0, a1) en unidades mínimas
        return (a0 * s ** 2 + a1) * u / d1

    a0u, a1u = B._amounts(sp0, sa, sb)         # por unidad de liquidez
    per_l = val(a0u, a1u, sp0, u10)
    share0 = val(a0u, 0, sp0, u10) / per_l
    stable0, stable1 = pool["symbol0"] in B._STABLE, pool["symbol1"] in B._STABLE
    value = capital - gas
    swap = 0.0 if abierta else value * h.swap_fee * ((1 - share0) if stable0 else share0 if stable1 else 0.5)
    costs = gas + swap
    L = (value - swap) / per_l
    a0, a1 = a0u * L, a1u * L

    dil = h.liquidity_now / (h.liquidity_now + L) if (diluir and h.liquidity_now > 0) else 1.0
    inside = (sp >= sa) & (sp < sb)
    part = (inside[:, :-1].astype(float) + inside[:, 1:]) / 2
    nxt = sp[:, 1:]
    f0h = L * dil * paths.q0 / nxt * part      # token0 cobrado cada hora
    f1h = L * dil * paths.q1 * nxt * part
    fees = (val(f0h, f1h, nxt, u1[:, 1:])).sum(axis=1)
    f0, f1 = f0h.sum(axis=1), f1h.sum(axis=1)

    se, ue = sp[:, -1], u1[:, -1]
    p0 = np.where(se <= sa, 1 / sa - 1 / sb, np.where(se >= sb, 0.0, 1 / se - 1 / sb)) * L
    p1 = np.where(se <= sa, 0.0, np.where(se >= sb, sb - sa, se - sa)) * L
    final = val(p0 + f0, p1 + f1, se, ue)
    hodl = val(a0, a1, se, ue) + costs
    _, _, asset0 = B.pair_of(pool)
    side = np.where(se < sa, -1, np.where(se >= sb, 1, 0)) * (1 if asset0 else -1)
    horas = sp.shape[1] - 1
    return Sim(rango, capital, horas / 24, costs, final, fees, hodl, part.mean(axis=1), side, dil,
               _display(pool, sp0 ** 2))


def resumen(s: Sim) -> dict:
    q = lambda a: {f"p{p}": float(np.percentile(a, p)) for p in PCTS}
    rent = s.final / s.capital - 1
    ann = 365 / max(s.dias, 1e-9)
    med_fees = float(np.median(s.fees))
    return {
        "estrategia": s.rango.nombre, "texto": s.rango.texto, "lo": s.rango.lo, "hi": s.rango.hi,
        "invertido": s.capital, "costes_apertura": s.costs, "dilucion": s.dilution,
        "fuera_hoy": not (s.rango.lo <= s.precio0 <= s.rango.hi),
        "valor_final": q(s.final), "valor_media": float(np.mean(s.final)),
        "vs_mantener": q(s.final - s.hodl), "comisiones": q(s.fees),
        "apr": med_fees / s.capital * ann, "rent": q(rent), "rent_anual": float(np.median(rent)) * ann,
        "en_rango": float(np.mean(s.in_range)), "en_rango_p": q(s.in_range),
        "prob_perdida": float(np.mean(s.final < s.capital)),
        "prob_gana_mantener": float(np.mean(s.final > s.hodl)),
        "acaba": {"dentro": float(np.mean(s.side == 0)), "debajo": float(np.mean(s.side < 0)),
                  "encima": float(np.mean(s.side > 0))},
        "desglose": desglose(s),
    }


def desglose(s: Sim) -> dict:
    """De dónde sale el resultado (media de los escenarios, en $; suma exacta):
    precio = lo que cambian de valor los tokens depositados; perdida_impermanente = lo que la posición
    vale menos que esos mismos tokens guardados; comisiones; costes = gas + cambio inicial.
    «vs mantener» = perdida_impermanente + comisiones + costes."""
    tokens = s.hodl - s.costs                              # los tokens depositados, guardados, al final
    return {"precio": float(np.mean(tokens - (s.capital - s.costs))),
            "perdida_impermanente": float(np.mean(s.final - s.fees - tokens)),
            "comisiones": float(np.mean(s.fees)), "costes": -float(s.costs),
            "total": float(np.mean(s.final - s.capital))}


def abanico(h: History, paths: Paths, puntos: int = 60) -> dict:
    """Bandas de percentiles del precio mostrado a lo largo del plazo (para el gráfico)."""
    H = paths.sp.shape[1] - 1
    cols = np.unique(np.linspace(0, H, min(puntos, H + 1)).round().astype(int))
    raw = paths.sp[:, cols] ** 2
    shown = np.vectorize(lambda x: _display(h.pool, x))(raw) if raw.size < 5000 else _display_arr(h.pool, raw)
    return {"horas": cols.tolist(), **{f"p{p}": np.percentile(shown, p, axis=0).tolist() for p in PCTS}}


def _display_arr(pool: dict, raw: np.ndarray) -> np.ndarray:
    human = raw * 10 ** (pool["dec0"] - pool["dec1"])
    _, _, asset0 = B.pair_of(pool)
    return human if asset0 else 1 / human


def referencia_sin_riesgo(chain: str) -> dict | None:
    """Rentabilidad de hoy de USDC en Aave en la misma red (MetaProDB), como referencia en dólares."""
    try:
        con = B._connect()
        try:
            row = con.execute("""SELECT arg_max(supply_apy, hour), max(hour) FROM aave_reserve_hour_v
                                 WHERE chain = ? AND market = ? AND symbol = 'USDC'""", [chain, chain]).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row or row[0] is None:
        return None
    return {"nombre": f"USDC en Aave {chain}", "apy": float(row[0]), "hora": str(row[1])}


# --- buscador de rango ------------------------------------------------------------------------

ANCHOS = (0.001, 0.002, 0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20,
          0.25, 0.30, 0.40, 0.50, 0.70)
CRITERIOS = {"media": "vs_media", "mediana": "vs_mediana"}


def ancho_minimo(h: History, k: float = 3.0) -> float:
    """Ancho mínimo fiable: k × la volatilidad diaria del pool (±9 % en ETH/USDC, ±0,1 % en estables).
    Validado el 23-nov-2026 (15 pools, 3.587 aperturas de dos años, a 7, 30 y 90 días): con datos hora a
    hora el simulador sobrestima los rangos estrechos (real − simulado, «vs mantener» medio a 30 días:
    −70 a −134 $ por 5.000 $ con ±0,1–2 % frente a −14 a −35 $ con ±30–70 %); sin este mínimo el buscador
    elegía ±0,1 % y en la realidad perdía más; con él, su orden coincide con el real."""
    return max(ANCHOS[0], k * float(np.std(h.ret_p)) * math.sqrt(24))


def barrido(h: History, paths: Paths, capital: float = 5000, gas: float | None = None,
            anchos: tuple[float, ...] = ANCHOS, min_ancho: float | None = None) -> list[dict]:
    """Rangos simétricos de `ancho_minimo` a ±70 % con los mismos escenarios. Los que caen en los mismos
    ticks se cuentan una vez. «vs mantener» = valor final − los mismos tokens guardados (incluye costes)."""
    mn = ancho_minimo(h) if min_ancho is None else min_ancho
    anchos = [w for w in anchos if w >= mn] or [anchos[-1]]
    seen, out = set(), []
    for w in anchos:
        rg = parse_rango(f"{w * 100:g}", h)
        if (rg.tl, rg.tu) in seen:
            continue
        seen.add((rg.tl, rg.tu))
        s = simulate(h, paths, rg, capital, gas)
        vm = s.final - s.hodl
        out.append({"texto": f"±{w * 100:g}%", "ancho": w, "lo": rg.lo, "hi": rg.hi,
                    "vs_media": float(vm.mean()), "vs_mediana": float(np.median(vm)),
                    "vs_p10": float(np.percentile(vm, 10)), "comisiones": float(s.fees.mean()),
                    "en_rango": float(s.in_range.mean()), "valor_media": float(s.final.mean()),
                    "valor_mediana": float(np.median(s.final)), "valor_p10": float(np.percentile(s.final, 10))})
    return out


def mejor_rango(curva: list[dict], criterio: str = "media") -> dict:
    """El ancho con más «vs mantener»: en media (cuenta los escenarios malos con su peso) o en mediana."""
    return max(curva, key=lambda r: r[CRITERIOS[criterio]])


def optimizar(key: str, chain: str | None, capital: float = 5000, dias: int = 30, historia: int = HISTORIA_DIAS,
              escenarios: int = 1000, bloque: int = 168, tendencia: str = "neutral", gas: float | None = None,
              h: History | None = None, ajuste: float = AJUSTE_COMISIONES, nivel_dias: int | None = NIVEL_DIAS,
              exponente: float = EXPONENTE_PLAZO, criterio: str = "media") -> dict:
    h = h or load(key, chain, historia)
    paths = bootstrap(h, dias * 24, escenarios, bloque, tendencia, ajuste=ajuste, nivel_dias=nivel_dias,
                      exponente=exponente)
    curva = barrido(h, paths, capital, gas)
    best = mejor_rango(curva, criterio)
    return {"pool": {k: h.pool[k] for k in ("chain", "version", "label", "pool_id")}, "dias": dias,
            "capital": capital, "criterio": criterio, "curva": curva, "ancho_minimo": ancho_minimo(h),
            "mejor": best, "mejor_media": mejor_rango(curva, "media"),
            "mejor_mediana": mejor_rango(curva, "mediana"), "en_el_borde": best is curva[-1]}


# --- comparar el mismo par en varios pools ----------------------------------------------------

def par_de(pool: dict) -> tuple[str, str]:
    """Par comparable: ETH = WETH, BTC = WBTC = cbBTC, y cualquier estable cuenta como USD."""
    asset, quote, _ = B.pair_of(pool)
    estables = {s.upper() for s in B._STABLE}
    return asset, ("USD" if quote.upper() in estables else quote)


def pools_recientes() -> list[dict]:
    """Pools con datos en los últimos 3 días, con símbolos, comisión y TVL de la última hora."""
    con = B._connect()
    try:
        rows = con.execute("""
            WITH last AS (SELECT chain, pool_id, max(hour) h FROM pool_hour_v
                          WHERE hour > now() - INTERVAL 3 DAY AND usd0 IS NOT NULL GROUP BY 1, 2)
            SELECT p.chain, p.version, p.pool_id, p.label, p.fee, p.tick_spacing, t0.symbol, t1.symbol,
                   t0.decimals, t1.decimals, v.tvl_usd
            FROM pools p JOIN last USING (chain, pool_id)
            JOIN pool_hour_v v ON v.chain = p.chain AND v.pool_id = p.pool_id AND v.hour = last.h
            JOIN tokens t0 ON t0.chain = p.chain AND t0.address = p.token0
            JOIN tokens t1 ON t1.chain = p.chain AND t1.address = p.token1
            ORDER BY v.tvl_usd DESC NULLS LAST""").fetchall()
    finally:
        con.close()
    keys = ("chain", "version", "pool_id", "label", "fee", "tick_spacing", "symbol0", "symbol1", "dec0", "dec1", "tvl")
    return [dict(zip(keys, r)) for r in rows]


def comparar(pool_id: str, chain: str, rango: str = "P10-P90", capital: float = 5000, dias: int = 30,
             historia: int = HISTORIA_DIAS, escenarios: int = 500, tendencia: str = "neutral",
             min_tvl: float = 100_000, cargar=None, candidatos: list[dict] | None = None) -> dict:
    """La misma estrategia (y el mejor ancho de cada uno) en todos los pools del mismo par con TVL ≥ min_tvl.
    `cargar(pool_id, chain, historia)` permite reutilizar históricos ya leídos."""
    cargar = cargar or (lambda p, c, hh: load(p, c, hh))
    todos = candidatos if candidatos is not None else pools_recientes()
    base = next((p for p in todos if p["pool_id"] == pool_id and p["chain"] == chain), None)
    if base is None:
        raise ValueError("ese pool no tiene datos recientes")
    par = par_de(base)
    grupo = [p for p in todos if par_de(p) == par and ((p["tvl"] or 0) >= min_tvl or p is base)]
    filas = []
    for p in grupo:
        try:
            h = cargar(p["pool_id"], p["chain"], historia)
            paths = bootstrap(h, dias * 24, escenarios, tendencia=tendencia)
            r = resumen(simulate(h, paths, parse_rango(rango, h, paths), capital))
            best = mejor_rango(barrido(h, paths, capital), "media")
            g = r["desglose"]
            filas.append({"chain": p["chain"], "version": p["version"], "pool_id": p["pool_id"], "label": p["label"],
                          "tvl": p["tvl"], "es_este": p is base,
                          "fiabilidad": fiabilidad(h, dias, calibracion_pool(p["pool_id"], p["chain"], dias))["nivel"],
                          "rango": {"lo": r["lo"], "hi": r["hi"], "comisiones": r["comisiones"]["p50"], "apr": r["apr"],
                                    "vs_mediana": r["vs_mantener"]["p50"], "vs_media": g["total"] - g["precio"],
                                    "valor_p50": r["valor_final"]["p50"], "valor_p10": r["valor_final"]["p10"],
                                    "en_rango": r["en_rango"]},
                          "mejor": {"texto": best["texto"], "vs_media": best["vs_media"], "vs_mediana": best["vs_mediana"]}})
        except Exception as e:                                   # noqa: BLE001 (un pool no tumba la tabla)
            filas.append({"chain": p["chain"], "version": p["version"], "pool_id": p["pool_id"], "label": p["label"],
                          "tvl": p["tvl"], "es_este": p is base, "error": f"{type(e).__name__}: {e}"})
    filas.sort(key=lambda f: -f["rango"]["vs_media"] if "rango" in f else float("inf"))
    return {"par": f"{par[0]}/{par[1]}", "rango": rango, "dias": dias, "capital": capital, "pools": filas}


# --- fiabilidad y conclusión ------------------------------------------------------------------

CALIBRACION_LOG = Path(__file__).resolve().parent.parent / "out" / "simulador_calibracion.jsonl"
_NIVELES = ("alta", "media", "baja")


def calibracion_pool(pool_id: str, chain: str, dias: int, log: Path | None = None) -> dict | None:
    """Resultado de este pool en la última calibración mensual, al plazo más parecido (o None)."""
    log = log or CALIBRACION_LOG
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if "plazos" not in rec:
            continue
        found = [(p["dias"], x) for p in rec["plazos"] for x in p["pools"]
                 if x.get("pool_id") == pool_id and x.get("red") == chain]
        if not found:
            return None
        d, x = min(found, key=lambda dx: abs(math.log(dx[0] / max(dias, 1))))
        return {"dias": d, "comisiones": x["comisiones"], "cobertura": x["cobertura"],
                "pruebas": x["pruebas"], "fecha": rec.get("fecha")}
    return None


def fiabilidad(h: History, dias: int, calib: dict | None = None) -> dict:
    """Semáforo: alta / media / baja, con los motivos. Se basa en lo validado (pools con ≥ 2 años y
    TVL > 2 M$; 7–180 días) y, si existe, en la calibración mensual del propio pool."""
    from .simulador_aviso import ANOMALOS, MIN_TVL
    malos: list[tuple[int, str]] = []          # (0 media / 1 baja, motivo)
    buenos: list[str] = []
    pool = h.pool
    if (pool["chain"], pool["pool_id"]) in ANOMALOS:
        malos.append((1, "MetaProDB tiene comisiones anómalas en este pool: el simulador no las reproduce"))
    hist = h.dias_historia
    if hist < 180:
        malos.append((1, f"solo {hist:.0f} días de histórico"))
    elif hist < HISTORIA_DIAS * 0.9:
        malos.append((0, f"{hist:.0f} días de histórico (lo validado es {HISTORIA_DIAS})"))
    if h.tvl_now < MIN_TVL / 4:
        malos.append((1, f"TVL de {h.tvl_now / 1e6:.2f} M$: comisiones muy ruidosas"))
    elif h.tvl_now < MIN_TVL:
        malos.append((0, f"TVL de {h.tvl_now / 1e6:.1f} M$ (lo validado es > {MIN_TVL / 1e6:.0f} M$)"))
    if dias > 90:
        malos.append((0, f"a {dias} días el error por pool es mayor (en comisiones, la mitad de los pools se "
                         "desvía más de un 20 %)"))
    if calib:
        r, c = calib["comisiones"], calib["cobertura"]
        txt = (f"en su calibración a {calib['dias']} días lo real fue ×{r:.2f} de las comisiones simuladas "
               f"y cayó dentro del P10–P90 el {_pct(c)}").replace(".", ",")
        if not 0.5 <= r <= 2 or c < 0.4:
            malos.append((1, txt))
        elif not 0.75 <= r <= 1.33 or c < 0.6:
            malos.append((0, txt))
        else:
            buenos.append(txt)
    elif not malos:
        buenos.append("pool como los validados (≥ 2 años de histórico, TVL > 2 M$)")
    nivel = _NIVELES[max((m for m, _ in malos), default=-1) + 1]
    return {"nivel": nivel, "motivos": [t for _, t in malos], "a_favor": buenos}


def _pct(x: float) -> str:
    return f"{x * 100:.0f} %"


def _usd(x: float, signo: bool = True) -> str:
    return (f"{x:+,.0f}" if signo else f"{x:,.0f}").replace(",", ".").replace("-", "−") + " $"


def conclusiones(res: dict) -> list[str]:
    """Tres o cuatro frases que resumen la tabla."""
    rs = res["rangos"]
    if not rs:
        return []
    out = []
    best = max(rs, key=lambda r: r["vs_mantener"]["p50"])
    vm = best["vs_mantener"]["p50"]
    if vm > 0:
        out.append(f"Frente a mantener los tokens, el mejor es {best['texto']}: {_usd(vm)} en la mediana, "
                   f"y gana a mantener en el {_pct(best['prob_gana_mantener'])} de los escenarios.")
    else:
        out.append(f"Ningún rango supera a mantener los tokens en la mediana (el mejor, {best['texto']}: "
                   f"{_usd(vm)}): las comisiones no compensan lo que se pierde por el movimiento del precio.")
    top = max(rs, key=lambda r: r["valor_final"]["p50"])
    gana = top["valor_final"]["p50"] - res["capital"]
    ref = res.get("sin_riesgo")
    if ref:
        sr = ref["valor_final"] - res["capital"]
        if sr >= gana:
            out.append(f"{ref['nombre']} da {_usd(sr)} en {res['dias']} días sin riesgo de precio, más que la "
                       f"mediana de cualquier rango (la mejor, {top['texto']}: {_usd(gana)}).")
        else:
            out.append(f"{top['texto']} supera en la mediana a {ref['nombre']} ({_usd(gana)} frente a {_usd(sr)}), "
                       f"pero en el escenario malo (P10) acaba en {_usd(top['valor_final']['p10'], False)} y pierde "
                       f"dinero en el {_pct(top['prob_perdida'])} de los escenarios.")
    cola = [r for r in rs if "desglose" in r and r["desglose"]["total"] < 0 < r["valor_final"]["p50"] - res["capital"]]
    if cola:
        r = min(cola, key=lambda r: r["desglose"]["total"])
        out.append(f"Ojo con {r['texto']}: la mediana gana {_usd(r['valor_final']['p50'] - res['capital'])}, pero la "
                   f"media de los escenarios pierde {_usd(-r['desglose']['total'], False)}: cuando sale mal, sale muy mal "
                   f"(pérdida impermanente media {_usd(r['desglose']['perdida_impermanente'])}).")
    cortos = [r["texto"] for r in rs if r["en_rango"] < 0.6]
    if cortos:
        out.append(f"{', '.join(cortos)}: dentro menos del 60 % del tiempo, estrecho para {res['dias']} días.")
    f = res.get("fiabilidad")
    if f and f["nivel"] != "alta":
        out.append(f"Fiabilidad {f['nivel']}: {'; '.join(f['motivos'])}.")
    return out


def recomendacion(res: dict) -> dict:
    """La lectura para quien no quiere interpretar la tabla: veredicto, qué hacer, qué no hacer y cómo
    intentar mejorarlo. Se basa en la MEDIA de los escenarios (cuenta los malos con su peso), comparada con
    guardar los tokens y con la referencia sin riesgo. Es análisis de escenarios, no una orden."""
    rs, cap, dias = res["rangos"], res["capital"], res["dias"]
    if not rs:
        return {}
    vm = lambda r: r["desglose"]["total"] - r["desglose"]["precio"]      # vs mantener, media
    gana = lambda r: r["valor_media"] - cap                              # resultado medio en $
    ref = res.get("sin_riesgo")
    sr = ref["valor_final"] - cap if ref else 0.0
    best = max(rs, key=vm)
    f = res.get("fiabilidad") or {"nivel": "alta", "motivos": []}
    asset = (res.get("par") or "/").split("/")[0]
    hacer, no_hacer, mejorar = [], [], []
    buenos = [r for r in rs if vm(r) > 0 and gana(r) > sr]

    if buenos:
        b = max(buenos, key=gana)
        perdida_mala = cap - b["valor_final"]["p10"]
        veredicto = ("favorable",
                     f"Con estos números, abrir el rango {b['texto']} tiene sentido: de media gana {_usd(vm(b))} frente a "
                     f"guardar los tokens y {_usd(gana(b))} en total en {dias} días"
                     + (f", más que los {_usd(sr)} sin riesgo de {ref['nombre']}." if ref else "."))
        hacer.append(f"Abre {b['texto']} ({_fmt_price(b['lo'])}–{_fmt_price(b['hi'])}) solo con dinero que puedas "
                     f"permitirte ver caer: en un escenario malo (1 de cada 10) pierdes unos {_usd(perdida_mala, False)}.")
        hacer.append("Vigílala: apúntala en posiciones.txt para que la rutina te avise si se acerca a un borde, y "
                     "revísala a mitad de plazo con «Mis posiciones».")
    elif any(r["valor_final"]["p50"] > cap for r in rs):
        t = max(rs, key=lambda r: r["valor_final"]["p50"])
        veredicto = ("desfavorable",
                     f"Con estos números no abriría ninguno de estos rangos. Lo normal es ganar un poco (con {t['texto']}, "
                     f"{_usd(t['valor_final']['p50'] - cap)} en el escenario de en medio), pero cuando el precio se mueve "
                     f"mucho se pierde bastante más (unos {_usd(cap - t['valor_final']['p10'], False)} en un escenario malo, "
                     "1 de cada 10) "
                     f"y, de media, acabas perdiendo {_usd(-gana(t), False)}.")
    else:
        veredicto = ("desfavorable", "Con estos números no abriría ninguno de estos rangos: pierden tanto en el caso típico "
                                     "como de media.")

    if not buenos:
        hacer.append(f"Si lo que quieres es tener {asset}, guárdalo sin más: el pool no te paga lo suficiente por el "
                     f"riesgo (frente a guardar los tokens, el mejor rango da {_usd(vm(best))} de media).")
        if ref:
            hacer.append(f"Si lo que buscas es renta en dólares sin riesgo de precio: {ref['nombre']} da {_usd(sr)} en "
                         f"{dias} días con {_usd(cap, False)}.")
        if vm(best) > -0.01 * cap:
            hacer.append(f"Si aun así quieres probar, usa el rango menos malo, {best['texto']} ({_usd(gana(best))} de media), "
                         "con poco dinero y vigilándolo.")

    for r in rs:
        if r["en_rango"] < 0.6:
            no_hacer.append(f"No uses {r['texto']}: está fuera de rango el {_pct(1 - r['en_rango'])} del tiempo (sin cobrar) "
                            f"y es de los que más pierden cuando el precio se escapa ({_usd(gana(r))} de media).")
    peor = min(rs, key=gana)
    if peor["comisiones"]["p50"] == max(r["comisiones"]["p50"] for r in rs) and len(rs) > 1 and gana(peor) < 0:
        no_hacer.append(f"No elijas por el APR: {peor['texto']} tiene el APR más alto ({_pct(peor['apr'])}) y, aun así, "
                        f"el peor resultado medio ({_usd(gana(peor))}).")
    no_hacer.append("No te fíes solo de la columna «Valor final»: es el caso típico. Mira también «Valor final (media)» "
                    "y el P10: ahí se ve lo que pasa cuando sale mal.")
    if f["nivel"] != "alta":
        no_hacer.append(f"No tomes las cifras al pie de la letra: fiabilidad {f['nivel']} ({'; '.join(f['motivos'])}).")
    if any(r.get("fuera_hoy") for r in rs):
        no_hacer.append("No abras un rango que ya está fuera hoy pensando que cobrará: no cobra nada hasta que el precio entre.")

    mb = res.get("mejor_ancho")
    if mb:
        if mb["vs_media"] > max(vm(r) for r in rs) and not (mb["es_el_mas_ancho"] and mb["vs_media"] <= 0):
            mejorar.append(f"Prueba {mb['texto']}: el buscador de anchos le da {_usd(mb['vs_media'])} de media frente a "
                           "guardar los tokens, mejor que tus rangos.")
        elif mb["es_el_mas_ancho"] and mb["vs_media"] <= 0:
            mejorar.append(f"Ni el rango más ancho que prueba el buscador ({mb['texto']}) gana de media "
                           f"({_usd(mb['vs_media'])}): en este pool, ahora, ningún rango compensa.")
    mejorar.append("Compara el mismo par en otros pools (botón «Comparar», justo encima): con el mismo precio, el que cobra "
                   "más comisiones por la misma liquidez puede darle la vuelta.")
    nivel = res.get("nivel_reciente") or 1.0
    if nivel < 0.8:
        mejorar.append(f"Las comisiones de este pool están ahora al {_pct(nivel)} de su media de dos años: vuelve a "
                       "mirarlo cuando suban (el resultado mejora casi en proporción).")
    if dias >= 60:
        mejorar.append("Prueba un plazo de 30 días: el simulador es más fiable y te deja revisar y decidir antes.")
    if (res.get("escala_vol") or 1.0) < 0.95:
        mejorar.append("Los últimos meses han sido tranquilos y los escenarios lo reflejan: si vuelve la volatilidad, el "
                       "pool saldrá peor que esto. Vuelve a simular antes de abrir.")
    return {"veredicto": veredicto[0], "texto": veredicto[1], "hacer": hacer, "no_hacer": no_hacer, "mejorar": mejorar,
            "nota": "Análisis de escenarios con el histórico real del pool, no una recomendación de inversión "
                    "personalizada: el simulador no conoce tu situación ni sabe hacia dónde irá el precio."}


def run(key: str, chain: str | None, rangos: list[str], capital: float = 5000, dias: int = 30,
        historia: int = HISTORIA_DIAS, escenarios: int = 1000, bloque: int = 168, tendencia: str = "neutral",
        gas: float | None = None, h: History | None = None, ajuste: float = AJUSTE_COMISIONES,
        nivel_dias: int | None = NIVEL_DIAS, exponente: float = EXPONENTE_PLAZO, escala: float | None = None) -> dict:
    h = h or load(key, chain, historia)
    paths = bootstrap(h, dias * 24, escenarios, bloque, tendencia, ajuste=ajuste, nivel_dias=nivel_dias,
                      exponente=exponente, escala=escala)
    out = [resumen(simulate(h, paths, parse_rango(r, h, paths), capital, gas)) for r in rangos]
    asset, quote, _ = B.pair_of(h.pool)
    ref = referencia_sin_riesgo(h.pool["chain"])
    if ref:
        ref["valor_final"] = capital * (1 + ref["apy"]) ** (dias / 365)
    fiab = fiabilidad(h, dias, calibracion_pool(h.pool["pool_id"], h.pool["chain"], dias))
    res = {"pool": {k: h.pool[k] for k in ("chain", "version", "label", "pool_id", "fee", "tick_spacing")},
            "par": f"{asset}/{quote}", "precio": price_now(h), "hora": str(h.hour_now), "tvl": h.tvl_now,
            "historia_desde": str(h.df["hour"].iloc[0]), "horas_historia": len(h.df),
            "capital": capital, "dias": dias, "escenarios": escenarios, "bloque_h": bloque,
            "tendencia": tendencia, "comision_cambio": h.swap_fee, "ajuste_comisiones": ajuste,
            "nivel_dias": nivel_dias, "nivel_reciente": h.nivel(nivel_dias), "factor_plazo": factor_plazo(dias, exponente),
            "vol_reciente": h.vol_reciente(VOL_DIAS), "escala_vol": escala_vol(h, dias) if escala is None else escala,
            "gas": GAS.get(h.pool["chain"], 0.05) if gas is None else gas,
            "aviso": h.pool.get("aviso"), "rangos": out, "abanico": abanico(h, paths),
            "sin_riesgo": ref, "fiabilidad": fiab}
    res["conclusiones"] = conclusiones(res)
    curva = barrido(h, paths, capital, gas)
    res["mejor_ancho"] = mejor_rango(curva, "media") | {"es_el_mas_ancho": mejor_rango(curva, "media") is curva[-1]}
    res["recomendacion"] = recomendacion(res)
    return res


# --- calibración: ¿habría acertado en el pasado? ----------------------------------------------

def calibrar(key: str, chain: str | None, dias: int = 30, ancho: float = 0.10, historia: int = HISTORIA_DIAS,
             escenarios: int = 400, bloque: int = 168, paso: int = 7, tendencia: str = "neutral",
             ajuste: float = AJUSTE_COMISIONES, ultimos: int | None = None, hasta: str | None = None,
             full: History | None = None, nivel_dias: int | None = NIVEL_DIAS,
             exponente: float = EXPONENTE_PLAZO) -> dict:
    """Desde muchas fechas pasadas: escenarios con los datos de ANTES de la fecha y comparación con
    lo que pasó de verdad. Si está bien calibrado, el real cae dentro del P10–P90 ~80 % de las veces."""
    full = full or load(key, chain, historia=100_000)
    df, pool = full.df, full.pool
    if hasta:                                     # como si hoy fuera `hasta`
        df = df[df["hour"] <= pd.Timestamp(hasta)].reset_index(drop=True)
    hours = df["hour"]
    t = hours.iloc[0] + pd.Timedelta(days=historia)
    if ultimos:                                   # solo las aperturas de los últimos `ultimos` días
        t = max(t, hours.iloc[-1] - pd.Timedelta(days=ultimos + dias))
    rows = []
    while t + pd.Timedelta(days=dias) <= hours.iloc[-1]:
        past = df[(hours > t - pd.Timedelta(days=historia)) & (hours <= t)].reset_index(drop=True)
        fut = df[(hours >= t) & (hours <= t + pd.Timedelta(days=dias))].reset_index(drop=True)
        if len(past) > historia * 24 * 0.8 and len(fut) >= dias * 24 * 0.9:
            hp = features(pool, past)
            hf = features(pool, fut)
            p = price_now(hp)
            rg = parse_rango(f"{ancho * 100}", hp)
            sim = simulate(hp, bootstrap(hp, len(fut) - 1, escenarios, bloque, tendencia, seed=None, ajuste=ajuste,
                                        nivel_dias=nivel_dias, exponente=exponente), rg)
            hf.liquidity_now = hp.liquidity_now                   # lo que se sabía al abrir
            real = simulate(hf, replay(hf), rg)
            fv, rv = sim.final, float(real.final[0])
            rows.append({"fecha": str(t.date()), "precio": p, "real": rv,
                         "pct": float(np.mean(fv < rv)), "p10": float(np.percentile(fv, 10)),
                         "p50": float(np.median(fv)), "p90": float(np.percentile(fv, 90)),
                         "fees_real": float(real.fees[0]), "fees_p50": float(np.mean(sim.fees)),
                         "rango_real": float(real.in_range[0]), "rango_p50": float(np.mean(sim.in_range))})
        t += pd.Timedelta(days=paso)
    if not rows:
        raise B.BacktestError("no hay fechas suficientes para calibrar con ese histórico")
    r = pd.DataFrame(rows)
    pct = r["pct"].to_numpy()
    return {"pool": pool["label"], "red": pool["chain"], "pool_id": pool["pool_id"], "dias": dias, "ancho": ancho,
            "historia": historia,
            "pruebas": len(r), "dentro_p10_p90": float(np.mean((pct >= 0.1) & (pct <= 0.9))),
            "dentro_p5_p95": float(np.mean((pct >= 0.05) & (pct <= 0.95))),
            "por_debajo_p10": float(np.mean(pct < 0.1)), "por_encima_p90": float(np.mean(pct > 0.9)),
            "mediana_pct": float(np.median(pct)),
            "comisiones_real_vs_p50": float(r["fees_real"].sum() / max(r["fees_p50"].sum(), 1e-9)),
            "en_rango_real": float(r["rango_real"].mean()), "en_rango_sim": float(r["rango_p50"].mean()),
            "detalle": rows}


# --- consola ----------------------------------------------------------------------------------

def _eur(x: float, signo: bool = False) -> str:
    s = f"{x:+,.0f}" if signo else f"{x:,.0f}"
    return s.replace(",", ".") + " $"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pool")
    ap.add_argument("--red")
    ap.add_argument("--capital", type=float, default=5000)
    ap.add_argument("--dias", type=int, default=30)
    ap.add_argument("--rangos", default="±5%,±10%,±20%", help="lista: 1500-1900,±10%%,-5/+15")
    ap.add_argument("--historia", type=int, default=HISTORIA_DIAS, help="días de histórico para los escenarios")
    ap.add_argument("--escenarios", type=int, default=1000)
    ap.add_argument("--bloque", type=int, default=168, help="horas consecutivas por bloque")
    ap.add_argument("--tendencia", choices=("neutral", "historica"), default="neutral")
    ap.add_argument("--gas", type=float)
    ap.add_argument("--ajuste", type=float, default=AJUSTE_COMISIONES, help="factor común sobre las comisiones (1 = sin ajuste)")
    ap.add_argument("--nivel", type=int, default=NIVEL_DIAS, help="días del nivel reciente de comisiones (0 = sin nivel)")
    ap.add_argument("--exponente", type=float, default=EXPONENTE_PLAZO,
                    help="corrección de comisiones por plazo: × (días/30)^-exponente (0 = sin corrección)")
    ap.add_argument("--escala", type=float, help="escala de la volatilidad de los escenarios (por defecto, automática; 1 = el histórico tal cual)")
    ap.add_argument("--calibrar", action="store_true")
    ap.add_argument("--ancho", type=float, default=10, help="calibración: ±%% del rango")
    ap.add_argument("--paso", type=int, default=7)
    ap.add_argument("--ultimos", type=int, help="calibración: solo aperturas de los últimos N días")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.calibrar:
            c = calibrar(a.pool, a.red, a.dias, a.ancho / 100, a.historia, paso=a.paso, tendencia=a.tendencia, ajuste=a.ajuste,
                         ultimos=a.ultimos, nivel_dias=a.nivel or None, exponente=a.exponente)
            if a.json:
                print(json.dumps(c, ensure_ascii=False, indent=1))
                return 0
            print(f"Calibración {c['pool']} ({c['red']}) · rango ±{a.ancho:g} % · {a.dias} días · "
                  f"histórico {a.historia} d · {c['pruebas']} fechas")
            print(f"  real dentro de P10–P90: {c['dentro_p10_p90']:.0%} (ideal 80 %) · P5–P95: {c['dentro_p5_p95']:.0%} (ideal 90 %)")
            print(f"  por debajo de P10: {c['por_debajo_p10']:.0%} · por encima de P90: {c['por_encima_p90']:.0%} (ideal 10 % y 10 %)")
            print(f"  comisiones reales / media simulada: {c['comisiones_real_vs_p50']:.2f} (ideal ≈ 1)")
            print(f"  tiempo en rango real {c['en_rango_real']:.0%} · simulado {c['en_rango_sim']:.0%}")
            return 0
        rangos = [r for r in a.rangos.split(",") if r.strip()]
        res = run(a.pool, a.red, rangos, a.capital, a.dias, a.historia, a.escenarios, a.bloque, a.tendencia, a.gas,
                  ajuste=a.ajuste, nivel_dias=a.nivel or None, exponente=a.exponente, escala=a.escala)
    except (B.BacktestError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    print(f"{res['pool']['label']} ({res['pool']['chain']} {res['pool']['version']}) · {res['par']} = "
          f"{_fmt_price(res['precio'])} a {res['hora']} · {res['escenarios']} escenarios a {res['dias']} días "
          f"(histórico desde {res['historia_desde'][:10]}, tendencia {res['tendencia']}, comisiones al nivel de "
          f"{res['nivel_dias'] or 0} días ×{res['nivel_reciente']:.2f}, ajuste {res['ajuste_comisiones']:.2f} "
          f"y plazo ×{res['factor_plazo']:.2f})")
    print(f"{'Estrategia':<28}{'Valor final':>13}{'P10–P90':>22}{'vs mant.':>10}{'Comis.':>9}{'APR':>8}"
          f"{'Rent':>8}{'En rango':>10}{'P(pérd.)':>10}")
    for r in res["rangos"]:
        vf = r["valor_final"]
        print(f"{r['estrategia']:<28}{_eur(vf['p50']):>13}{_eur(vf['p10']) + '–' + _eur(vf['p90']):>22}"
              f"{_eur(r['vs_mantener']['p50'], True):>10}{_eur(r['comisiones']['p50']):>9}{r['apr']:>8.1%}"
              f"{r['rent']['p50']:>+8.1%}{r['en_rango']:>10.0%}{r['prob_perdida']:>10.0%}")
    if res["sin_riesgo"]:
        s = res["sin_riesgo"]
        print(f"Referencia: {s['nombre']} {s['apy']:.2%} → {_eur(s['valor_final'])} en {res['dias']} días")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
