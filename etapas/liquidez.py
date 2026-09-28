"""Liquidez concentrada (Uniswap v3 y Orca Whirlpools): rangos por perfil, idoneidad según la
etapa, ajuste a ticks, composición del depósito, pérdida frente a mantener y comisiones.

Toda la matemática sale de las fórmulas de Uniswap v3 (whitepaper, sección 6), que Orca usa
igual: precio = 1,0001^tick y, para una posición con liquidez L en el rango [a, b] con el
precio p dentro, cantidad del activo x = L·(1/√p − 1/√b) y de la moneda estable y = L·(√p − √a).
Aquí los precios son "humanos": moneda estable por unidad del activo (USDC por ETH).
"""
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .analysis import AssetResult, TimeframeResult
from .config import LP_CAPITAL_REF, LP_PROFILES, LP_QUANTILE, LP_MAX_POOL_DEVIATION, TIMEFRAMES
from .indicators import expected_range
from . import pools as _pools
from .pools import Pool, PoolState, pools_for

TICK_BASE = 1.0001
MIN_TICK, MAX_TICK = -887272, 887272

# ---------------------------------------------------------------- ticks


def human_to_raw(pool: Pool, price: float) -> float:
    """Precio humano (estable por activo) → precio del pool (token1 por token0, en unidades mínimas)."""
    p = price if pool.base_is_token0 else 1 / price
    return p * 10 ** (pool.dec1 - pool.dec0)


def raw_to_human(pool: Pool, raw: float) -> float:
    p = raw / 10 ** (pool.dec1 - pool.dec0)
    return p if pool.base_is_token0 else 1 / p


def snap_range(pool: Pool, low: float, high: float) -> tuple[float, float, int, int]:
    """Ajusta [low, high] a los ticks válidos del pool, redondeando hacia fuera (el rango nunca
    se estrecha). Devuelve (mín., máx., tick inferior, tick superior)."""
    t = sorted(math.log(human_to_raw(pool, x)) / math.log(TICK_BASE) for x in (low, high))
    s = pool.tick_spacing
    lower = max(math.floor(t[0] / s) * s, math.ceil(MIN_TICK / s) * s)
    upper = min(math.ceil(t[1] / s) * s, math.floor(MAX_TICK / s) * s)
    if upper <= lower:
        upper = lower + s
    prices = sorted(raw_to_human(pool, TICK_BASE ** k) for k in (lower, upper))
    return prices[0], prices[1], lower, upper


# ---------------------------------------------------------------- posición


def amounts_per_liquidity(p: float, a: float, b: float) -> tuple[float, float]:
    """(activo, estable) por unidad de liquidez en el rango [a, b] al precio p."""
    if p <= a:
        return 1 / math.sqrt(a) - 1 / math.sqrt(b), 0.0
    if p >= b:
        return 0.0, math.sqrt(b) - math.sqrt(a)
    return 1 / math.sqrt(p) - 1 / math.sqrt(b), math.sqrt(p) - math.sqrt(a)


def position_value(p: float, a: float, b: float, liquidity: float = 1.0) -> float:
    x, y = amounts_per_liquidity(p, a, b)
    return liquidity * (x * p + y)


def composition(p: float, a: float, b: float) -> float:
    """Fracción del valor depositado que va en el activo (el resto, en moneda estable)."""
    x, y = amounts_per_liquidity(p, a, b)
    return x * p / (x * p + y)


def divergence(p0: float, q: float, a: float, b: float) -> float:
    """Resultado de la posición frente a haber mantenido lo depositado, si el precio pasa de p0
    a q (sin contar comisiones). Negativo = pérdida («impermanent loss»)."""
    x0, y0 = amounts_per_liquidity(p0, a, b)
    return position_value(q, a, b) / (x0 * q + y0) - 1


def liquidity_for_capital(p: float, a: float, b: float, capital: float) -> float:
    """Liquidez (en unidades humanas) que se obtiene depositando `capital` en estable al precio p."""
    return capital / position_value(p, a, b)


# ---------------------------------------------------------------- idoneidad según la etapa

LEVELS = ["favorable", "precaución", "desfavorable"]
_STAGE_LEVEL = {1: 0, 3: 0, 2: 1, 4: 2}


def stage_effect(stage: int, base: str, quote: str) -> str:
    if stage in (1, 3):
        return ("Etapa lateral: el precio tiende a oscilar dentro de un rango, que es el escenario "
                "en el que la liquidez concentrada cobra más comisiones sin salirse.")
    if stage == 2:
        return (f"Tendencia alcista: si el precio sale por arriba, la posición queda 100 % en {quote} "
                f"(habrás vendido {base} durante la subida) y deja de cobrar comisiones. Rinde menos "
                f"que mantener {base}.")
    return (f"Tendencia bajista: si el precio sale por abajo, la posición queda 100 % en {base}, que "
            f"sigue cayendo, y deja de cobrar comisiones.")


def verdict(r: TimeframeResult, higher: TimeframeResult | None, base: str,
            quote: str = "USDC") -> tuple[str, list[str]]:
    """Idoneidad de abrir un rango en este marco: (nivel, motivos)."""
    if r.status != "ok":
        return "sin datos", [f"No hay etapa en el marco {r.label.lower()}: {r.message}"]
    level = _STAGE_LEVEL[r.stage]
    reasons = [f"Etapa {r.stage} ({r.stage_name}) en {r.label.lower()}. "
               + stage_effect(r.stage, base, quote)]
    if r.transition:
        target = int(r.transition[-1])
        new = math.ceil((level + _STAGE_LEVEL[target]) / 2)
        reasons.append(f"En transición {r.transition}: se promedia el riesgo de la etapa actual y el "
                       f"de la etapa {target}.")
        level = new
    if higher is not None and higher.status == "ok" and higher.stage == 4 and level == 0:
        level = 1
        reasons.append(f"El marco superior ({higher.label.lower()}) es bajista: una ruptura por abajo "
                       f"es más probable.")
    if r.confidence_label == "baja":
        reasons.append("La confianza en la etapa es baja: la lectura puede cambiar pronto.")
    return LEVELS[level], reasons


# ---------------------------------------------------------------- resultados


@dataclass
class PoolQuote:
    pool: Pool
    low: float | None = None              # rango ajustado a ticks
    high: float | None = None
    tick_lower: int | None = None
    tick_upper: int | None = None
    pool_price: float | None = None
    asset_share: float | None = None      # fracción del depósito en el activo, al precio del pool
    fee_day: float | None = None          # comisiones estimadas al día para LP_CAPITAL_REF
    fee_apr: float | None = None
    breakeven_days: float | None = None   # días de comisiones que cubren la pérdida en el peor borde
    volume_24h: float | None = None
    tvl: float | None = None
    error: str = ""


@dataclass
class ProfileResult:
    timeframe: str
    name: str                             # Diaria, Semanal, Mensual
    horizon: str
    level: str = "sin datos"
    reasons: list[str] = field(default_factory=list)
    center: float | None = None
    low: float | None = None
    high: float | None = None
    asset_share: float | None = None
    loss_low: float | None = None         # resultado frente a mantener si el precio llega al mínimo
    loss_high: float | None = None        # ... o al máximo
    levels_inside: list[str] = field(default_factory=list)
    quotes: list[PoolQuote] = field(default_factory=list)

    @property
    def main_quote(self) -> PoolQuote | None:
        """El pool con más comisiones estimadas para el capital de referencia."""
        ok = [q for q in self.quotes if q.fee_day is not None]
        return max(ok, key=lambda q: q.fee_day) if ok else None

    @property
    def width(self) -> tuple[float, float] | None:
        if self.low is None or not self.center:
            return None
        return self.low / self.center - 1, self.high / self.center - 1


@dataclass
class LiquidityResult:
    symbol: str
    price: float | None
    profiles: list[ProfileResult]
    pools: list[Pool]
    note: str = ""
    best: str = ""                        # recomendación de conjunto
    capital: float = LP_CAPITAL_REF

    def to_dict(self) -> dict:
        def q(x: PoolQuote) -> dict:
            return {"pool": x.pool.id, "dex": x.pool.dex, "red": x.pool.chain, "par": x.pool.pair,
                    "comision": x.pool.fee, "direccion": x.pool.address, "min_tick": _r(x.low),
                    "max_tick": _r(x.high), "tick_inferior": x.tick_lower, "tick_superior": x.tick_upper,
                    "precio_pool": _r(x.pool_price), "parte_activo": _r(x.asset_share),
                    "comisiones_dia": _r(x.fee_day), "apr_estimado": _r(x.fee_apr),
                    "dias_para_cubrir_borde": _r(x.breakeven_days), "volumen_24h": _r(x.volume_24h),
                    "tvl": _r(x.tvl), "error": x.error or None}
        return {"simbolo": self.symbol, "precio": _r(self.price), "capital_referencia": self.capital,
                "nota": self.note or None, "recomendacion": self.best,
                "perfiles": [{"perfil": p.name, "marco": p.timeframe, "horizonte": p.horizon,
                              "idoneidad": p.level, "motivos": p.reasons, "min": _r(p.low),
                              "max": _r(p.high), "parte_activo": _r(p.asset_share),
                              "resultado_en_min": _r(p.loss_low), "resultado_en_max": _r(p.loss_high),
                              "niveles_dentro": p.levels_inside, "pools": [q(x) for x in p.quotes]}
                             for p in self.profiles]}


def _r(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 6)


def lp_range(r: TimeframeResult, center: float) -> tuple[float, float] | None:
    """Rango de una vela completa desde `center`: cada extremo es el cuantil LP_QUANTILE de lo que
    se alejaron el máximo y el mínimo de las velas pasadas, en ATR (el mismo método, validado fuera
    de muestra, que el «rango extremo» del informe)."""
    if r.status != "ok" or r.candles is None or r.history is None:
        return None
    cfg = TIMEFRAMES[r.timeframe]
    low, high = expected_range(r.candles, r.history["atr"], LP_QUANTILE, cfg.range_lookback, center=center)
    if pd.isna(low) or pd.isna(high) or low <= 0:
        return None
    return float(low), float(high)


def quote_pool(pool: Pool, state: PoolState | None, low: float, high: float, market: float,
               capital: float) -> PoolQuote:
    """Rango ajustado a los ticks del pool y estimación de comisiones con su estado actual."""
    lo, hi, tl, tu = snap_range(pool, low, high)
    q = PoolQuote(pool, lo, hi, tl, tu)
    if state is None or state.price is None:
        q.error = state.error if state and state.error else "sin datos del pool"
        return q
    q.pool_price, q.volume_24h, q.tvl = state.price, state.volume_24h, state.tvl
    if abs(state.price / market - 1) > LP_MAX_POOL_DEVIATION:
        q.error = (f"el precio del pool ({state.price:,.2f}) se aleja más de un "
                   f"{LP_MAX_POOL_DEVIATION:.0%} del de mercado ({market:,.2f}); no se estima")
        return q
    p = state.price
    if not lo < p < hi:
        q.error = "el precio del pool queda fuera del rango"
        return q
    q.asset_share = composition(p, lo, hi)
    if state.volume_24h is None or not state.liquidity:
        q.error = state.error or "sin volumen o liquidez del pool: no se estiman comisiones"
        return q
    mine = liquidity_for_capital(p, lo, hi, capital) * 10 ** ((pool.dec0 + pool.dec1) / 2)
    share = mine / (state.liquidity + mine)
    q.fee_day = share * state.volume_24h * pool.fee * (1 - state.protocol_cut)
    q.fee_apr = q.fee_day * 365 / capital
    worst = min(divergence(p, lo, lo, hi), divergence(p, hi, lo, hi))
    if q.fee_day > 0:
        q.breakeven_days = -worst * capital / q.fee_day
    return q


def analyze_liquidity(asset: AssetResult, fetch=None,
                      capital: float = LP_CAPITAL_REF) -> LiquidityResult | None:
    """Perfiles diario, semanal y mensual para un criptoactivo (None para acciones)."""
    if asset.kind != "cripto" or asset.error or not asset.last_price:
        return None
    price = asset.last_price
    pools, note = pools_for(asset.symbol)
    states = (fetch or _pools.fetch_states)(pools)
    tfs = asset.timeframes
    higher = {"1d": "1w", "1w": "1M", "1M": None}
    profiles = []
    for key, name, horizon in LP_PROFILES:
        r = tfs.get(key)
        prof = ProfileResult(key, name, horizon, center=price)
        if r is None:
            profiles.append(prof)
            continue
        up = tfs.get(higher[key]) if higher[key] else None
        prof.level, prof.reasons = verdict(r, up, asset.symbol)
        rng = lp_range(r, price)
        if rng is None:
            prof.reasons.append("No hay historial suficiente para calcular el rango.")
            profiles.append(prof)
            continue
        prof.low, prof.high = rng
        prof.asset_share = composition(price, prof.low, prof.high)
        prof.loss_low = divergence(price, prof.low, prof.low, prof.high)
        prof.loss_high = divergence(price, prof.high, prof.low, prof.high)
        for label, lvl in (("confirma", r.confirm_level), ("invalida", r.invalid_level)):
            if lvl is not None and prof.low < lvl < prof.high:
                prof.levels_inside.append(f"nivel que {label} ({_fmt(lvl)})")
        prof.quotes = [quote_pool(p, states.get(p.id), prof.low, prof.high, price, capital) for p in pools]
        profiles.append(prof)
    res = LiquidityResult(asset.symbol, price, profiles, pools, note, capital=capital)
    res.best = recommendation(res)
    return res


def recommendation(res: LiquidityResult) -> str:
    """Frase de conjunto: qué perfil encaja mejor con las etapas actuales."""
    ok = [p for p in res.profiles if p.level in LEVELS and p.low is not None]
    if not ok:
        return "Sin datos suficientes para recomendar un perfil."
    fav = [p.name.lower() for p in ok if p.level == "favorable"]
    if fav:
        txt = "Perfiles con etapa favorable: " + ", ".join(fav) + "."
    elif all(p.level == "desfavorable" for p in ok):
        txt = "Ningún perfil es adecuado ahora: tendencia bajista en todos los marcos. Mejor esperar."
    else:
        txt = ("Ningún perfil es claramente favorable: si abres un rango, hazlo con precaución y "
               "vigilándolo más de lo habitual.")
    if not res.pools:
        txt += " " + res.note
    return txt


def _fmt(x: float) -> str:
    return f"{x:,.0f}" if x >= 1000 else f"{x:,.2f}" if x >= 1 else f"{x:.4g}"
