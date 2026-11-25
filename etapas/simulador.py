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
# Sin recalibrar la horquilla (25-nov-2026, 16 pools, 6.451 aperturas de dos años): el P10–P90 del valor
# final recoge el 77–80 % a 7–30 días y el 87–91 % a 90–180, pero el desvío cambia de signo según el año;
# reasignar percentiles o estirar alrededor de la mediana con un año empeoró el otro en 6 de 8 casos.


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
    reajustes: np.ndarray | None = None        # veces que se recentra el rango (con reajuste)
    costes_reajuste: np.ndarray | None = None  # $ de gas + cambio de esos reajustes
    regla: str = "nunca"


def simulate(h: History, paths: Paths, rango: Rango, capital: float = 5000.0, gas: float | None = None,
             diluir: bool = True, abierta: bool = False, cambio: float = 1.0) -> Sim:
    """`abierta`: la posición ya existe (vale `capital` hoy): sin gas ni cambio inicial. `cambio`: parte
    del cambio inicial que se paga (con cobertura, el activo prestado no hay que comprarlo)."""
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
    swap = 0.0 if abierta else cambio * value * h.swap_fee * ((1 - share0) if stable0 else share0 if stable1 else 0.5)
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


# Reglas de reajuste: horas seguidas fuera del rango antes de recentrarlo (None = nunca).
REGLAS = {"nunca": None, "al salirse": 0, "fuera 24 h": 24, "fuera 72 h": 72}


def simulate_reajuste(h: History, paths: Paths, rango: Rango, espera: int | None, capital: float = 5000.0,
                      gas: float | None = None, diluir: bool = True, abierta: bool = False) -> Sim:
    """Como `simulate`, pero cuando el precio lleva más de `espera` horas fuera del rango se recentra en el
    precio de ese momento con el mismo ancho en ticks (y la misma asimetría): se retira todo (posición +
    comisiones), se cambia lo que haga falta pagando la comisión del pool y se vuelve a abrir, con un gas
    por reajuste. Igual que `backtest.simulate` con `--reajuste fuera`. `espera` None = nunca (= simulate)."""
    if espera is None:
        s = simulate(h, paths, rango, capital, gas, diluir, abierta)
        s.reajustes, s.costes_reajuste = np.zeros(len(s.final)), np.zeros(len(s.final))
        return s
    pool = h.pool
    d1 = 10 ** pool["dec1"]
    gas_tx = GAS.get(pool["chain"], 0.05) if gas is None else gas
    base = simulate(h, paths, rango, capital, gas, diluir, abierta)      # apertura, mantener, dilución
    sp, u1 = paths.sp, paths.usd1
    S_, H = sp.shape[0], sp.shape[1] - 1
    sp0 = float(sp[0, 0])
    spacing = pool["tick_spacing"]
    t0 = 2 * math.log(sp0) / math.log(B.TICK_BASE)
    dl, du = t0 - rango.tl, rango.tu - t0                               # distancia en ticks a cada borde

    def val(a0, a1, s, u):
        return (a0 * s ** 2 + a1) * u / d1

    def amounts(s, sa, sb):                                             # por unidad de liquidez (vector)
        a0 = np.where(s <= sa, 1 / sa - 1 / sb, np.where(s >= sb, 0.0, 1 / s - 1 / sb))
        a1 = np.where(s <= sa, 0.0, np.where(s >= sb, sb - sa, s - sa))
        return a0, a1

    sa = np.full(S_, B._sqrt(rango.tl))
    sb = np.full(S_, B._sqrt(rango.tu))
    a0u, a1u = B._amounts(sp0, sa[0], sb[0])
    L = np.full(S_, (capital - base.costs) / val(a0u, a1u, sp0, float(u1[0, 0])))
    dil = base.dilution
    f0 = np.zeros(S_)
    f1 = np.zeros(S_)
    fees = np.zeros(S_)
    in_h = np.zeros(S_)
    out_h = np.zeros(S_, int)
    n_reb = np.zeros(S_)
    c_reb = np.zeros(S_)
    lo_t = math.ceil(B.MIN_TICK / spacing) * spacing
    hi_t = math.floor(B.MAX_TICK / spacing) * spacing
    for i in range(H):
        s_now, s_nxt = sp[:, i], sp[:, i + 1]
        ins_now = (s_now >= sa) & (s_now < sb)
        ins_nxt = (s_nxt >= sa) & (s_nxt < sb)
        part = (ins_now.astype(float) + ins_nxt) / 2
        in_h += part
        g0 = L * dil * paths.q0[:, i] / s_nxt * part
        g1 = L * dil * paths.q1[:, i] * s_nxt * part
        f0 += g0
        f1 += g1
        fees += val(g0, g1, s_nxt, u1[:, i + 1])
        out_h = np.where(ins_nxt, 0, out_h + 1)
        go = (out_h > espera) & (i + 1 < H)
        if go.any():
            s, u = s_nxt[go], u1[go, i + 1]
            p0, p1 = amounts(s, sa[go], sb[go])
            a0, a1 = p0 * L[go] + f0[go], p1 * L[go] + f1[go]
            v = val(a0, a1, s, u)
            cur0 = val(a0, 0, s, u) / np.where(v > 0, v, 1)
            t = 2 * np.log(s) / math.log(B.TICK_BASE)
            ntl = np.clip(np.floor((t - dl) / spacing) * spacing, lo_t, hi_t)
            ntu = np.clip(np.ceil((t + du) / spacing) * spacing, lo_t, hi_t)
            ntu = np.maximum(ntu, ntl + spacing)
            nsa, nsb = B.TICK_BASE ** (ntl / 2), B.TICK_BASE ** (ntu / 2)
            n0, n1 = amounts(s, nsa, nsb)
            per_l = val(n0, n1, s, u)
            nshare0 = val(n0, 0, s, u) / per_l
            c = gas_tx + v * np.abs(cur0 - nshare0) * h.swap_fee
            sa[go], sb[go] = nsa, nsb
            L[go] = np.maximum(v - c, 0) / per_l
            f0[go] = f1[go] = 0.0
            out_h[go] = 0
            n_reb[go] += 1
            c_reb[go] += c
    se, ue = sp[:, -1], u1[:, -1]
    p0, p1 = amounts(se, sa, sb)
    final = val(p0 * L + f0, p1 * L + f1, se, ue)
    _, _, asset0 = B.pair_of(pool)
    side = np.where(se < sa, -1, np.where(se >= sb, 1, 0)) * (1 if asset0 else -1)
    nombre = next((k for k, x in REGLAS.items() if x == espera), f"fuera {espera} h")
    return Sim(rango, capital, H / 24, base.costs, final, fees, base.hodl, in_h / max(H, 1), side, dil,
               base.precio0, n_reb, c_reb, nombre)


# Reglas de salida (stop): si el precio del activo cae un X % por debajo del borde de abajo del rango (al
# cierre de una hora), se cierra la posición y se pasa todo a dólares hasta el final del plazo.
SALIDAS = {"sin stop": None, "al salirse por abajo": 0.0, "5 % bajo el borde": 0.05, "10 % bajo el borde": 0.10}


def simulate_salida(h: History, paths: Paths, rango: Rango, x: float | None, capital: float = 5000.0,
                    gas: float | None = None, diluir: bool = True, abierta: bool = False, apy: float = 0.0) -> Sim:
    """Como `simulate`, pero con un stop: la primera hora en que el precio del activo cierra un `x` por
    debajo del borde de abajo, se retira todo (posición + comisiones), se vende a dólares pagando la
    comisión del pool sobre lo que no es estable y un gas, y se queda en dólares. `x` None = sin stop.
    `Sim.reajustes` = 1 si saltó el stop; `costes_reajuste` = lo que costó salir. `apy`: lo que rinden los
    dólares hasta el final del plazo (la referencia sin riesgo, USDC en Aave)."""
    base = simulate(h, paths, rango, capital, gas, diluir, abierta)
    n = len(base.final)
    if x is None:
        base.reajustes, base.costes_reajuste = np.zeros(n), np.zeros(n)
        base.regla = "sin stop"
        return base
    pool = h.pool
    d1 = 10 ** pool["dec1"]
    gas_tx = GAS.get(pool["chain"], 0.05) if gas is None else gas
    sa, sb = B._sqrt(rango.tl), B._sqrt(rango.tu)
    sp, u1 = paths.sp, paths.usd1
    sp0, u10 = float(sp[0, 0]), float(u1[0, 0])
    a0u, a1u = B._amounts(sp0, sa, sb)
    per_l = (a0u * sp0 ** 2 + a1u) * u10 / d1
    L = (capital - base.costs) / per_l
    dil = base.dilution
    inside = (sp >= sa) & (sp < sb)
    part = (inside[:, :-1].astype(float) + inside[:, 1:]) / 2
    nxt = sp[:, 1:]
    F0 = np.cumsum(L * dil * paths.q0 / nxt * part, axis=1)          # comisiones acumuladas a cada hora
    F1 = np.cumsum(L * dil * paths.q1 * nxt * part, axis=1)
    _, quote, asset0 = B.pair_of(pool)
    if asset0:                                   # precio del activo = P del pool
        salta = nxt ** 2 <= sa ** 2 * (1 - x)
    else:                                        # el activo es el token1: cae cuando P sube
        salta = nxt ** 2 >= sb ** 2 / (1 - x)
    hay = salta.any(axis=1)
    t = np.where(hay, salta.argmax(axis=1), nxt.shape[1] - 1)
    r = np.arange(n)
    s, u = nxt[r, t], u1[r, t + 1]
    p0 = np.where(s <= sa, 1 / sa - 1 / sb, np.where(s >= sb, 0.0, 1 / s - 1 / sb)) * L + F0[r, t]
    p1 = np.where(s <= sa, 0.0, np.where(s >= sb, sb - sa, s - sa)) * L + F1[r, t]
    v0, v1 = p0 * s ** 2 * u / d1, p1 * u / d1
    estable0, estable1 = pool["symbol0"] in B._STABLE, pool["symbol1"] in B._STABLE
    no_estable = (v1 if estable0 else v0 if estable1 else v0 + v1)
    coste = gas_tx + no_estable * h.swap_fee
    resto = (nxt.shape[1] - 1 - t) / 8760                       # años que quedan tras salir
    final = np.where(hay, (v0 + v1 - coste) * (1 + apy) ** resto, base.final)
    fees = np.where(hay, ((F0[r, t] * s ** 2 + F1[r, t]) * u / d1), base.fees)
    in_r = np.where(hay, part.cumsum(axis=1)[r, t] / max(nxt.shape[1], 1), base.in_range)
    nombre = next((k for k, v in SALIDAS.items() if v == x), f"{x:.0%} bajo el borde")
    return Sim(rango, capital, base.dias, base.costs, final, fees, base.hodl, in_r, base.side, dil, base.precio0,
               hay.astype(float), np.where(hay, coste, 0.0), nombre)


CERCA_STOP = 0.03                 # «cerca del stop»: a menos de un 3 % del precio del stop


def nivel_stop(pool: dict, tl: int, tu: int, x: float) -> dict:
    """El stop en ticks del pool, para vigilarlo con el tick que da Revert. Si el activo es el token0,
    salta cuando el tick baja de `tick` (precio ≤ borde de abajo × (1 − x)); si es el token1, cuando sube
    de `tick` (el activo cae cuando el precio del pool sube). `cerca`: el tick a un CERCA_STOP de él."""
    _, _, asset0 = B.pair_of(pool)
    d = math.log(1 - x) / math.log(B.TICK_BASE)          # ≤ 0
    c = math.log(1 + CERCA_STOP) / math.log(B.TICK_BASE)
    if asset0:
        t = tl + d
        return {"tick": t, "cerca": t + c, "asset0": True, "precio": _display(pool, B.TICK_BASE ** t)}
    t = tu - d
    return {"tick": t, "cerca": t - c, "asset0": False, "precio": _display(pool, B.TICK_BASE ** t)}


def estado_stop(tick: float, nivel: dict) -> str:
    """«cruzado» si el tick del pool está más allá del stop, «cerca» a menos de un CERCA_STOP, si no «lejos»."""
    s = 1 if nivel["asset0"] else -1                    # con asset0, más abajo = peor
    if s * tick <= s * nivel["tick"]:
        return "cruzado"
    return "cerca" if s * tick <= s * nivel["cerca"] else "lejos"


def salidas(h: History, paths: Paths, rango: Rango, capital: float = 5000.0, gas: float | None = None,
            abierta: bool = False, reglas: dict | None = None, apy: float = 0.0) -> list[dict]:
    """Cada stop frente a no poner ninguno, en los mismos escenarios (en $)."""
    out, fijo = [], None
    for nombre, x in (reglas or SALIDAS).items():
        s = simulate_salida(h, paths, rango, x, capital, gas, abierta=abierta, apy=apy)
        fijo = s.final if fijo is None else fijo
        out.append({"regla": nombre, "x": x, "dif_media": float(np.mean(s.final - fijo)),
                    "prob_salta": float(np.mean(s.reajustes)),
                    "valor_media": float(np.mean(s.final)), "valor_mediana": float(np.median(s.final)),
                    "valor_p5": float(np.percentile(s.final, 5)), "valor_p10": float(np.percentile(s.final, 10)),
                    "valor_p90": float(np.percentile(s.final, 90)),
                    "prob_perdida": float(np.mean(s.final < capital)),
                    "vs_media": float(np.mean(s.final - s.hodl)), "comisiones": float(np.mean(s.fees))})
    return out


# Validado el 25-nov-2026 (16 pools de referencia, aperturas de dos años, 7/30/90 días, ±10 % y ±20 %, con
# los escenarios de la tabla principal): las veces que salta el stop salen casi exactas (30 d ±10 % «al
# salirse por abajo»: 52 % simulado frente a 46 % real; 90 d: 74 % frente a 70 %), el P10–P90 con stop
# recoge el 77–88 % y el stop recorta la cola real como dice el simulador (30 d ±10 %: P10 real de 3.881 $
# a 4.453 $ por 5.000 $). Tras salir, los dólares rinden la referencia sin riesgo (USDC en Aave).
# Volver a entrar cuando el precio regresa al rango, probado y descartado (25-nov-2026, mismas aperturas): en
# la realidad salió peor que el stop solo en todos los casos (30 d ±10 %: −12 $ frente a +29 $; 90 d: −77 $
# frente a +44 $) y perdía casi toda la protección de la cola (P10 real 4.148 $ frente a 4.453 $): entra y
# sale una y otra vez (2,1 salidas de media a 30 días, 5,7 a 90) pagando cambio y gas en cada vuelta. Lo que cuesta de media depende de la tendencia, que el simulador no apuesta: en
# el año lateral (hasta ago-2025) coincidió (30 d −38 $ real, −45 $ simulado; 90 d −119 $ y −125 $), en el
# bajista (desde ago-2025) el stop ganó (+87 $ y +230 $). A 7 días casi nunca compensa.
PLAZO_SALIDA_VALIDADO = 90


def comparar_salidas(h: History, dias: int, rangos: list[Rango], paths: Paths, capital: float = 5000,
                     gas: float | None = None, abierta: bool = False, apy: float = 0.0) -> dict:
    """Para cada rango, sin stop frente a cada regla de SALIDAS en los escenarios principales. Se sugiere
    el stop que más sube el P10 si lo sube al menos un 2 % del capital y cuesta de media menos del 1 %."""
    items = []
    for rg in rangos:
        tabla = salidas(h, paths, rg, capital, gas, abierta, apy=apy)
        for x in tabla:
            x["precio"] = None if x["x"] is None else rg.lo * (1 - x["x"])
        p10 = tabla[0]["valor_p10"]
        ok = [x for x in tabla[1:] if x["valor_p10"] - p10 >= 0.02 * capital and x["dif_media"] >= -0.01 * capital]
        items.append({"texto": rg.texto, "reglas": tabla,
                      "sugerido": max(ok, key=lambda x: x["valor_p10"])["regla"] if ok else None})
    return {"rangos": items, "validado": dias <= PLAZO_SALIDA_VALIDADO, "apy": apy}


# Escenarios para comparar reglas de reajuste. Validado el 24-nov-2026 (16 pools de referencia, 1.113
# aperturas del último año, ±5 % y ±10 %): con los escenarios de la tabla principal, a 30 días el
# simulador contaba un 30–50 % más de reajustes de los reales (8,4 frente a 6,0 a ±5 %) y castigaba
# reajustar unos 100 $ por 5.000 $ de más, porque el ensanche por plazo hace los caminos más
# rugosos y el precio sale más veces. Con bloques de 14 días y la volatilidad reciente sin el ensanche
# por plazo, los reajustes salen 6,5 frente a 6,0 y 2,0 frente a 1,7, y la diferencia frente a dejarlo
# fijo queda a unos 20–35 $ de la real (algo prudente con el reajuste). A 7 días los dos coinciden.
BLOQUE_REAJUSTE = 336
PLAZO_REAJUSTE_VALIDADO = 30


def escenarios_reajuste(h: History, dias: int, escenarios: int = 500, seed: int | None = 11,
                        ajuste: float = AJUSTE_COMISIONES) -> Paths:
    esc = float(np.clip(h.vol_reciente(VOL_DIAS), 0.5, 2.0))
    return bootstrap(h, dias * 24, escenarios, BLOQUE_REAJUSTE, seed=seed, ajuste=ajuste, escala=esc)


def reajustes(h: History, paths: Paths, rango: Rango, capital: float = 5000.0, gas: float | None = None,
              abierta: bool = False, reglas: dict | None = None) -> list[dict]:
    """Cada regla de reajuste frente a dejar el rango fijo, en los mismos escenarios (medias en $).
    `dif_media`: lo que gana (o pierde) de media frente a no tocarlo."""
    out = []
    fijo = None
    for nombre, espera in (reglas or REGLAS).items():
        s = simulate_reajuste(h, paths, rango, espera, capital, gas, abierta=abierta)
        vs = s.final - s.hodl
        fijo = s.final if fijo is None else fijo
        out.append({"regla": nombre, "espera_h": espera, "dif_media": float(np.mean(s.final - fijo)),
                    "prob_mejor": float(np.mean(s.final > fijo + 1e-9)) if espera is not None else None,
                    "valor_media": float(np.mean(s.final)),
                    "valor_mediana": float(np.median(s.final)), "valor_p10": float(np.percentile(s.final, 10)),
                    "vs_media": float(np.mean(vs)), "vs_mediana": float(np.median(vs)),
                    "comisiones": float(np.mean(s.fees)), "en_rango": float(np.mean(s.in_range)),
                    "reajustes": float(np.mean(s.reajustes)), "coste_reajustes": float(np.mean(s.costes_reajuste))})
    return out


def resumen(s: Sim) -> dict:
    q = lambda a: {f"p{p}": float(np.percentile(a, p)) for p in PCTS}
    rent = s.final / s.capital - 1
    ann = 365 / max(s.dias, 1e-9)
    med_fees = float(np.median(s.fees))
    return {
        "estrategia": s.rango.nombre, "texto": s.rango.texto, "lo": s.rango.lo, "hi": s.rango.hi,
        "tl": s.rango.tl, "tu": s.rango.tu,
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


# --- etapa de Weinstein del par (la misma lectura que /pool, el cribado y la rutina) -------------

_ETAPA_CACHE: dict = {}
MARCO_LABEL = {"1d": "diario", "1w": "semanal", "1M": "mensual"}


def marco_para(dias: int) -> str:
    """Marco de la etapa que corresponde al plazo: semanal hasta 14 días, mensual a partir de ahí."""
    return "1w" if dias <= 14 else "1M"


def etapa_del_par(pool: dict, dias: int) -> dict | None:
    """Etapa del par en el marco del plazo y el paso 1 de la decisión del sistema (`liquidez.verdict`):
    apta / precaución. En el histórico (fase 0, oct-2026, ~8.400 aperturas), en etapa 2 los rangos
    semanales y mensuales pasaron dentro el 70 % y el 63 % de los ciclos (85–97 % en las demás) y
    perdieron más frente a mantener. None si el par es de dos estables; «sin datos» si no hay velas.
    Se guarda una hora (las velas son diarias como mínimo)."""
    from . import liquidez as LQ
    from .analysis import analyze_symbol
    from .data import canonical_pair, fetch_pair_candles
    asset, quote = par_de(pool)
    estables = {s.upper() for s in B._STABLE}
    if asset.upper() in estables:
        return None
    quote = "USDC" if quote == "USD" else quote
    base, quote = canonical_pair(asset, quote)
    serie = {"XAUT": "PAXG"}.get(base, base)
    marco = marco_para(dias)
    key = (base, quote, pd.Timestamp.now().floor("h"))
    if key not in _ETAPA_CACHE:
        try:
            _ETAPA_CACHE[key] = analyze_symbol(base, fetch=lambda t, cfg: fetch_pair_candles(serie, quote, cfg))
        except Exception as e:                                   # noqa: BLE001 (la etapa es un complemento)
            return {"par": f"{base}/{quote}", "marco": marco, "marco_label": MARCO_LABEL[marco], "nivel": "sin datos",
                    "motivos": [f"no se pudo leer el precio para la etapa ({type(e).__name__})"]}
    a = _ETAPA_CACHE[key]
    r = a.timeframes.get(marco)
    if r is None:
        return None
    nivel, motivos = LQ.verdict(r, None, base, quote)
    otros = {MARCO_LABEL[k]: (t.stage if t.status == "ok" else None) for k, t in a.timeframes.items() if k in MARCO_LABEL}
    return {"par": f"{base}/{quote}", "marco": marco, "marco_label": MARCO_LABEL[marco], "etapa": r.stage,
            "nombre": r.stage_name, "transicion": r.transition, "confianza": r.confidence_label, "nivel": nivel,
            "motivos": motivos, "otros_marcos": otros}


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
    """Semáforo: alta / media / baja, con los motivos. Se basa en lo validado (pools con ≥ 1 año y
    TVL > 2 M$; 7–180 días) y, si existe, en la calibración mensual del propio pool."""
    from .simulador_aviso import ANOMALOS, MIN_TVL
    malos: list[tuple[int, str]] = []          # (0 media / 1 baja, motivo)
    buenos: list[str] = []
    pool = h.pool
    if (pool["chain"], pool["pool_id"]) in ANOMALOS:
        malos.append((1, "MetaProDB tiene comisiones anómalas en este pool: el simulador no las reproduce"))
    hist = h.dias_historia
    # 25-nov-2026 (13 pools, 901 aperturas del último año a 7 y 30 días): recortado a 365 o 180 días, el
    # histórico acertó igual que con 730 (P10–P90 68–69 % a 30 días, error medio 242–246 $ por 5.000 $), y
    # completarlo con otro pool del mismo par no mejoró nada. Por debajo de 180 no está medido.
    if hist < 180:
        malos.append((1, f"solo {hist:.0f} días de histórico"))
    elif hist < 365:
        malos.append((0, f"{hist:.0f} días de histórico: con 180–365 acertó igual que con 730 en el último año, "
                         "pero cubre menos rachas distintas del mercado"))
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
        buenos.append("pool como los validados (≥ 1 año de histórico, TVL > 2 M$)")
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


def _num1(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


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

    et = res.get("etapa") or {}
    if et.get("nivel") == "precaución":
        ttxt = f" (en transición {et['transicion']})" if et.get("transicion") else ""
        no_hacer.append(f"No abras un rango estrecho y centrado: {et['par']} está en etapa {et['etapa']} ({et['nombre']}) "
                        f"en el marco {et['marco_label']}{ttxt}. En el histórico, los rangos semanales y mensuales abiertos "
                        "en etapa 2 pasaron dentro solo el 70 % y el 63 % de los ciclos (85–97 % en las demás etapas) y "
                        "perdieron más frente a mantener. El simulador no apuesta por la dirección, así que este riesgo "
                        "no está en sus cifras.")
        mejorar.append("Si abres igualmente con la tendencia alcista, ensancha el rango o desplázalo hacia arriba "
                       "(asimétrico, por ejemplo −5/+15) y vigílalo de cerca: la tendencia empuja el precio fuera por arriba.")
        if buenos:
            hacer.append("Ojo: aunque los números salen favorables, la etapa pide precaución (arriba, «Qué no "
                         "recomiendo»): usa menos capital del que usarías en una etapa lateral.")
    elif et.get("nivel") == "apta" and et.get("etapa") in (1, 3) and buenos:
        hacer.append(f"La etapa acompaña: {et['par']} está en etapa {et['etapa']} (lateral) en el marco "
                     f"{et['marco_label']}, la más favorable para un rango.")
    elif et.get("nivel") == "sin datos":
        mejorar.append("No he podido leer la etapa del par: compruébala con /pool antes de abrir.")

    rj = res.get("reajuste") or {}
    foco = (max(buenos, key=gana) if buenos else best)["texto"]
    item = next((x for x in rj.get("rangos", []) if x["texto"] == foco), None)
    if item:
        reglas = {x["regla"]: x for x in item["reglas"]}
        aviso = "" if rj.get("validado") else " (a más de 30 días esta comparación no está validada: tómala como orientación)"
        if item["mejor"] != "nunca":
            m = reglas[item["mejor"]]
            mejorar.append(f"Si el precio se sale de {foco}, recentrarlo con la regla «{item['mejor']}» gana de media "
                           f"{_usd(m['dif_media'])} frente a dejarlo fijo ({_num1(m['reajustes'])} reajustes, "
                           f"{_usd(m['coste_reajustes'], False)} de gas y cambios){aviso}.")
        else:
            al = reglas.get("al salirse")
            txt = (f"recentrarlo nada más salirse {'pierde' if al['dif_media'] < 0 else 'gana'} de media "
                   f"{_usd(abs(al['dif_media']), False)} frente a dejarlo fijo ({_num1(al['reajustes'])} reajustes de media)"
                   if al else "ninguna regla de reajuste mejora a dejarlo fijo")
            no_hacer.append(f"No persigas el precio: con {foco}, {txt}. Cada reajuste fija la pérdida frente a "
                            f"mantener y paga gas y cambio; si se sale, decide entre esperar o cerrar{aviso}.")

    es = next((x for x in (res.get("estres") or {}).get("rangos", []) if x["texto"] == foco), None)
    if es:
        bd, casos = es["bordes"], {c["caso"]: c for c in es["casos"]}
        cae, peor = casos.get("Cae 30 %"), next((c for c in es["casos"] if c.get("real")), None)
        txt = (f"Ten clara tu salida con {foco}: deja de cobrar si {asset} baja a {_fmt_price(bd['abajo']['precio'])} "
               f"(la posición valdría {_usd(bd['abajo']['valor'], False)}, {_usd(bd['abajo']['resultado'])}) o sube a "
               f"{_fmt_price(bd['arriba']['precio'])}. Ese precio de abajo es un buen sitio para el stop.")
        if cae:
            txt += (f" Si {asset} cae un 30 % en el plazo, el resultado sería de {_usd(cae['resultado'])} "
                    f"({_usd(cae['vs_mantener'])} frente a guardar los tokens)")
            txt += (f"; en el peor tramo real del pool ({asset} {_mov(peor['movimiento'])} en {dias} días desde el "
                    f"{pd.Timestamp(peor['desde']):%d-%m-%Y}), de {_usd(peor['resultado'])}." if peor else ".")
        sl = next((x for x in (res.get("salida") or {}).get("rangos", []) if x["texto"] == foco), None)
        if sl:
            sg = next((x for x in sl["reglas"] if x["regla"] == sl["sugerido"]), None)
            r0 = sl["reglas"][0]
            if sg:
                txt += (f" Un stop {sg['regla']} (vender a dólares si {asset} cierra una hora por debajo de "
                        f"{_fmt_price(sg['precio'])}) sube el escenario malo (P10) de {_usd(r0['valor_p10'], False)} a "
                        f"{_usd(sg['valor_p10'], False)} y {'cuesta' if sg['dif_media'] < 0 else 'gana'} de media "
                        f"{_usd(abs(sg['dif_media']), False)}; salta en el {sg['prob_salta'] * 100:.0f} % de los "
                        f"escenarios. Es un seguro: en un año bajista habría ganado, en uno lateral cuesta.")
            else:
                txt += (" Aquí un stop automático apenas mejora el escenario malo o cuesta demasiado de media: "
                        "mejor vigilar y decidir si cierras cuando se salga.")
        (hacer if buenos else mejorar).append(txt if buenos else "Si aun así abres: " + txt[0].lower() + txt[1:])

    cb = res.get("cobertura") or {}
    it = next((x for x in cb.get("rangos", []) if x["texto"] == foco), None)
    if it and len(it["filas"]) > 1:
        av, f0 = cb["aave"], it["filas"][0]
        f1 = max(it["filas"][1:], key=lambda f: f["valor_media"])
        nombre = "del todo" if f1["cobertura"] >= 1 else f"al {f1['cobertura']:.0%}"
        cae = lambda f: next((e["resultado"] for e in f.get("estres", []) if e["caso"] == "Cae 30 %"), None)
        txt = (f"Cubrir {foco} con un préstamo de {av['activo']} en Aave ({nombre}, HF {_num1(cb['hf'])}): de media "
               f"{_usd(f1['vs_sin_cubrir'])} frente a no cubrirlo, y el escenario malo (P10) pasa de "
               f"{_usd(f0['valor_p10'] - cap)} a {_usd(f1['valor_p10'] - cap)}")
        if cae(f0) is not None:
            txt += f"; si {asset} cae un 30 %, de {_usd(cae(f0))} a {_usd(cae(f1))}"
        txt += (f". Aun así, frente a dejar el dinero en {av['estable']} en Aave queda en {_usd(f1['vs_sin_riesgo'])}"
                + (f", y te liquidarían en el {_pct(f1['prob_liquidacion'])} de los escenarios" if f1["prob_liquidacion"] > 0.005
                   else "") + ".")
        if not av.get("disponible", True):
            txt = "Hoy no se puede: " + "; ".join(av["motivos"]) + ". Si Aave lo vuelve a permitir: " + txt[0].lower() + txt[1:]
        mejorar.append(txt)

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


def comparar_reajustes(h: History, dias: int, rangos: list[Rango], capital: float = 5000, gas: float | None = None,
                       escenarios: int = 500, ajuste: float = AJUSTE_COMISIONES, abierta: bool = False) -> dict:
    """Para cada rango: dejarlo fijo frente a recentrarlo al salirse (REGLAS), en escenarios propios
    (`escenarios_reajuste`). La mejor regla solo se destaca si gana de media más del 0,5 % del capital."""
    paths = escenarios_reajuste(h, dias, escenarios, ajuste=ajuste)
    items = []
    for rg in rangos:
        tabla = reajustes(h, paths, rg, capital, gas, abierta)
        mejor = max(tabla[1:], key=lambda r: r["dif_media"])
        items.append({"texto": rg.texto, "reglas": tabla,
                      "mejor": mejor["regla"] if mejor["dif_media"] > 0.005 * capital else "nunca"})
    return {"rangos": items, "validado": dias <= PLAZO_REAJUSTE_VALIDADO, "escenarios": escenarios,
            "gas": GAS.get(h.pool["chain"], 0.05) if gas is None else gas}


# --- cobertura con Aave -----------------------------------------------------------------------------
#
# Liquidez cubierta (como aave_backtest.simulate_covered): con el capital C en la estable de un pool
# activo/estable, se deposita D de la estable en Aave, se pide prestada una parte c del activo que pide
# el rango y el pool recibe ese activo (el resto se compra) más la estable. Con s = parte del activo en la
# posición y u = umbral / HF: V = C / (1 + c·s·(1/u − 1)), D = c·s·V / u, deuda = c·s·V. Si el activo cae,
# la deuda vale menos y compensa lo que pierde la posición; si sube, la deuda crece y el HF baja (riesgo de
# liquidación). Tipos y parámetros de Aave: los de hoy (MetaProDB), fijos durante el plazo.

COBERTURAS = (0.0, 0.5, 1.0)
HF_COBERTURA = 2.0                 # perfil de Javier: liquidez cubierta con HF ≥ 2 (aave-estrategias/perfil.md)


def aave_hoy(pool: dict) -> dict | None:
    """Datos de hoy de Aave en la red del pool: la estable como garantía y el activo como deuda.
    None si el par no es activo/estable o Aave no tiene esas reservas; {"error": …} si no deja pedir."""
    from . import aave_backtest as AB
    s0, s1 = pool["symbol0"] in B._STABLE, pool["symbol1"] in B._STABLE
    if s0 == s1:
        return None
    stable, asset = (pool["symbol0"], pool["symbol1"]) if s0 else (pool["symbol1"], pool["symbol0"])
    try:
        con = B._connect()
        try:
            a_st, n_st = AB._reserve(con, pool["chain"], pool["chain"], stable)
            a_as, n_as = AB._reserve(con, pool["chain"], pool["chain"], asset)
            q = """SELECT arg_max(supply_apy, hour), arg_max(borrow_apy, hour), arg_max(liq_threshold, hour),
                          arg_max(liq_bonus, hour), arg_max(ltv, hour),
                          arg_max(borrowing AND active AND NOT frozen AND NOT paused, hour), max(hour)
                   FROM aave_reserve_hour_v WHERE chain = ? AND market = ? AND asset = ?"""
            st = con.execute(q, [pool["chain"], pool["chain"], a_st]).fetchone()
            ast = con.execute(q, [pool["chain"], pool["chain"], a_as]).fetchone()
        finally:
            con.close()
    except Exception as e:                                       # noqa: BLE001 (la cobertura es un complemento)
        return {"error": f"Aave no tiene {stable} y {asset} en {pool['chain']} ({type(e).__name__})"}
    if not st or st[0] is None or not ast or ast[1] is None:
        return {"error": f"sin datos de Aave para {stable}/{asset} en {pool['chain']}"}
    if not st[2]:
        return {"error": f"{n_st} no cuenta como garantía en Aave {pool['chain']} (umbral 0)"}
    motivos = []
    if not st[4]:          # desde el 29-oct-2026 las estables tienen LTV 0 en Ethereum, Base y Arbitrum
        motivos.append(f"{n_st} tiene LTV 0 en Aave {pool['chain']}: cuenta para no ser liquidado, pero no deja "
                       "pedir prestado con ella")
    if not ast[5]:
        motivos.append(f"Aave {pool['chain']} no deja pedir {n_as} ahora")
    return {"estable": n_st, "activo": n_as, "apy_garantia": float(st[0]), "apy_prestamo": float(ast[1]),
            "umbral": float(st[2]), "bonus": float(st[3]), "ltv": float(st[4]), "hora": str(st[6]),
            "asset_token0": not s0, "disponible": not motivos, "motivos": motivos}


def _precio_activo(h: History, sp: np.ndarray, u1: np.ndarray, asset_token0: bool) -> np.ndarray:
    """Precio del activo en $ en cada punto de los caminos."""
    if asset_token0:
        return sp ** 2 * 10 ** (h.pool["dec0"] - h.pool["dec1"]) * u1
    return u1


def hf_historico(h: History, dias: int, asset_token0: bool) -> float:
    """HF inicial que habría aguantado la mayor subida del activo en cualquier tramo de `dias` del
    histórico (deuda = activo, garantía = estable; sin intereses): la mayor subida desde el inicio del tramo."""
    a = np.log(_precio_activo(h, h.df["sqrt_price"].to_numpy(float), h.df["usd1"].to_numpy(float), asset_token0))
    n = dias * 24 + 1
    if len(a) <= n:
        return 1.0
    fut_max = pd.Series(a[::-1]).rolling(n, min_periods=1).max().to_numpy()[::-1]
    return float(np.exp(np.max(fut_max - a)))


def _cubierta(h: History, paths: Paths, rg: Rango, c: float, capital: float, gas_tx: float, av: dict,
              hf: float) -> dict:
    """Pool + préstamo en cada camino (arrays de tamaño S)."""
    S_, H = paths.sp.shape[0], paths.sp.shape[1] - 1
    pa = _precio_activo(h, paths.sp, paths.usd1, av["asset_token0"])
    sp0, u10 = float(paths.sp[0, 0]), float(paths.usd1[0, 0])
    a0u, a1u = B._amounts(sp0, B._sqrt(rg.tl), B._sqrt(rg.tu))
    v0, v1 = a0u * sp0 ** 2 * u10, a1u * u10
    s = (v0 if av["asset_token0"] else v1) / (v0 + v1)
    u = av["umbral"] / hf
    V = capital / (1 + c * s * (1 / u - 1))
    D = c * s * V / u
    debt0 = c * s * V
    lp = simulate(h, paths, rg, V, gas_tx, cambio=1 - c)
    t = np.arange(H + 1) / (24 * 365)
    g = (1 + av["apy_garantia"]) ** t                       # crecimiento de la garantía
    b = (1 + av["apy_prestamo"]) ** t                       # crecimiento de la deuda (en unidades)
    coll = np.full(S_, D)                                   # garantía y deuda «escaladas» (valor al abrir)
    units = np.full(S_, debt0 / float(pa[0, 0]))
    liq = np.zeros(S_, bool)
    perdida_liq = np.zeros(S_)
    hf_min = np.full(S_, np.inf)
    if debt0 > 0:
        for i in range(H + 1):
            cu, du = coll * g[i], units * b[i] * pa[:, i]
            hfi = np.where(du > 0, cu * av["umbral"] / np.where(du > 0, du, 1), np.inf)
            bad = hfi < 1
            for _ in range(10):                             # liquidaciones de esa hora (como aave_backtest)
                if not bad.any():
                    break
                cf = np.where((hfi < 0.95) | (du < 2000) | (cu < 2000), 1.0, 0.5)
                repay = np.where(bad, cf * du, 0.0)
                seize = np.minimum(repay * (1 + av["bonus"]), cu)
                repay = seize / (1 + av["bonus"])
                coll = coll - seize / g[i]
                units = units - repay / (b[i] * pa[:, i])
                perdida_liq += seize - repay
                liq |= bad
                cu, du = coll * g[i], units * b[i] * pa[:, i]
                hfi = np.where(du > 1e-9, cu * av["umbral"] / np.where(du > 1e-9, du, 1), np.inf)
                bad = hfi < 1
            hf_min = np.minimum(hf_min, hfi)
    gas_aave = 4 * gas_tx if c > 0 else 0.0
    aave_final = coll * g[-1] - units * b[-1] * pa[:, -1]
    interes = debt0 * (b[-1] - 1) * pa[:, -1] / float(pa[0, 0])  # aproximado (sin liquidaciones)
    rend = D * (g[-1] - 1)
    return {"final": lp.final + aave_final - gas_aave, "V": V, "D": D, "deuda": debt0, "s": s, "lp": lp,
            "hf_min": hf_min, "liquidada": liq, "perdida_liq": perdida_liq, "interes": interes, "rend": rend,
            "gas_aave": gas_aave}


def cobertura(h: History, dias: int, rangos: list[Rango], paths: Paths, capital: float = 5000,
              gas: float | None = None, est_paths: Paths | None = None, est_info: list[dict] | None = None,
              coberturas: tuple = COBERTURAS) -> dict | None:
    """Cada rango sin cubrir, cubierto a medias y cubierto del todo, en los escenarios de la tabla (y en los
    de estrés). None si el par no es activo/estable; {"error": …} si Aave no lo permite hoy."""
    av = aave_hoy(h.pool)
    if av is None or "error" in av:
        return av
    gas_tx = GAS.get(h.pool["chain"], 0.05) if gas is None else gas
    hf_hist = hf_historico(h, dias, av["asset_token0"])
    hf = round(max(HF_COBERTURA, math.ceil((hf_hist + 0.1) * 10) / 10), 2)
    sin_riesgo = capital * (1 + av["apy_garantia"]) ** (dias / 365)
    out = []
    for rg in rangos:
        filas, base, base_est = [], None, None
        for c in coberturas:
            r = _cubierta(h, paths, rg, c, capital, gas_tx, av, hf)
            f = r["final"]
            base = f if base is None else base
            fila = {"cobertura": c, "valor_media": float(np.mean(f)), "valor_mediana": float(np.median(f)),
                    "valor_p10": float(np.percentile(f, 10)), "valor_p90": float(np.percentile(f, 90)),
                    "vs_sin_cubrir": float(np.mean(f - base)), "vs_sin_riesgo": float(np.mean(f) - sin_riesgo),
                    "prob_perdida": float(np.mean(f < capital)), "en_pool": r["V"], "deposito": r["D"],
                    "deuda": r["deuda"], "comisiones": float(np.mean(r["lp"].fees)),
                    "aave_neto": float(np.mean(r["rend"] - r["interes"])),
                    "hf_min_p5": float(np.percentile(r["hf_min"], 5)) if c > 0 else None,
                    "prob_liquidacion": float(np.mean(r["liquidada"])),
                    "perdida_liq_media": float(np.mean(r["perdida_liq"]))}
            if est_paths is not None:
                e = _cubierta(h, est_paths, rg, c, capital, gas_tx, av, hf)
                base_est = e["final"] if base_est is None else base_est
                fila["estres"] = [{"caso": inf["caso"], "resultado": float(e["final"][j] - capital),
                                   "liquidada": bool(e["liquidada"][j])} for j, inf in enumerate(est_info or [])]
            filas.append(fila)
        out.append({"texto": rg.texto, "filas": filas})
    return {"aave": av, "hf": hf, "hf_historico": hf_hist, "sin_riesgo": sin_riesgo, "rangos": out}


# --- estrés: ¿y si sale mal? --------------------------------------------------------------------

# (nombre, descripción, función del instante t∈[0,1] → precio del activo / precio de hoy)
CASOS_ESTRES = (
    ("Cae 30 %", "el precio baja un 30 % de forma continua hasta el final del plazo", lambda t: 0.7 ** t),
    ("Sube 30 %", "el precio sube un 30 % de forma continua hasta el final del plazo", lambda t: 1.3 ** t),
    ("Latigazo −20 %", "cae un 20 % hasta la mitad del plazo y vuelve al precio de hoy",
     lambda t: 0.8 ** (1 - abs(2 * t - 1))),
)


def _mov(x: float) -> str:
    return f"{x * 100:+.0f} %".replace("-", "−")


def _paths_estres(h: History, horas: int, k: float) -> tuple[Paths, list[dict]]:
    """Un camino por caso: los fijos de CASOS_ESTRES (comisiones por hora = la media del histórico × k) y
    el peor tramo real de `horas` del histórico del pool (sus movimientos y comisiones tal cual, × k)."""
    _, _, asset0 = B.pair_of(h.pool)
    sgn = 1 if asset0 else -1                      # precio del activo = P del pool o su inversa
    t = np.arange(horas + 1) / horas
    sps, u1s, q0s, q1s, info = [], [], [], [], []
    q0m, q1m = np.full(horas, h.q0.mean() * k), np.full(horas, h.q1.mean() * k)
    for nombre, desc, f in CASOS_ESTRES:
        fa = np.array([f(x) for x in t])           # precio del activo / hoy
        sps.append(h.sp_now * fa ** (sgn / 2))
        u1s.append(h.usd1_now * (np.ones_like(fa) if asset0 else fa))   # la moneda de cotización no se mueve
        q0s.append(q0m)
        q1s.append(q1m)
        info.append({"caso": nombre, "descripcion": desc, "real": False})
    m = len(h.ret_p)
    if m > horas:
        ra = sgn * h.ret_p                         # log-rendimientos del precio del activo
        c = np.concatenate([[0.0], np.cumsum(ra)])
        mov = c[horas:] - c[:-horas]               # movimiento de cada tramo de `horas`
        i = int(np.argmin(mov))
        lp = np.concatenate([[0.0], np.cumsum(h.ret_p[i:i + horas])])
        lu = np.concatenate([[0.0], np.cumsum(h.ret_u1[i:i + horas])])
        sps.append(h.sp_now * np.exp(lp / 2))
        u1s.append(h.usd1_now * np.exp(lu))
        q0s.append(h.q0[i:i + horas] * k)
        q1s.append(h.q1[i:i + horas] * k)
        desde = h.df["hour"].iloc[i]
        info.append({"caso": f"Peor tramo real ({horas // 24} d)",
                     "descripcion": f"el peor tramo de {horas // 24} días del histórico del pool, desde el "
                                    f"{desde:%d-%m-%Y}: el activo " + _mov(math.expm1(float(mov[i]))),
                     "real": True, "desde": str(desde.date()), "movimiento": math.expm1(float(mov[i]))})
    return Paths(np.vstack(sps), np.vstack(u1s), np.vstack(q0s), np.vstack(q1s)), info


def estres(h: History, dias: int, rangos: list[Rango], capital: float = 5000, gas: float | None = None,
           ajuste: float = AJUSTE_COMISIONES, abierta: bool = False) -> dict:
    """¿Y si sale mal? Cada rango en unos pocos caminos fijos (CASOS_ESTRES) y en el peor tramo real del
    histórico, con dónde deja de cobrar y cuánto vale la posición en cada borde. No es una previsión:
    son casos para ver la cola con nombre y cifra."""
    horas = dias * 24
    k = ajuste * h.nivel(NIVEL_DIAS) * factor_plazo(dias)
    paths, info = _paths_estres(h, horas, k)
    _, _, asset0 = B.pair_of(h.pool)
    out = []
    for rg in rangos:
        s = simulate(h, paths, rg, capital, gas, abierta=abierta)
        sa, sb = B._sqrt(rg.tl), B._sqrt(rg.tu)
        dentro = (paths.sp >= sa) & (paths.sp < sb)
        casos = []
        for j, inf in enumerate(info):
            fuera = np.flatnonzero(~dentro[j])
            sale = int(fuera[0]) if len(fuera) and dentro[j, 0] else None
            casos.append(inf | {"valor_final": float(s.final[j]), "resultado": float(s.final[j] - capital),
                                "vs_mantener": float(s.final[j] - s.hodl[j]), "comisiones": float(s.fees[j]),
                                "en_rango": float(s.in_range[j]),
                                "precio_final": _display(h.pool, float(paths.sp[j, -1]) ** 2),
                                "sale_dia": None if sale is None else sale / 24,
                                "sale_precio": None if sale is None else _display(h.pool, float(paths.sp[j, sale]) ** 2)})
        # valor de la posición (sin comisiones) justo en cada borde, frente a guardar los tokens
        bordes = Paths(np.array([[h.sp_now, sa], [h.sp_now, sb]]), np.full((2, 2), h.usd1_now), np.zeros((2, 1)),
                       np.zeros((2, 1)))
        if not asset0:                             # el token1 es el activo: su precio en $ va con el borde
            bordes.usd1[:, 1] = h.usd1_now * (h.sp_now / np.array([sa, sb])) ** 2
        sbd = simulate(h, bordes, rg, capital, gas, diluir=False, abierta=abierta)
        b = {}
        for j, raw in enumerate((sa, sb)):
            lado = ("abajo" if j == 0 else "arriba") if asset0 else ("arriba" if j == 0 else "abajo")
            b[lado] = {"precio": _display(h.pool, raw ** 2), "valor": float(sbd.final[j]),
                       "resultado": float(sbd.final[j] - capital), "vs_mantener": float(sbd.final[j] - sbd.hodl[j])}
        out.append({"texto": rg.texto, "lo": rg.lo, "hi": rg.hi, "casos": casos, "bordes": b})
    return {"rangos": out, "precio": price_now(h)}


def run(key: str, chain: str | None, rangos: list[str], capital: float = 5000, dias: int = 30,
        historia: int = HISTORIA_DIAS, escenarios: int = 1000, bloque: int = 168, tendencia: str = "neutral",
        gas: float | None = None, h: History | None = None, ajuste: float = AJUSTE_COMISIONES,
        nivel_dias: int | None = NIVEL_DIAS, exponente: float = EXPONENTE_PLAZO, escala: float | None = None) -> dict:
    h = h or load(key, chain, historia)
    paths = bootstrap(h, dias * 24, escenarios, bloque, tendencia, ajuste=ajuste, nivel_dias=nivel_dias,
                      exponente=exponente, escala=escala)
    rgs = [parse_rango(r, h, paths) for r in rangos]
    out = [resumen(simulate(h, paths, rg, capital, gas)) for rg in rgs]
    asset, quote, _ = B.pair_of(h.pool)
    ref = referencia_sin_riesgo(h.pool["chain"])
    if ref:
        ref["valor_final"] = capital * (1 + ref["apy"]) ** (dias / 365)
    fiab = fiabilidad(h, dias, calibracion_pool(h.pool["pool_id"], h.pool["chain"], dias))
    res = {"pool": {k: h.pool[k] for k in ("chain", "version", "label", "pool_id", "fee", "tick_spacing")},
            "par": f"{asset}/{quote}", "precio": price_now(h), "hora": str(h.hour_now), "tvl": h.tvl_now,
            "liquidez_activa": h.liquidity_now,
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
    res["etapa"] = etapa_del_par(h.pool, dias)
    res["reajuste"] = comparar_reajustes(h, dias, rgs, capital, gas, ajuste=ajuste)
    res["estres"] = estres(h, dias, rgs, capital, gas, ajuste=ajuste)
    res["salida"] = comparar_salidas(h, dias, rgs, paths, capital, gas, apy=ref["apy"] if ref else 0.0)
    ep, ei = _paths_estres(h, dias * 24, ajuste * h.nivel(nivel_dias) * factor_plazo(dias, exponente))
    res["cobertura"] = cobertura(h, dias, rgs, paths, capital, gas, ep, ei)
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
