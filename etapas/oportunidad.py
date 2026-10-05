"""Mejores oportunidades de liquidez concentrada para un par (ETH/USDC, ETH/BTC...).

    python -m etapas.oportunidad ETH/USDC --capital 5000
    python -m etapas.oportunidad ETH/BTC --capital 5000 --json

Para cada ciclo (diario, semanal, mensual) y cada pool verificado del par:
  1. Idoneidad según la etapa de Weinstein del par en ese marco (liquidez.verdict).
  2. Rango con el método validado del proyecto (liquidez.lp_range), ajustado a los ticks del pool.
  3. Comportamiento histórico del mismo método en este par, fuera de muestra: fracción de ciclos en
     que el precio no salió del rango y resultado medio frente a mantener los tokens.
  4. Comisiones para el capital: lo que cobró de verdad cada unidad de liquidez del pool en los
     últimos 7 y 30 días (el menor), leído de la blockchain (pools.realized_fees). Si la red no tiene nodo con
     histórico (Solana) o falla, estimación con la mediana del volumen diario de 30 días y la
     liquidez activa actual, ya descontada la comisión del protocolo.
  5. Gas de un reajuste por ciclo.
Resultado neto por ciclo = comisiones × fracción de ciclos dentro + resultado medio frente a
mantener × capital − gas. Se proponen las dos mejores combinaciones (de ciclos distintos si
es posible) entre las que no tienen etapa desfavorable y dan un resultado neto positivo.
"""
import argparse
import json
import math
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import pools as P
from .analysis import AssetResult, TimeframeResult, analyze_symbol
from .config import DISCLAIMER, LP_MIN_TVL, LP_MIN_VOLUME, LP_NO_DAILY_ON, LP_QUANTILE, TIMEFRAMES
from .data import STABLES, DataError, canonical_pair, fetch_candles, fetch_pair_candles
from .descubrir import discover, supported_symbols
from .indicators import expected_range
from .liquidez import PoolQuote, composition, divergence, lp_range, quote_pool, verdict
from .pools import Pool

CYCLES = [("1d", "Diario", 1.0), ("1w", "Semanal", 7.0), ("1M", "Mensual", 30.44)]
HIGHER = {"1d": "1w", "1w": "1M", "1M": None}
BACKTEST = {"1d": 365, "1w": 104, "1M": 48}          # ciclos pasados que se comprueban
MIN_BACKTEST = 24
MIN_TVL = LP_MIN_TVL                                  # con menos, 5.000 $ pesan demasiado en el pool
MIN_VOLUME = LP_MIN_VOLUME                            # volumen diario mínimo (mediana 30 días)
REBALANCE_GAS = 900_000                               # retirar + cobrar + cambiar + abrir (Uniswap v3)
L2_DATA_FEE_USD = 0.05                                # margen por la tarifa de datos de L1 en Base/Arbitrum
SOLANA_REBALANCE_SOL = 0.001                          # comisiones de transacción y prioridad en Solana
NO_DAILY_ON = LP_NO_DAILY_ON                          # el ciclo diario no se propone en Ethereum (gas)
# Margen de seguridad: la pérdida frente a mantener, medida por anchura del rango, varía hasta un
# 20-30 % entre mitades del histórico (BTC, SOL). Solo se propone lo que sigue siendo rentable con
# una pérdida un 25 % peor que la estimada.
SAFETY_LOSS = 1.25
REAL_FEE_DAYS = (7, 30)                               # ventanas de comisiones reales; se usa la menor
ALIASES = {"WETH": "ETH", "WBTC": "BTC", "CBBTC": "BTC", "WSOL": "SOL", "USDT0": "USDT"}
# Activos sin histórico suficiente que siguen el mismo precio que otro: XAUt y PAXG son una onza
# de oro cada uno (XAUT cotiza en Binance solo desde marzo de 2026).
PRICE_PROXY = {"XAUT": "PAXG"}


@dataclass
class Backtest:
    inside: float | None          # fracción de ciclos en que el precio no salió del rango
    mean_result: float | None     # resultado medio frente a mantener (negativo = pérdida)
    samples: int
    per_width: float | None = None   # resultado medio por unidad de semianchura del rango (log)

    def expected(self, low: float, high: float) -> float | None:
        """Resultado esperado frente a mantener para un rango [low, high]. El método ajusta la
        anchura del rango a la volatilidad de cada momento, así que el resultado es proporcional a
        la anchura: se reescala la media histórica a la anchura actual, en lugar de mezclar la
        volatilidad de todo el histórico con las comisiones de hoy."""
        return None if self.per_width is None else self.per_width * math.log(high / low) / 2


@dataclass
class Opportunity:
    cycle: str
    name: str
    days: float
    level: str
    reasons: list[str]
    pool: Pool
    quote: PoolQuote
    backtest: Backtest
    gas_usd: float | None = None
    expected_result: float | None = None   # resultado esperado frente a mantener, con el rango actual
    fees_cycle: float | None = None
    net_cycle: float | None = None
    net_safe: float | None = None          # neto con la pérdida un SAFETY_LOSS peor
    net_apr: float | None = None
    base_amount: float | None = None
    quote_amount: float | None = None
    excluded: str = ""


@dataclass
class Report:
    base: str
    quote: str
    capital: float
    price: float | None
    quote_usd: float | None
    asset: AssetResult | None
    stages: dict = field(default_factory=dict)
    ranges: dict = field(default_factory=dict)
    backtests: dict = field(default_factory=dict)
    opportunities: list[Opportunity] = field(default_factory=list)
    proposals: list[Opportunity] = field(default_factory=list)
    illiquid: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------- par


def parse_pair(text: str) -> tuple[str, str, list[str]]:
    """'eth/usdc' → ('ETH', 'USDC'). Admite '/', '-' y espacios, y nombres de tokens envueltos
    (WETH, WBTC, cbBTC). Con la estable delante (USDC/ETH) se da la vuelta: el precio se expresa
    siempre en la moneda estable."""
    parts = [x for x in text.upper().replace("-", "/").replace(" ", "/").split("/") if x]
    if len(parts) != 2:
        raise ValueError(f"'{text}' no es un par: escribe dos símbolos, por ejemplo ETH/USDC")
    base, quote = (ALIASES.get(x, x) for x in parts)
    notes = []
    if base in STABLES and quote not in STABLES:
        base, quote = quote, base
        notes.append(f"Par expresado como {base}/{quote} (precio en la moneda estable)")
    if base == quote or (base in STABLES and quote in STABLES):
        raise ValueError("Pares entre dos estables o de un token consigo mismo no se analizan: las "
                         "etapas de Weinstein no tienen sentido en ellos")
    unknown = [x for x in (base, quote) if x not in supported_symbols()]
    if unknown:
        raise ValueError(f"Sin tokens verificados para {', '.join(unknown)}. Disponibles: "
                         f"{', '.join(supported_symbols())} (se añaden en etapas/descubrir.py)")
    return base, quote, notes


# ---------------------------------------------------------------- piezas


def backtest(r: TimeframeResult, samples: int) -> Backtest:
    """Aplica el método del rango a cada uno de los últimos `samples` ciclos con los datos que había
    al empezar cada uno (fuera de muestra) y mide qué pasó en ese ciclo."""
    if r.status != "ok" or r.candles is None or r.history is None:
        return Backtest(None, None, 0)
    cfg = TIMEFRAMES[r.timeframe]
    df, atr = r.candles, r.history["atr"]
    inside, results, scaled = [], [], []
    for t in range(max(0, len(df) - 1 - samples), len(df) - 1):
        lo, hi = expected_range(df.iloc[:t + 1], atr.iloc[:t + 1], LP_QUANTILE, cfg.range_lookback)
        if pd.isna(lo) or pd.isna(hi) or lo <= 0:
            continue
        c, nxt = df["close"].iloc[t], df.iloc[t + 1]
        inside.append(nxt["low"] >= lo and nxt["high"] <= hi)
        results.append(divergence(c, nxt["close"], lo, hi))
        scaled.append(results[-1] / (math.log(hi / lo) / 2))
    if len(inside) < MIN_BACKTEST:
        return Backtest(None, None, len(inside))
    return Backtest(float(np.mean(inside)), float(np.mean(results)), len(inside), float(np.mean(scaled)))


def gas_costs(chains: set[str]) -> tuple[dict[str, float], list[str]]:
    """Coste en dólares de un reajuste en cada red, con el precio del gas actual."""
    costs, warnings = {}, []
    prices = {}
    for sym in ("ETH", "SOL"):
        try:
            c = fetch_candles(sym, TIMEFRAMES["1d"])
            prices[sym] = float(c.current["close"] if c.current is not None else c.df["close"].iloc[-1])
        except DataError:
            pass
    for chain in chains:
        try:
            if chain == "solana":
                costs[chain] = SOLANA_REBALANCE_SOL * prices["SOL"]
            else:
                gwei = int(P._rpc(chain, "eth_gasPrice", []), 16) / 1e9
                costs[chain] = REBALANCE_GAS * gwei * 1e-9 * prices["ETH"]
                if chain != "ethereum":
                    costs[chain] += L2_DATA_FEE_USD
        except (OSError, ValueError, KeyError, TypeError):
            warnings.append(f"Sin precio del gas en {chain}: no se descuenta")
    return costs, warnings


def _quote_usd(quote: str) -> float:
    if quote in STABLES:
        return 1.0
    c = fetch_candles(quote, TIMEFRAMES["1d"])
    return float(c.current["close"] if c.current is not None else c.df["close"].iloc[-1])


# ---------------------------------------------------------------- análisis completo


def analyze(pair: str, capital: float = 5000.0, chains=None) -> Report:
    """Analiza un par. `chains` limita los pools a esas redes (por ejemplo, ["base"])."""
    try:
        base, quote, notes = parse_pair(pair)
    except ValueError as e:
        return Report(pair, "", capital, None, None, None, error=str(e))
    cb, cq = canonical_pair(base, quote)
    if (cb, cq) != (base, quote):
        notes.append(f"Se analiza como {cb}/{cq}, la orientación en que cotiza el par: es el mismo pool y "
                     f"el mismo rango. En la propuesta tienes también los precios en {base}/{quote}.")
        base, quote = cb, cq
    rep = Report(base, quote, capital, None, None, None, warnings=list(notes))
    series = PRICE_PROXY.get(base, base)
    if series != base:
        rep.warnings.append(f"Etapas y rangos de {base} calculados con el histórico de {series}, que sigue "
                            f"el mismo precio y tiene más años de datos")
    asset = analyze_symbol(base, fetch=lambda t, cfg: fetch_pair_candles(series, quote, cfg))
    if asset.error or not asset.last_price:
        rep.error = f"No hay histórico de precios de {base}/{quote}: {asset.error}"
        return rep
    rep.asset, rep.price = asset, asset.last_price
    try:
        rep.quote_usd = _quote_usd(quote)
    except DataError as e:
        rep.error = f"No hay precio en dólares de {quote}: {e}"
        return rep

    for key, name, _ in CYCLES:
        r = asset.timeframes[key]
        up = asset.timeframes.get(HIGHER[key]) if HIGHER[key] else None
        rep.stages[key] = verdict(r, up, base, quote)
        rep.ranges[key] = lp_range(r, rep.price)
        rep.backtests[key] = backtest(r, BACKTEST[key])

    found, warn = discover(base, quote)
    rep.warnings += warn
    if chains:
        found = [p for p in found if p.chain in chains]
        rep.warnings.append("Solo se evalúan pools en: " + ", ".join(c.capitalize() for c in chains))
    if not found:
        rep.warnings.append(f"No hay pools de Uniswap v3, v4 ni de Orca para {base}/{quote} con los tokens verificados")
        return rep
    states = P.fetch_states(found)
    liquid = []
    for pool in found:
        st = states.get(pool.id)
        if st is None or st.price is None:
            rep.illiquid.append(f"{pool.label}: sin datos ({st.error if st else 'sin estado'})")
            continue
        if st.tvl is None or st.tvl < MIN_TVL:
            rep.illiquid.append(f"{pool.label}: TVL {'desconocido' if st.tvl is None else f'{st.tvl / 1e3:,.0f} k$'}")
            continue
        vol = P.median_volume(pool)
        if vol is None or vol < MIN_VOLUME:
            rep.illiquid.append(f"{pool.label}: volumen diario {'desconocido' if vol is None else f'{vol / 1e3:,.0f} k$'}")
            continue
        base_usd = rep.price * rep.quote_usd
        usd0, usd1 = (base_usd, rep.quote_usd) if pool.base_is_token0 else (rep.quote_usd, base_usd)
        # Prudencia: el menor entre lo cobrado a 7 y a 30 días (una semana buena no se extrapola)
        real = [r for r in (P.realized_fees(pool, usd0, usd1, d) for d in REAL_FEE_DAYS) if r is not None]
        liquid.append((pool, st, vol, min(real) if real else None))
    unknown = sum(1 for p in found if (st := states.get(p.id)) and st.price is not None and st.tvl is None)
    if unknown:
        rep.warnings.append(f"GeckoTerminal no devolvió el TVL de {unknown} pools: el análisis puede estar "
                            f"incompleto; vuelve a ejecutarlo en unos minutos")
    gas, warn = gas_costs({p.chain for p, _, _, _ in liquid})
    rep.warnings += warn

    for key, name, days in CYCLES:
        level, reasons = rep.stages[key]
        rng, bt = rep.ranges[key], rep.backtests[key]
        for pool, st, vol, real in liquid:
            q = (quote_pool(pool, st, rng[0], rng[1], rep.price, capital, rep.quote_usd, vol, real)
                 if rng else PoolQuote(pool, error="sin rango: historial insuficiente"))
            o = Opportunity(key, name, days, level, reasons, pool, q, bt, gas.get(pool.chain))
            if rng is None:
                o.excluded = "sin rango: historial insuficiente"
            elif key == "1d" and pool.chain in NO_DAILY_ON:
                o.excluded = "el ciclo diario no se propone en Ethereum (coste del gas)"
            elif q.fee_day is None:
                o.excluded = q.error or "sin estimación de comisiones"
            elif bt.inside is None:
                o.excluded = f"historial insuficiente para comprobar el método ({bt.samples} ciclos)"
            elif o.gas_usd is None:
                o.excluded = "sin precio del gas: no se puede calcular el neto"
            else:
                o.fees_cycle = q.fee_day * days * bt.inside
                o.expected_result = bt.expected(q.low, q.high)
                o.net_cycle = o.fees_cycle + o.expected_result * capital - o.gas_usd
                o.net_safe = o.fees_cycle + SAFETY_LOSS * o.expected_result * capital - o.gas_usd
                o.net_apr = o.net_cycle / capital * 365 / days
                share = composition(q.pool_price, q.low, q.high)
                o.base_amount = capital * share / (q.pool_price * rep.quote_usd)
                o.quote_amount = capital * (1 - share) / rep.quote_usd
                if level == "desfavorable":
                    o.excluded = "etapa desfavorable"
                elif level == "sin datos":
                    o.excluded = "sin etapa"
                elif o.net_cycle <= 0:
                    o.excluded = "resultado neto estimado negativo"
                elif o.net_safe <= 0:
                    o.excluded = "rentable solo por poco: negativo con el margen de seguridad"
            rep.opportunities.append(o)
    rep.proposals = choose(rep.opportunities)
    return rep


def choose(opps: list[Opportunity]) -> list[Opportunity]:
    """La mejor combinación y la mejor de otro ciclo (si no la hay, la mejor en otro pool)."""
    ok = sorted((o for o in opps if not o.excluded), key=lambda o: o.net_apr, reverse=True)
    if not ok:
        return []
    first = ok[0]
    second = (next((o for o in ok[1:] if o.cycle != first.cycle), None)
              or next((o for o in ok[1:] if o.pool.id != first.pool.id), None))
    return [first] + ([second] if second else [])


# ---------------------------------------------------------------- salida


def _p(x: float | None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".") if abs(x) < 10 else f"{x:,.2f}"
    return f"{x:.6g}"


def _pct(x: float | None, sign: bool = True) -> str:
    return "—" if x is None else (f"{x:+.1%}" if sign else f"{x:.0%}")


def _usd(x: float | None) -> str:
    return "—" if x is None else f"{x:,.2f} $"


def _stage_line(rep: Report, key: str) -> str:
    r = rep.asset.timeframes[key]
    if r.status != "ok":
        return f"sin datos ({r.message})"
    trans = f" (en transición {r.transition})" if r.transition else ""
    return f"etapa {r.stage} · {r.stage_name}{trans}, confianza {r.confidence_label}"


def render(rep: Report) -> str:
    if rep.error:
        return f"**No se puede analizar {rep.base}{'/' + rep.quote if rep.quote else ''}**: {rep.error}"
    b, q = rep.base, rep.quote
    out = [f"# Liquidez concentrada · {b}/{q} · capital {rep.capital:,.0f} $", "",
           f"Precio actual: **{_p(rep.price)} {q}**" + ("" if q in STABLES else f" (1 {q} = {_p(rep.quote_usd)} $)"),
           ""]
    out += [f"> {n}" for n in rep.warnings if n.startswith(("Se analiza como", "Par expresado"))] + (
        [""] if any(n.startswith(("Se analiza como", "Par expresado")) for n in rep.warnings) else [])
    if rep.proposals:
        for i, o in enumerate(rep.proposals, 1):
            out += _proposal(rep, o, i)
    else:
        out += ["## Sin propuesta ahora",
                "Ninguna combinación de ciclo y pool cumple a la vez: etapa no desfavorable, liquidez "
                "suficiente y resultado neto positivo incluso con el margen de seguridad. Con los datos de "
                "hoy, las comisiones no compensan lo que se pierde frente a mantener los tokens: mejor "
                "esperar y volver a consultar.", ""]
    out += ["## Etapas del par", "",
            "| Ciclo | Etapa | Idoneidad | Rango del método | Histórico: dentro / resultado medio frente a mantener |",
            "|---|---|---|---|---|"]
    for key, name, _ in CYCLES:
        level, _ = rep.stages[key]
        rng, bt = rep.ranges[key], rep.backtests[key]
        rtxt = f"{_p(rng[0])} – {_p(rng[1])} ({rng[0] / rep.price - 1:+.1%} / {rng[1] / rep.price - 1:+.1%})" if rng else "—"
        btxt = (f"{bt.inside:.0%} / {bt.mean_result:+.2%} ({bt.samples} ciclos)" if bt.inside is not None
                else f"insuficiente ({bt.samples} ciclos)")
        out.append(f"| {name} | {_stage_line(rep, key)} | {level} | {rtxt} | {btxt} |")
    out += ["", "## Todas las combinaciones evaluadas", "",
            "| Ciclo | Pool | Comisiones/día | Fuente | Gas/reajuste | Neto/ciclo | Neto anual | Estado |",
            "|---|---|---|---|---|---|---|---|"]
    for o in sorted(rep.opportunities, key=lambda o: (o.net_apr is None, -(o.net_apr or 0))):
        star = "**propuesta**" if o in rep.proposals else (o.excluded or "válida")
        out.append(f"| {o.name} | {o.pool.label} | {_usd(o.quote.fee_day)} | "
                   f"{ {'real': 'real', 'volumen': 'volumen'}.get(o.quote.fee_source, '—') } | {_usd(o.gas_usd)} | "
                   f"{_usd(o.net_cycle)} | {_pct(o.net_apr)} | {star} |")
    if rep.illiquid:
        out += ["", f"Pools descartados por poca liquidez (TVL < {MIN_TVL / 1e3:,.0f} k$ o volumen < "
                    f"{MIN_VOLUME / 1e3:,.0f} k$/día): " + "; ".join(rep.illiquid)]
    if rep.warnings:
        out += ["", "Avisos: " + " · ".join(rep.warnings)]
    out += ["", "**Cómo se calcula.** Rango: hasta dónde llegó el precio del par en el 90 % de los ciclos "
                "pasados, por cada lado, con la volatilidad actual. «Dentro» y «resultado medio» salen de "
                "aplicar el mismo método a los ciclos pasados sin ver el futuro; el resultado se reescala "
                "a la anchura actual del rango, que depende de la volatilidad de hoy. Comisiones «real»: lo "
                "que cobró de verdad cada unidad de liquidez de ese pool, leído de la blockchain (el menor "
                "entre la media de 7 y de 30 días), por la liquidez que aportas; «volumen»: estimación con la mediana del volumen "
                "de 30 días cuando no hay dato on-chain. Se cobran solo con el precio dentro. Neto por ciclo = "
                "comisiones × fracción de ciclos dentro + resultado esperado frente a mantener × capital − gas "
                "de un reajuste. Solo se propone lo que sigue siendo positivo con una pérdida un "
                f"{SAFETY_LOSS - 1:.0%} peor. Son estimaciones: el volumen y la liquidez del pool cambian cada día.",
            "", f"_{DISCLAIMER}_"]
    return "\n".join(out)


def _proposal(rep: Report, o: Opportunity, i: int) -> list[str]:
    b, q, qt = rep.base, rep.quote, o.quote
    pool = o.pool
    base_tok, quote_tok = (pool.pair.split("/") if pool.base_is_token0 else pool.pair.split("/")[::-1])
    level, reasons = rep.stages[o.cycle]
    return [
        f"## Propuesta {i}: ciclo {o.name.lower()} en {pool.label}",
        "",
        f"- **Pool**: {pool.dex} en {pool.chain.capitalize()}, par {pool.pair}, comisión "
        f"{P._pct(pool.fee)}" + (", sin hook" if pool.is_v4 else "") + ". "
        + ("Identificador" if pool.is_v4 else "Dirección") + f" `{pool.address}` ("
        + (f"[abrir en Uniswap]({pool.app_url}) · " if pool.app_url else "")
        + f"[ver en GeckoTerminal]({pool.url}))."
        + (" Al crear la posición, comprueba que la aplicación marca **v4** y esta comisión; puedes "
           "depositar ETH sin envolver." if pool.is_v4 and "ETH" in pool.pair.split("/") else
           " Al crear la posición, comprueba que la aplicación marca **v4** y esta comisión."
           if pool.is_v4 else
           " Al crear la posición, elige **v3** (la aplicación propone v4 por defecto) y esta comisión."
           if pool.dex == "Uniswap v3" else ""),
        f"- **Rango a introducir** (ya ajustado a los ticks del pool): mínimo **{_p(qt.low)}** y máximo "
        f"**{_p(qt.high)}** {q} por {b} ({qt.low / qt.pool_price - 1:+.1%} / {qt.high / qt.pool_price - 1:+.1%} "
        f"desde el precio del pool, {_p(qt.pool_price)}). Ticks {qt.tick_lower} / {qt.tick_upper}."
        + ("" if q in STABLES else f" Si el DEX muestra el precio al revés ({b} por {q}): mínimo "
           f"**{_p(1 / qt.high)}** y máximo **{_p(1 / qt.low)}**."),
        f"- **Depósito para {rep.capital:,.0f} $**: {_p(o.base_amount)} {base_tok} "
        f"({_pct(qt.asset_share, False)}) y {_p(o.quote_amount)} {quote_tok} ({_pct(1 - qt.asset_share, False)}).",
        f"- **Etapa**: {_stage_line(rep, o.cycle)} → {level}. " + " ".join([reasons[0].split(". ", 1)[-1]] + reasons[1:]),
        f"- **Histórico del método en este par** ({o.backtest.samples} ciclos): el precio no salió del "
        f"rango en el {o.backtest.inside:.0%} de los ciclos. Resultado esperado frente a mantener los "
        f"tokens, con la anchura actual del rango: {o.expected_result:+.2%} por ciclo "
        f"({o.expected_result * rep.capital:+,.2f} $).",
        f"- **Estimación por ciclo**: comisiones {_usd(o.fees_cycle)} ({_usd(qt.fee_day)}/día con el "
        f"precio dentro × {o.days:g} días × {o.backtest.inside:.0%}; "
        + ("según lo que pagó de verdad este pool, el menor entre los últimos 7 y 30 días"
           if qt.fee_source == "real"
           else "estimadas por volumen, sin dato on-chain")
        + "), resultado frente a mantener "
        f"{o.expected_result * rep.capital:+,.2f} $, gas {_usd(o.gas_usd)} → **neto {_usd(o.net_cycle)}**, "
        f"equivalente a {_pct(o.net_apr)} anual. Con una pérdida un {SAFETY_LOSS - 1:.0%} peor: "
        f"{_usd(o.net_safe)} por ciclo.",
        f"- **Si sale por arriba** te quedas 100 % en {quote_tok}; **por abajo**, 100 % en {base_tok}. En "
        f"ambos casos deja de cobrar comisiones: vuelve a ejecutar la habilidad y reajusta. Revisa como "
        f"mínimo al cerrar cada vela {'diaria' if o.cycle == '1d' else 'semanal' if o.cycle == '1w' else 'mensual'}.",
        "",
    ]


def to_json(rep: Report) -> dict:
    def opp(o: Opportunity) -> dict:
        return {"ciclo": o.name, "pool": o.pool.label, "direccion": o.pool.address, "red": o.pool.chain,
                "dex": o.pool.dex, "comision": o.pool.fee, "min": o.quote.low, "max": o.quote.high,
                "tick_inferior": o.quote.tick_lower, "tick_superior": o.quote.tick_upper,
                "precio_pool": o.quote.pool_price, "cantidad_base": o.base_amount,
                "cantidad_cotizada": o.quote_amount, "idoneidad": o.level,
                "historico_dentro": o.backtest.inside, "historico_resultado_medio": o.backtest.mean_result,
                "resultado_esperado": o.expected_result, "comisiones_ciclo": o.fees_cycle,
                "comisiones_dia": o.quote.fee_day, "fuente_comisiones": o.quote.fee_source or None,
                "enlace_dex": o.pool.app_url or None, "enlace_geckoterminal": o.pool.url,
                "gas_reajuste": o.gas_usd, "neto_ciclo": o.net_cycle,
                "neto_anual": o.net_apr, "neto_ciclo_con_margen": o.net_safe, "excluida": o.excluded or None}
    return {"par": f"{rep.base}/{rep.quote}", "capital": rep.capital, "precio": rep.price,
            "error": rep.error or None, "propuestas": [opp(o) for o in rep.proposals],
            "combinaciones": [opp(o) for o in rep.opportunities], "avisos": rep.warnings,
            "descartados_por_liquidez": rep.illiquid, "aviso": DISCLAIMER}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="python -m etapas.oportunidad",
                                 description="Mejores rangos de liquidez concentrada para un par.")
    ap.add_argument("par", help="Par, por ejemplo ETH/USDC, BTC/USDT o ETH/BTC")
    ap.add_argument("--capital", type=float, default=5000.0, help="Capital por posición en dólares")
    ap.add_argument("--red", default="", metavar="REDES",
                    help="Limita a estas redes, separadas por comas: ethereum, base, arbitrum, solana")
    ap.add_argument("--json", action="store_true", help="Salida en JSON")
    args = ap.parse_args(argv)
    chains = [c.strip().lower() for c in args.red.split(",") if c.strip()]
    rep = analyze(args.par, args.capital, chains or None)
    print(json.dumps(to_json(rep), ensure_ascii=False, indent=2, default=float) if args.json else render(rep))
    return 1 if rep.error else 0


if __name__ == "__main__":
    sys.exit(main())
