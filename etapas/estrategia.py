"""Liquidez cubierta: USDC en Aave V3 como garantía, el activo prestado y los dos en el pool.

    python -m etapas.oportunidad ETH/USDC --red base            # sección «Liquidez cubierta»
    python -m etapas.oportunidad ETH/USDC --ltv 0.30 --json

La estrategia, para un par activo/estable (ETH/USDC, BTC/USDC):
  1. Se deposita D de la estable en Aave V3 como garantía (sigue cobrando su APY de depósito).
  2. Se pide prestado el activo, B unidades: justo las que pide la posición de liquidez, así que al
     abrir la exposición al precio es nula (lo que tienes en el pool lo debes a Aave).
  3. Se aporta al pool B del activo y la parte en estable que pide el rango, del capital propio.
Con capital C, una posición de valor V con una fracción s en el activo y un LTV usado l:
  deuda = s·V, garantía D = s·V / l y C = D + (1 − s)·V  →  V = C / (s/l + 1 − s).
Factor de salud (Aave V3 Technical Paper, secc. 4.3): HF = Σ garantía × umbral de liquidación /
deuda = umbral / l al abrir. La garantía es estable y la deuda es del activo, así que solo hay
liquidación si el activo sube: al precio p × HF (el pool no cuenta como garantía en Aave).
Con la exposición cubierta, lo que se pierde frente a la cartera cubierta (deuda del activo +
tokens del pool) es la pérdida frente a mantener de la posición (IL), ya medida en el histórico
por oportunidad.backtest. Neto por ciclo = comisiones + IL − interés neto (préstamo − depósito) −
gas (pool y ajuste del préstamo) − cambio de tokens. Solo cobertura con LTV prudente: no se usa
E-Mode ni se apalanca.
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import aave as A
from .data import STABLES
from .liquidez import composition, position_value
from .oportunidad import (CYCLES, HORIZON_DAYS, L2_DATA_FEE_USD, REBALANCE_GAS, SAFETY_LOSS, Opportunity,
                          Report)

HEDGE_LTV = 0.35              # LTV usado por defecto: deuda / garantía al abrir
LTV_SHARE = 0.5               # ... y nunca más de la mitad del LTV máximo de la garantía
MIN_HEALTH = 2.0              # factor de salud inicial mínimo (liquidación con el activo al doble)
AAVE_TX_GAS = 300_000         # depositar, pedir prestado o devolver en Aave V3
LIQ_HORIZON = HORIZON_DAYS    # días sin reajustar el préstamo en la comprobación histórica


@dataclass
class LiquidationHistory:
    """Entrando cada día del histórico diario y sin tocar el préstamo LIQ_HORIZON días."""
    samples: int                          # días de entrada comprobados
    hits: int = 0                         # en cuántos se habría tocado el precio de liquidación
    worst_rise: float | None = None       # mayor subida desde la entrada dentro del plazo

    @property
    def share(self) -> float | None:
        return self.hits / self.samples if self.samples else None


@dataclass
class Hedge:
    opp: Opportunity
    collateral: str                       # token de garantía en Aave (USDC)
    debt: str                             # token prestado (WETH, cbBTC...)
    ltv_target: float
    ltv_used: float | None = None
    ltv_max: float | None = None
    threshold: float | None = None        # umbral de liquidación de la garantía
    bonus: float | None = None            # bonus de liquidación de la deuda
    supply_apy: float | None = None       # lo que cobra la garantía
    borrow_apy: float | None = None       # lo que cuesta la deuda
    lp_value: float | None = None         # V: valor de la posición en el pool ($)
    deposit: float | None = None          # D: estable depositada en Aave
    borrow_amount: float | None = None    # B: unidades del activo prestadas
    borrow_usd: float | None = None
    lp_stable: float | None = None        # estable propia que va al pool
    health: float | None = None
    liq_price: float | None = None        # precio del par al que HF < 1
    fees_cycle: float | None = None
    il_cycle: float | None = None         # resultado frente a la cartera cubierta (negativo = pérdida)
    interest_cycle: float | None = None   # interés del préstamo − rendimiento del depósito
    gas_cycle: float | None = None
    swap_cycle: float | None = None
    entry_gas: float | None = None        # depositar y pedir prestado al abrir
    net_cycle: float | None = None
    net_safe: float | None = None
    net_apr: float | None = None
    net_safe_apr: float | None = None
    history: LiquidationHistory | None = None
    scenarios: dict = field(default_factory=dict)   # {nombre: (precio, resultado frente al capital)}
    excluded: str = ""


def sizing(capital: float, share: float, ltv: float) -> tuple[float, float, float]:
    """(valor de la posición, garantía, deuda en $) para cubrir el activo de la posición."""
    value = capital / (share / ltv + 1 - share)
    debt = share * value
    return value, debt / ltv, debt


def liquidation_price(deposit: float, threshold: float, borrow_amount: float) -> float:
    """Precio del activo al que HF = deposit × umbral / (B × precio) baja de 1."""
    return deposit * threshold / borrow_amount


def liquidation_history(daily: pd.DataFrame | None, ratio: float,
                        horizon: int = LIQ_HORIZON) -> LiquidationHistory:
    """Cuántas veces, entrando al cierre de cada día y sin tocar el préstamo `horizon` días, el
    máximo diario llegó a `ratio` veces el precio de entrada (el precio de liquidación)."""
    if daily is None or len(daily) <= horizon:
        return LiquidationHistory(0)
    close, high = daily["close"].to_numpy(float), daily["high"].to_numpy(float)
    n = len(close) - horizon
    peak = np.array([high[i + 1:i + 1 + horizon].max() for i in range(n)]) / close[:n]
    return LiquidationHistory(n, int((peak >= ratio).sum()), float(peak.max() - 1))


def _reserve(chain: str, token: str):
    try:
        return A.reserve(chain, token), ""
    except (OSError, ValueError, KeyError, TypeError) as e:
        return None, f"sin datos de Aave V3 ({type(e).__name__})"


def _tx_usd(o: Opportunity, gas: int) -> float:
    """Coste de una transacción de `gas` unidades, con el precio del gas que ya usa el reajuste."""
    l2 = 0.0 if o.pool.chain == "ethereum" else L2_DATA_FEE_USD
    return (o.gas_usd - l2) * gas / REBALANCE_GAS + l2


def hedge(rep: Report, o: Opportunity, ltv: float = HEDGE_LTV,
          history: dict | None = None) -> Hedge | None:
    """Liquidez cubierta para una combinación de ciclo y pool. None si el par no es activo/estable."""
    toks = o.pool.pair.split("/")
    base_tok, quote_tok = (toks[0], toks[1]) if o.pool.base_is_token0 else (toks[1], toks[0])
    if rep.quote not in STABLES or quote_tok not in STABLES:
        return None
    h = Hedge(o, quote_tok, A.NATIVE_ALIAS.get(base_tok, base_tok), ltv)
    if o.pool.chain not in A.CHAINS:
        h.excluded = f"Aave V3 no está en {o.pool.chain.capitalize()}"
        return h
    if o.fees_cycle is None or o.expected_result is None or o.gas_usd is None:
        h.excluded = o.excluded or "faltan datos de la posición"
        return h
    col, err = _reserve(o.pool.chain, quote_tok)
    debt, err2 = _reserve(o.pool.chain, base_tok) if not err else (None, "")
    if err or err2:
        h.excluded = err or err2
        return h
    if col is None:
        h.excluded = f"{quote_tok} no está listado en Aave V3 en {o.pool.chain.capitalize()}"
        return h
    if debt is None:
        h.excluded = f"{h.debt} no está listado en Aave V3 en {o.pool.chain.capitalize()}"
        return h
    h.ltv_max, h.threshold, h.bonus = col.ltv, col.liquidation_threshold, debt.liquidation_bonus
    h.supply_apy, h.borrow_apy = col.supply_apy, debt.borrow_apy
    if col.ltv == 0 or not col.collateral:
        h.excluded = f"{quote_tok} no da poder de préstamo en Aave V3 (LTV 0)"
        return h
    h.ltv_used = min(ltv, LTV_SHARE * col.ltv)
    q, cap = o.quote, rep.capital
    share = composition(q.pool_price, q.low, q.high)
    h.lp_value, h.deposit, h.borrow_usd = sizing(cap, share, h.ltv_used)
    price_usd = q.pool_price * rep.quote_usd
    h.borrow_amount = h.borrow_usd / price_usd
    h.lp_stable = h.lp_value - h.borrow_usd
    h.health = h.threshold / h.ltv_used
    h.liq_price = liquidation_price(h.deposit, h.threshold, h.borrow_amount) / rep.quote_usd
    # Comisiones, pérdida y cambio de tokens escalan con el tamaño de la posición
    scale = h.lp_value / cap
    h.fees_cycle, h.il_cycle = o.fees_cycle * scale, o.expected_result * h.lp_value
    h.swap_cycle = o.swap_cycle * scale
    h.interest_cycle = (h.borrow_usd * h.borrow_apy - h.deposit * h.supply_apy) * o.days / 365
    h.gas_cycle = o.gas_usd + _tx_usd(o, AAVE_TX_GAS)          # reajustar el pool y la deuda
    h.entry_gas = 2 * _tx_usd(o, AAVE_TX_GAS)
    fixed = h.interest_cycle + h.gas_cycle + h.swap_cycle
    h.net_cycle = h.fees_cycle + h.il_cycle - fixed
    h.net_safe = h.fees_cycle + SAFETY_LOSS * h.il_cycle - fixed
    h.net_apr, h.net_safe_apr = (x / cap * 365 / o.days for x in (h.net_cycle, h.net_safe))
    key = (o.pool.chain, round(h.health, 6))
    if history is not None and key in history:
        h.history = history[key]
    else:
        daily = rep.asset.timeframes["1d"].candles if rep.asset and rep.asset.timeframes["1d"].status == "ok" else None
        h.history = liquidation_history(daily, h.health)
        if history is not None:
            history[key] = h.history
    v0 = position_value(q.pool_price, q.low, q.high)
    for label, p in (("min", q.low), ("max", q.high), ("liquidacion", h.liq_price)):
        lp = h.lp_value * position_value(p, q.low, q.high) / v0
        h.scenarios[label] = (p, (lp + h.deposit - h.borrow_amount * p * rep.quote_usd) / cap - 1)
    # Vetos, de disponibilidad a rentabilidad
    if col.isolated:
        h.excluded = f"{quote_tok} está en Modo de Aislamiento en Aave V3: solo permite pedir estables"
    elif not col.can_supply:
        h.excluded = f"Aave V3 no admite depósitos de {quote_tok} ({A.status(col)})"
    elif col.supply_cap is not None and col.supply_cap - col.supplied < h.deposit:
        h.excluded = f"límite de depósito de {quote_tok} alcanzado en Aave V3"
    elif not (debt.borrowing and debt.active) or debt.frozen or debt.paused:
        h.excluded = f"{h.debt} no se puede pedir prestado en Aave V3 ({A.status(debt)})"
    elif debt.borrow_cap is not None and debt.borrow_cap - debt.borrowed < h.borrow_amount:
        h.excluded = f"límite de deuda de {h.debt} alcanzado en Aave V3"
    elif h.health < MIN_HEALTH:
        h.excluded = f"factor de salud inicial {h.health:.2f}, por debajo de {MIN_HEALTH:g}"
    elif h.net_cycle <= 0:
        h.excluded = "resultado neto estimado negativo"
    elif h.net_safe_apr <= h.supply_apy:
        h.excluded = f"rinde menos que depositar el {quote_tok} en Aave"
    return h


def evaluate(rep: Report, ltv: float = HEDGE_LTV) -> list[Hedge]:
    """Liquidez cubierta para todas las combinaciones del análisis (vacío si el par no es activo/estable)."""
    if rep.error or rep.quote not in STABLES:
        return []
    cache: dict = {}
    return [h for o in rep.opportunities if (h := hedge(rep, o, ltv, cache)) is not None]


def best(hedges: list[Hedge]) -> Hedge | None:
    """La cubierta válida con mejor neto o, si no hay, la de mejor neto entre las calculadas."""
    ok = [h for h in hedges if not h.excluded]
    pool = ok or [h for h in hedges if h.net_apr is not None]
    return max(pool, key=lambda h: h.net_apr) if pool else None


# ---------------------------------------------------------------- salida


def _usd(x: float | None) -> str:
    return "—" if x is None else f"{x:,.2f} $"


def _pct(x: float | None, sign: bool = True) -> str:
    return "—" if x is None else (f"{x:+.1%}" if sign else f"{x:.1%}")


def _apr2(x: float | None) -> str:
    return "—" if x is None else f"{x:.2%}"


def _num(x: float) -> str:
    return f"{x:,.2f}" if abs(x) >= 1000 else f"{x:,.4f}" if abs(x) >= 1 else f"{x:.6g}"


def history_txt(h: Hedge) -> str:
    hs = h.history
    if hs is None or not hs.samples:
        return "Sin histórico diario para comprobarlo"
    return (f"En el histórico diario ({hs.samples} días de entrada, {LIQ_HORIZON} días sin tocar el préstamo) se "
            f"habría tocado {hs.hits} veces ({hs.share:.1%}); la mayor subida en ese plazo fue {hs.worst_rise:+.0%}")


def short_line(rep: Report, hedges: list[Hedge]) -> list[str]:
    """Una línea para la respuesta corta."""
    h = best(hedges)
    if h is None:
        return []
    o = h.opp
    head = (f"- **Liquidez cubierta** ({h.collateral} en Aave V3 como garantía, {h.debt} prestado): "
            if not h.excluded else f"- **Liquidez cubierta: no se propone** ({h.excluded}). ")
    return [head + f"{'Mejor' if h.excluded else 'mejor'} combinación {o.pool.label}, ciclo {o.name.lower()}: neto {_pct(h.net_apr)} anual "
            f"({_pct(h.net_safe_apr)} con el margen de seguridad) frente a {_apr2(h.supply_apy)} del {h.collateral} "
            f"en Aave; liquidación si {rep.base} sube a {_num(h.liq_price)} {rep.quote} "
            f"({h.liq_price / o.quote.pool_price - 1:+.0%}, factor de salud {h.health:.2f})."]


def render(rep: Report, hedges: list[Hedge]) -> list[str]:
    if not hedges:
        return []
    b, q = rep.base, rep.quote
    out = [f"## Liquidez cubierta: {q} en Aave V3 como garantía y {b} prestado", "",
           f"Depositas {q} en Aave V3, pides prestado el {b} que necesita la posición y lo aportas al pool con la "
           f"parte en {q}: lo que tienes de {b} en el pool lo debes a Aave, así que el precio no te afecta al abrir. "
           f"Ganas las comisiones y el depósito; pagas el préstamo y la pérdida frente a mantener (IL), que ya no "
           f"queda compensada por la subida del activo. LTV prudente ({rep.hedge_ltv:.0%}, como mucho la mitad "
           f"del máximo), sin E-Mode ni apalancamiento.", ""]
    h = best(hedges)
    if h is not None and h.net_apr is not None:
        o = h.opp
        out += [f"**{'Mejor combinación' if not h.excluded else 'La que mejor sale (no se propone: ' + h.excluded + ')'}"
                f": ciclo {o.name.lower()} en {o.pool.label}**", "",
                f"- **Garantía**: deposita **{_usd(h.deposit)}** de {h.collateral} en Aave V3 en "
                f"{o.pool.chain.capitalize()} (cobra {_apr2(h.supply_apy)} anual).",
                f"- **Préstamo**: pide **{_num(h.borrow_amount)} {h.debt}** ({_usd(h.borrow_usd)}, tipo variable "
                f"{_apr2(h.borrow_apy)} anual): LTV usado {h.ltv_used:.0%} de un máximo de {h.ltv_max:.0%}.",
                f"- **Pool**: aporta esos {_num(h.borrow_amount)} {h.debt} y {_usd(h.lp_stable)} de {h.collateral} al "
                f"rango {_num(o.quote.low)} – {_num(o.quote.high)} {q} (posición de {_usd(h.lp_value)}). No hay que "
                f"comprar {b}: sale del préstamo.",
                f"- **Factor de salud inicial {h.health:.2f}** (umbral de liquidación {h.threshold:.0%} / LTV "
                f"{h.ltv_used:.0%}). **Liquidación si {b} sube a {_num(h.liq_price)} {q}** "
                f"({h.liq_price / o.quote.pool_price - 1:+.0%}); el liquidador se queda la garantía con un "
                f"{h.bonus:.1%} de descuento. La deuda crece con el interés y baja algo ese precio. "
                + history_txt(h) + ".",
                f"- **Por ciclo**: comisiones {_usd(h.fees_cycle)}, pérdida frente a la cartera cubierta "
                f"{_usd(h.il_cycle)}, interés neto {_usd(h.interest_cycle)} (préstamo − depósito), gas "
                f"{_usd(h.gas_cycle)} (pool y ajuste del préstamo), cambio de tokens {_usd(h.swap_cycle)} → "
                f"**neto {_usd(h.net_cycle)}** ({_pct(h.net_apr)} anual sobre {rep.capital:,.0f} $). Con la pérdida "
                f"un {SAFETY_LOSS - 1:.0%} peor: {_usd(h.net_safe)} ({_pct(h.net_safe_apr)} anual) frente a "
                f"{_apr2(h.supply_apy)} de dejar el {h.collateral} en Aave. Abrir: gas de Aave {_usd(h.entry_gas)}.",
                f"- **Escenarios sin comisiones** (resultado frente al capital): en el mínimo del rango "
                f"{_pct(h.scenarios['min'][1])}; en el máximo {_pct(h.scenarios['max'][1])}; en el precio de "
                f"liquidación {_pct(h.scenarios['liquidacion'][1])}. Por encima del rango la posición queda en "
                f"{q} y la deuda en {b} sigue subiendo: hay que reajustar o cerrar.", ""]
    out += ["| Ciclo | Pool | Garantía | Préstamo | HF | Liquidación | Comisiones/ciclo | IL/ciclo | Interés neto | "
            "Gas + swap | Neto/ciclo | Neto anual (con margen) | Estado |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    order = {k: i for i, (k, _, _) in enumerate(CYCLES)}
    for x in sorted(hedges, key=lambda x: (x.net_apr is None, -(x.net_apr or 0), order[x.opp.cycle])):
        o = x.opp
        calc = x.net_apr is not None
        liq = f"{_num(x.liq_price)} ({x.liq_price / o.quote.pool_price - 1:+.0%})" if calc else "—"
        state = "válida" if not x.excluded else x.excluded
        if x is h and not x.excluded:
            state = "**mejor**"
        out.append(f"| {o.name} | {o.pool.label} | {_usd(x.deposit)} | "
                   + (f"{_num(x.borrow_amount)} {x.debt}" if calc else "—")
                   + f" | {'—' if x.health is None else f'{x.health:.2f}'} | {liq} | {_usd(x.fees_cycle)} | "
                   f"{_usd(x.il_cycle)} | {_usd(x.interest_cycle)} | "
                   f"{_usd(None if not calc else x.gas_cycle + x.swap_cycle)} | {_usd(x.net_cycle)} | "
                   f"{_pct(x.net_apr)} ({_pct(x.net_safe_apr)}) | {state} |")
    out += ["", f"Neto = comisiones + IL − interés neto − gas − cambio de tokens, sobre el capital. Vetos: neto con "
                f"margen por debajo del {q} en Aave, factor de salud inicial < {MIN_HEALTH:g}, límite de depósito o "
                f"de deuda alcanzado, garantía sin poder de préstamo (LTV 0) o en Modo de Aislamiento. Riesgos: "
                f"liquidación si el activo sube mucho sin reajustar, tipos variables (el préstamo puede encarecerse), "
                f"oráculos y contratos de Aave y Uniswap, y cambios de parámetros por la gobernanza.", ""]
    return out


def to_json(rep: Report, hedges: list[Hedge]) -> list[dict]:
    def one(h: Hedge) -> dict:
        o = h.opp
        return {"ciclo": o.name, "pool": o.pool.label, "red": o.pool.chain, "garantia_token": h.collateral,
                "deuda_token": h.debt, "ltv_objetivo": h.ltv_target, "ltv_usado": h.ltv_used, "ltv_maximo": h.ltv_max,
                "umbral_liquidacion": h.threshold, "bonus_liquidacion": h.bonus, "apy_deposito": h.supply_apy,
                "apy_prestamo": h.borrow_apy, "valor_posicion": h.lp_value, "garantia": h.deposit,
                "prestamo_cantidad": h.borrow_amount, "prestamo_usd": h.borrow_usd, "estable_al_pool": h.lp_stable,
                "min": o.quote.low, "max": o.quote.high, "factor_salud": h.health, "precio_liquidacion": h.liq_price,
                "comisiones_ciclo": h.fees_cycle, "il_ciclo": h.il_cycle, "interes_neto_ciclo": h.interest_cycle,
                "gas_ciclo": h.gas_cycle, "swap_ciclo": h.swap_cycle, "gas_entrada": h.entry_gas,
                "neto_ciclo": h.net_cycle, "neto_ciclo_con_margen": h.net_safe, "neto_anual": h.net_apr,
                "neto_anual_con_margen": h.net_safe_apr,
                "historico_liquidacion": None if h.history is None or not h.history.samples else {
                    "dias_entrada": h.history.samples, "veces_tocado": h.history.hits,
                    "fraccion": h.history.share, "dias_sin_tocar": LIQ_HORIZON,
                    "mayor_subida": h.history.worst_rise},
                "escenarios": {k: {"precio": p, "resultado": r} for k, (p, r) in h.scenarios.items()},
                "excluida": h.excluded or None}
    return [one(h) for h in hedges]

