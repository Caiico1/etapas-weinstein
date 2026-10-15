"""Comisiones por ventanas, permanencia en el rango, estrategia sin tocar frente a reajustar,
respuesta corta y búsqueda por temática (sin red)."""
import numpy as np
import pandas as pd
import pytest

from etapas import oportunidad as op
from etapas import pools as P
from etapas import tema as T
from etapas.analysis import analyze_candles
from etapas.config import TIMEFRAMES
from etapas.data import Candles
from etapas.pools import FeeRates, Pool, PoolState
from synth import ohlcv_from_close
from test_oportunidad import fake_world  # noqa: F401  (fixture)
from test_v4 import V3, _word


# ---------------------------------------------------------------- comisiones por ventanas

def test_fee_rates_by_window(monkeypatch):
    """Acumuladores que crecen 1 $ por unidad de liquidez y día, salvo una semana floja a la mitad."""
    day = 86400
    head, t_head = 3_000_000, 10_000_000
    blocks = {head: t_head}
    for d in P.FEE_SNAPSHOT_DAYS:
        blocks[head - d * 100] = t_head - d * day
    unit = int(10 ** V3.dec0 * 2 ** 128)        # 1 $ de token0 (precio 1) por unidad de liquidez

    def growth_at(t):                            # dólares acumulados hasta t (en días hacia atrás)
        days_ago = (t_head - t) / day
        g = 100 - days_ago                       # 1 $/día...
        if days_ago > 14:                        # ...pero la semana de hace 14-21 días cobró 0,5 $/día
            g += 0.5 * min(days_ago - 14, 7)
        return int(g * unit)

    def archive(chain, method, params, retries=3):
        if method == "eth_blockNumber":
            return hex(head + 5)
        n = int(params[0], 16) if method == "eth_getBlockByNumber" else int(params[1], 16)
        if method == "eth_getBlockByNumber":
            if n not in blocks:                  # el bloque de prueba para medir el tiempo de bloque
                return {"timestamp": hex(t_head - (head - n) * day // 100)}
            return {"timestamp": hex(blocks[n])}
        g = growth_at(blocks[n])
        return "0x" + _word(g if params[0]["data"] == "0xf3058399" else 0)

    monkeypatch.setattr(P, "_archive", archive)
    r = P.fee_rates(V3, 1.0, 1.0)
    assert r.day == pytest.approx(1) and r.week == pytest.approx(1)
    assert r.weeks == pytest.approx([1, 1, 0.5, 1])
    assert r.month == pytest.approx((30 - 0.5 * 7) / 30)
    assert r.prudent == pytest.approx(r.month) and r.weakest_week == pytest.approx(0.5)


def test_fee_rates_offline_is_none():
    assert P.fee_rates(V3, 1.0, 1.0) is None                      # sin red en los tests


# ---------------------------------------------------------------- permanencia en el rango

def _walk(n=700, seed=3, vol=0.02):
    rng = np.random.default_rng(seed)
    return ohlcv_from_close(100 * np.exp(np.cumsum(rng.normal(0, vol, n))), rng, wick=vol / 2)


def test_permanence_on_a_random_walk():
    df = _walk()
    r = analyze_candles(Candles(df, None, "test"), TIMEFRAMES["1d"])
    pm = op.permanence(r, df, 300)
    assert pm.samples >= op.MIN_PERMANENCE
    assert 0 <= pm.days_to_exit <= op.HORIZON_DAYS
    assert 0 < pm.inside <= 1 and 0 < pm.exits <= 1 and 0 <= pm.up <= 1
    # El rango diario aguanta ~1 día de media; en 30 días casi siempre sale
    assert pm.days_to_exit < 10 and pm.exits > 0.9
    assert pm.per_width < 0                                        # sin tocar, pierde frente a mantener


def test_permanence_only_uses_daily_paths_that_start_with_the_cycle():
    """Con un histórico diario más corto que el de los ciclos, los ciclos sin velas diarias desde su
    inicio no cuentan (no se toma un tramo diario de otra fecha)."""
    df = _walk(n=1400)
    weekly = df.resample("W-MON", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    r = analyze_candles(Candles(weekly, None, "test"), TIMEFRAMES["1w"])
    full = op.permanence(r, df, 150)
    short = op.permanence(r, df.iloc[-400:], 150)
    assert full.samples > short.samples
    assert short.samples <= 400 // 7 - op.HORIZON_DAYS // 7 + 1


def test_permanence_without_daily_data():
    df = _walk()
    r = analyze_candles(Candles(df, None, "test"), TIMEFRAMES["1d"])
    assert op.permanence(r, None, 300).inside is None
    assert op.permanence(r, df.iloc[:0], 300).samples == 0


# ---------------------------------------------------------------- respuesta corta y estrategias

def test_windows_passive_and_short_answer(fake_world, monkeypatch):
    rates = FeeRates(day=3e-15, week=1e-15, month=1.2e-15, weeks=[1e-15, 0.6e-15, 1.1e-15, 1.3e-15])
    monkeypatch.setattr(P, "fee_rates", lambda pool, u0, u1: rates)
    rep = op.analyze("ETH/USDC", 5000)
    priced = [o for o in rep.opportunities if o.net_cycle is not None]
    assert priced
    for o in priced:
        assert o.quote.fee_source == "real"
        w = o.apr_windows
        # Las ventanas son proporcionales a lo cobrado; la de 7 días es la que usa el cálculo (la menor)
        assert w["7 d"] == pytest.approx(o.quote.fee_day * 365 / 5000)
        assert w["24 h"] == pytest.approx(3 * w["7 d"]) and w["semana floja"] == pytest.approx(0.6 * w["7 d"])
        assert o.fee_apr_cycle == pytest.approx(o.fees_cycle / 5000 * 365 / o.days)
        if o.passive_net is not None:
            assert o.passive_net == pytest.approx(o.passive_fees + o.passive_il - o.gas_usd)
            assert o.passive_apr == pytest.approx(o.passive_net / 5000 * 365 / op.HORIZON_DAYS)
    text = op.render(rep)
    head = text.split("## Respuesta corta")[1].split("##")[0]
    assert "(24 h)" in head and "día de volumen fuerte" in head      # 24 h = 3 × 30 d: inflado
    assert "pérdida frente a mantener" in head
    assert "## Comisiones, permanencia en el rango y estrategias" in text
    best = op.headline(rep)
    assert best.pool.label in head
    data = op.to_json(rep)
    assert data["respuesta_corta"] and "**" not in data["respuesta_corta"]
    assert set(data["permanencia_en_rango"]) == {"Diario", "Semanal", "Mensual"}
    assert "Cómo se calcula" in text and "Cómo se calcula" not in op.render(rep, footer=False)
    assert "{" not in text                                         # ninguna plantilla sin rellenar


def test_onchain_fees_override_a_wrong_volume(fake_world, monkeypatch):
    """Si GeckoTerminal da un volumen que no cuadra con lo cobrado on-chain, manda la blockchain."""
    monkeypatch.setattr(P, "median_volume", lambda pool: 1e3)
    rep = op.analyze("ETH/USDC", 5000)
    assert not any(o.quote.fee_day for o in rep.opportunities)         # sin dato on-chain: descartados
    monkeypatch.setattr(P, "fee_rates", lambda pool, u0, u1: FeeRates(1e-12, 1e-12, 1e-12))
    rep = op.analyze("ETH/USDC", 5000)
    assert any(o.quote.fee_day for o in rep.opportunities)
    assert any("no cuadra con las comisiones cobradas on-chain" in w for w in rep.warnings)


# ---------------------------------------------------------------- temáticas

@pytest.mark.parametrize("text, key", [("oro", "oro"), ("Oro tokenizado", "oro"), ("GOLD", "oro"),
                                       ("oro-tokenizado", "oro"), ("Bitcoin", "bitcoin"), ("ETH/USDC", None),
                                       ("XAUT/USDT", None)])
def test_find_theme(text, key):
    found = T.find_theme(text)
    assert (found is None) if key is None else (found is T.TEMAS[key])


def test_chains_with_tokens():
    assert T.chains_with_tokens(T.TEMAS["oro"]) == ["ethereum"]
    assert set(T.chains_with_tokens(T.TEMAS["bitcoin"])) == {"ethereum", "base", "arbitrum"}


def _gold_pool(n, asset, pair, fee):
    return Pool(f"ethereum-uniswap-{n}", asset, "Uniswap v3", "ethereum", f"0x{n:040x}", pair, fee, 10, 6, 6, True)


def test_theme_ranking_and_render(monkeypatch):
    xaut = _gold_pool(1, "XAUT/USD", "XAUt/USDT", 0.0005)
    paxg = _gold_pool(2, "PAXG/USD", "PAXG/USDC", 0.0005)
    thin = _gold_pool(3, "PAXG/USD", "PAXG/USDT", 0.003)
    both = _gold_pool(4, "XAUT/PAXG", "XAUt/PAXG", 0.0001)
    found = {"XAUT/USD": [xaut], "PAXG/USD": [paxg, thin], "XAUT/PAXG": [both]}
    monkeypatch.setattr(T, "discover", lambda b, q: (found[f"{b}/{q}"], []))
    states = {xaut.id: PoolState(4000, 10 ** 18, 0.0, 6.4e6, 4.5e6), paxg.id: PoolState(4000, 10 ** 18, 0.0, 7e5, 2.2e6),
              thin.id: PoolState(4000, 10 ** 15, 0.0, 1e3, 5e4), both.id: PoolState(1.0, 10 ** 18, 0.0, 3e6, 5.7e6)}
    monkeypatch.setattr(P, "fetch_states", lambda pools: {p.id: states[p.id] for p in pools})
    monkeypatch.setattr(T, "_usd", lambda s: 1.0)
    monkeypatch.setattr(P, "fee_rates", lambda pool, u0, u1: FeeRates(1e-15, 4e-16, 3e-16) if pool is xaut else None)
    analyzed = []

    def fake_analyze(pair, capital, chains):
        analyzed.append(pair)
        return op.Report(*pair.split("/"), capital, None, None, None, error="sin datos en el test")
    monkeypatch.setattr(T.op, "analyze", fake_analyze)
    rep = T.analyze_theme(T.TEMAS["oro"], 1000)
    assert [r.pool for r in rep.rows][0] is xaut                   # ordenado por el APR de 30 días
    assert thin not in [r.pool for r in rep.rows]                  # menos de 100 k$ de TVL
    row = rep.rows[0]
    assert row.apr_volume == pytest.approx(6.4e6 * 0.0005 / 4.5e6 * 365)
    assert row.apr_month == pytest.approx(3e-16 * 10 ** 18 / 4.5e6 * 365)
    assert next(r for r in rep.rows if r.pool is both).related
    assert analyzed == ["XAUT/USD", "PAXG/USD"]                    # el par XAUt/PAXG solo se clasifica
    text = T.render_theme(rep)
    assert "## Respuesta corta" in text and "## Pools de oro tokenizado: clasificación rápida" in text
    assert "XAUt/PAXG 0,01 % ¹" in text and "no hay tokens de oro tokenizado verificados" in text
    data = T.theme_json(rep)
    assert len(data["clasificacion"]) == 3 and data["redes_con_tokens"] == ["ethereum"]


def test_theme_skips_pairs_without_liquidity(monkeypatch):
    small = _gold_pool(5, "XAUT/USD", "XAUt/USDT", 0.0005)
    monkeypatch.setattr(T, "discover", lambda b, q: ([small] if b == "XAUT" and q == "USD" else [], []))
    monkeypatch.setattr(P, "fetch_states", lambda pools: {small.id: PoolState(4000, 10 ** 18, 0.0, 1e5, 5e5)})
    monkeypatch.setattr(T, "_usd", lambda s: 1.0)
    monkeypatch.setattr(T.op, "analyze", lambda *a: pytest.fail("no debe analizarse"))
    rep = T.analyze_theme(T.TEMAS["oro"], 1000, ["base"])
    assert rep.rows == [] and rep.skipped == ["XAUT/USD", "PAXG/USD"]
    assert "No hay ningún pool de oro tokenizado" in T.render_theme(rep)


def test_main_routes_themes(monkeypatch, capsys):
    monkeypatch.setattr(T, "analyze_theme", lambda tema, capital, chains: T.ThemeReport(tema, capital, chains))
    assert op.main(["oro", "--capital", "1000"]) == 0
    assert "oro tokenizado" in capsys.readouterr().out
