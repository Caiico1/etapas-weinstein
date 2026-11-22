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
AJUSTE_COMISIONES = 0.88


def historia_para(dias: int) -> int:
    """Días de histórico por defecto para un plazo. Medido el 22-nov-2026 (aperturas comunes, 17–20
    pools): 730 iguala o supera a 365 y a 1.095 en todos los plazos (P10–P90 a 7 d 83 % frente a 82 %;
    60 d 71 % frente a 68 %; 90 d 89 % frente a 85 %; 180 d 83 % frente a 78 %), así que no cambia."""
    return HISTORIA_DIAS


# Comisiones reales ÷ simuladas según el plazo (mismas calibraciones del 22-nov-2026): el nivel
# reciente se arrastra todo el plazo y a plazos largos sobrestima. Se muestra como aviso en pantalla.
SESGO_PLAZO = {60: 0.88, 90: 0.75, 180: 0.60}


def aviso_plazo(dias: int) -> str | None:
    largos = [d for d in SESGO_PLAZO if d <= dias]
    if not largos:
        return None
    d = max(largos)
    return (f"a {dias} días las comisiones simuladas han salido altas en el pasado: lo real fue ×"
            f"{SESGO_PLAZO[d]:.2f} de lo simulado a {d} días; aplica ese descuento a Comisiones y APR").replace(".", ",")


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
    return History(pool, df, 2 * np.log(sp[1:] / sp[:-1]), np.log(u1[1:] / u1[:-1]),
                   g0 * sp[1:], g1 / sp[1:], float(sp[-1]), float(u1[-1]),
                   float(np.median(recent)) if len(recent) else 0.0, fee, df["hour"].iloc[-1])


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
              nivel_dias: int | None = NIVEL_DIAS) -> Paths:
    m = len(h.ret_p)
    if m < 48:
        raise B.BacktestError("histórico demasiado corto para construir escenarios")
    bloque = max(1, min(bloque, m))
    rng = np.random.default_rng(seed)
    nb = math.ceil(horas / bloque)
    starts = rng.integers(0, m - bloque + 1, size=(escenarios, nb))
    idx = (starts[:, :, None] + np.arange(bloque)[None, None, :]).reshape(escenarios, -1)[:, :horas]
    rp, ru = h.ret_p[idx], h.ret_u1[idx]
    if tendencia == "neutral":
        rp = rp - h.ret_p.mean()
        ru = ru - h.ret_u1.mean()
    zero = np.zeros((escenarios, 1))
    sp = h.sp_now * np.exp(np.concatenate([zero, np.cumsum(rp, axis=1)], axis=1) / 2)
    u1 = h.usd1_now * np.exp(np.concatenate([zero, np.cumsum(ru, axis=1)], axis=1))
    k = ajuste * h.nivel(nivel_dias)
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
             diluir: bool = True) -> Sim:
    pool = h.pool
    d1 = 10 ** pool["dec1"]
    gas = GAS.get(pool["chain"], 0.05) if gas is None else gas
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
    swap = value * h.swap_fee * ((1 - share0) if stable0 else share0 if stable1 else 0.5)
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
        "valor_final": q(s.final), "vs_mantener": q(s.final - s.hodl), "comisiones": q(s.fees),
        "apr": med_fees / s.capital * ann, "rent": q(rent), "rent_anual": float(np.median(rent)) * ann,
        "en_rango": float(np.mean(s.in_range)), "en_rango_p": q(s.in_range),
        "prob_perdida": float(np.mean(s.final < s.capital)),
        "prob_gana_mantener": float(np.mean(s.final > s.hodl)),
        "acaba": {"dentro": float(np.mean(s.side == 0)), "debajo": float(np.mean(s.side < 0)),
                  "encima": float(np.mean(s.side > 0))},
    }


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


def run(key: str, chain: str | None, rangos: list[str], capital: float = 5000, dias: int = 30,
        historia: int = HISTORIA_DIAS, escenarios: int = 1000, bloque: int = 168, tendencia: str = "neutral",
        gas: float | None = None, h: History | None = None, ajuste: float = AJUSTE_COMISIONES,
        nivel_dias: int | None = NIVEL_DIAS) -> dict:
    h = h or load(key, chain, historia)
    paths = bootstrap(h, dias * 24, escenarios, bloque, tendencia, ajuste=ajuste, nivel_dias=nivel_dias)
    out = [resumen(simulate(h, paths, parse_rango(r, h, paths), capital, gas)) for r in rangos]
    asset, quote, _ = B.pair_of(h.pool)
    ref = referencia_sin_riesgo(h.pool["chain"])
    if ref:
        ref["valor_final"] = capital * (1 + ref["apy"]) ** (dias / 365)
    return {"pool": {k: h.pool[k] for k in ("chain", "version", "label", "pool_id", "fee", "tick_spacing")},
            "par": f"{asset}/{quote}", "precio": price_now(h), "hora": str(h.hour_now),
            "historia_desde": str(h.df["hour"].iloc[0]), "horas_historia": len(h.df),
            "capital": capital, "dias": dias, "escenarios": escenarios, "bloque_h": bloque,
            "tendencia": tendencia, "comision_cambio": h.swap_fee, "ajuste_comisiones": ajuste,
            "nivel_dias": nivel_dias, "nivel_reciente": h.nivel(nivel_dias),
            "gas": GAS.get(h.pool["chain"], 0.05) if gas is None else gas,
            "aviso": h.pool.get("aviso"), "aviso_plazo": aviso_plazo(dias), "rangos": out, "abanico": abanico(h, paths),
            "sin_riesgo": ref}


# --- calibración: ¿habría acertado en el pasado? ----------------------------------------------

def calibrar(key: str, chain: str | None, dias: int = 30, ancho: float = 0.10, historia: int = HISTORIA_DIAS,
             escenarios: int = 400, bloque: int = 168, paso: int = 7, tendencia: str = "neutral",
             ajuste: float = AJUSTE_COMISIONES, ultimos: int | None = None, hasta: str | None = None,
             full: History | None = None, nivel_dias: int | None = NIVEL_DIAS) -> dict:
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
                                        nivel_dias=nivel_dias), rg)
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
    return {"pool": pool["label"], "red": pool["chain"], "dias": dias, "ancho": ancho, "historia": historia,
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
    ap.add_argument("--calibrar", action="store_true")
    ap.add_argument("--ancho", type=float, default=10, help="calibración: ±%% del rango")
    ap.add_argument("--paso", type=int, default=7)
    ap.add_argument("--ultimos", type=int, help="calibración: solo aperturas de los últimos N días")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.calibrar:
            c = calibrar(a.pool, a.red, a.dias, a.ancho / 100, a.historia, paso=a.paso, tendencia=a.tendencia, ajuste=a.ajuste,
                         ultimos=a.ultimos, nivel_dias=a.nivel or None)
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
                  ajuste=a.ajuste, nivel_dias=a.nivel or None)
    except (B.BacktestError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    print(f"{res['pool']['label']} ({res['pool']['chain']} {res['pool']['version']}) · {res['par']} = "
          f"{_fmt_price(res['precio'])} a {res['hora']} · {res['escenarios']} escenarios a {res['dias']} días "
          f"(histórico desde {res['historia_desde'][:10]}, tendencia {res['tendencia']}, comisiones al nivel de "
          f"{res['nivel_dias'] or 0} días ×{res['nivel_reciente']:.2f} y ajuste {res['ajuste_comisiones']:.2f})")
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
