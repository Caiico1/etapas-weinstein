"""Oportunidades por par: parseo, comprobación histórica del método, elección y análisis completo
con datos falsos (sin red)."""
import math

import numpy as np
import pandas as pd
import pytest

from etapas import oportunidad as op
from etapas import pools as P
from etapas.analysis import AssetResult, analyze_candles
from etapas.config import TIMEFRAMES
from etapas.data import Candles, invert_candles
from etapas.descubrir import TOKENS, tokens_for
from etapas.liquidez import divergence
from etapas.pools import Pool, PoolState
from synth import cycle_for, ohlcv_from_close


# ---------------------------------------------------------------- par y tokens

@pytest.mark.parametrize("text, pair", [("eth/usdc", ("ETH", "USDC")), ("WETH-USDC", ("ETH", "USDC")),
                                        ("cbBTC/WETH", ("BTC", "ETH")), ("ETH BTC", ("ETH", "BTC")),
                                        ("USDC/ETH", ("ETH", "USDC")), ("SOL/USD", ("SOL", "USD"))])
def test_parse_pair(text, pair):
    assert op.parse_pair(text)[:2] == pair


@pytest.mark.parametrize("text", ["USDC/USDT", "ETH/ETH", "ETH", "ADA/USDC"])
def test_parse_pair_rejects(text):
    with pytest.raises(ValueError):
        op.parse_pair(text)


def test_token_registry():
    for chain, reg in TOKENS.items():
        addrs = [a for toks in reg.values() for _, a, _ in toks]
        assert len(addrs) == len(set(addrs)), chain
        assert all(0 < d <= 18 for toks in reg.values() for _, _, d in toks)
    assert [s for s, _, _ in tokens_for("arbitrum", "USD")] == ["USDC", "USDT"]
    assert tokens_for("base", "SOL") == []


def test_invert_candles():
    df = pd.DataFrame({"open": [2.0], "high": [4.0], "low": [1.0], "close": [2.5], "volume": [7.0]})
    inv = invert_candles(df)
    assert inv.iloc[0].tolist() == [0.5, 1.0, 0.25, 0.4, 7.0]
    assert (inv["high"] >= inv[["open", "close"]].max(axis=1)).all()
    assert (inv["low"] <= inv[["open", "close"]].min(axis=1)).all()


# ---------------------------------------------------------------- comprobación histórica

def _walk(n=700, seed=0, vol=0.02):
    rng = np.random.default_rng(seed)
    return ohlcv_from_close(100 * np.exp(np.cumsum(rng.normal(0, vol, n))), rng, wick=vol / 2)


def test_backtest_matches_method_and_scales_with_width():
    cfg = TIMEFRAMES["1d"]
    r = analyze_candles(Candles(_walk(), None, "test"), cfg)
    bt = op.backtest(r, 300)
    assert bt.samples == 300
    assert 0.7 < bt.inside < 0.9                         # ~80 %, como el método validado
    assert bt.mean_result < 0 and bt.per_width < 0       # frente a mantener, siempre se pierde algo
    e1, e2 = bt.expected(95, 105), bt.expected(90, 110)
    assert e2 / e1 == pytest.approx(math.log(110 / 90) / math.log(105 / 95))
    short = op.backtest(analyze_candles(Candles(_walk(50), None, "test"), cfg), 300)
    assert short.inside is None                          # con pocos ciclos no se inventa nada


def test_backtest_result_is_divergence_formula():
    """El resultado de cada ciclo es la fórmula de Uniswap v3 al precio de cierre del ciclo."""
    cfg = TIMEFRAMES["1d"]
    r = analyze_candles(Candles(_walk(400, 3), None, "test"), cfg)
    bt = op.backtest(r, 30)
    df = r.candles
    from etapas.config import LP_QUANTILE
    from etapas.indicators import expected_range
    vals = []
    for t in range(len(df) - 31, len(df) - 1):
        lo, hi = expected_range(df.iloc[:t + 1], r.history["atr"].iloc[:t + 1], LP_QUANTILE, cfg.range_lookback)
        vals.append(divergence(df["close"].iloc[t], df["close"].iloc[t + 1], lo, hi))
    assert bt.mean_result == pytest.approx(np.mean(vals))


# ---------------------------------------------------------------- análisis completo sin red

POOL_ARB = Pool("arbitrum-uniswap-x", "ETH/USDC", "Uniswap v3", "arbitrum",
                "0xc6962004f452be9203591991d15f6b388e09e8d0", "WETH/USDC", 0.0005, 10, 18, 6, True)
POOL_ETH = Pool("ethereum-uniswap-y", "ETH/USDC", "Uniswap v3", "ethereum",
                "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640", "USDC/WETH", 0.0005, 10, 6, 18, False)
POOL_THIN = Pool("base-uniswap-z", "ETH/USDC", "Uniswap v3", "base", "0x0000000000000000000000000000000000000001",
                 "WETH/USDC", 0.01, 200, 18, 6, True)


def _fake_asset(symbol, fetch=None, provisional=True):
    res = {}
    for k, cfg in TIMEFRAMES.items():
        df, _ = cycle_for(cfg, seed=1)
        res[k] = analyze_candles(Candles(df, None, "sintético"), cfg, provisional=False)
    return AssetResult(symbol, res)


@pytest.fixture
def fake_world(monkeypatch):
    price = _fake_asset("ETH").last_price
    monkeypatch.setattr(op, "analyze_symbol", _fake_asset)
    monkeypatch.setattr(op, "_quote_usd", lambda q: 1.0)
    monkeypatch.setattr(op, "discover", lambda b, q: ([POOL_ARB, POOL_ETH, POOL_THIN], []))
    states = {POOL_ARB.id: PoolState(price, 10 ** 19, 0.25, 5e7, 5e7),
              POOL_ETH.id: PoolState(price, 10 ** 19, 0.25, 5e7, 9e7),
              POOL_THIN.id: PoolState(price, 10 ** 15, 0.25, 1e4, 2e5)}
    monkeypatch.setattr(P, "fetch_states", lambda pools: states)
    monkeypatch.setattr(P, "median_volume", lambda pool: 4e7)
    monkeypatch.setattr(op, "gas_costs", lambda chains: ({"arbitrum": 0.1, "ethereum": 1.5}, []))
    return price


def test_analyze_end_to_end(fake_world):
    rep = op.analyze("ETH/USDC", 5000)
    assert not rep.error and rep.price == fake_world
    assert any("TVL 200 k$" in x for x in rep.illiquid)                # el pool pequeño no entra
    daily_eth = [o for o in rep.opportunities if o.cycle == "1d" and o.pool.chain == "ethereum"]
    assert daily_eth and all("Ethereum" in o.excluded for o in daily_eth)
    for o in rep.opportunities:
        if o.base_amount is not None:                                   # el depósito suma el capital
            total = o.base_amount * o.quote.pool_price + o.quote_amount
            assert total == pytest.approx(5000)
            assert o.quote.low <= rep.ranges[o.cycle][0] and o.quote.high >= rep.ranges[o.cycle][1]
        if not o.excluded:
            assert o.level != "desfavorable" and o.net_cycle > 0 and o.net_safe > 0
            assert o.net_cycle == pytest.approx(o.fees_cycle + o.expected_result * 5000 - o.gas_usd)
    ok = sorted((o for o in rep.opportunities if not o.excluded), key=lambda o: -o.net_apr)
    assert rep.proposals == op.choose(rep.opportunities)
    if ok:
        assert rep.proposals[0] is ok[0]
    text = op.render(rep)
    assert "Etapas del par" in text and "Todas las combinaciones evaluadas" in text
    data = op.to_json(rep)
    assert data["par"] == "ETH/USDC" and len(data["combinaciones"]) == len(rep.opportunities)


def test_choose_prefers_another_cycle():
    def o(cycle, pool, apr):
        x = op.Opportunity(cycle, cycle, 1, "favorable", [], pool, None, op.Backtest(0.8, -0.01, 50))
        x.net_apr = apr
        return x
    a, b, c = o("1w", POOL_ARB, 0.3), o("1w", POOL_ETH, 0.2), o("1M", POOL_ARB, 0.1)
    assert op.choose([b, c, a]) == [a, c]
    assert op.choose([a, b]) == [a, b]
    assert op.choose([]) == []


def test_invalid_pair_is_reported():
    rep = op.analyze("USDC/USDT")
    assert rep.error and "No se puede analizar" in op.render(rep)


# ---------------------------------------------------------------- simetría ETH/BTC ↔ BTC/ETH

def test_verdict_symmetric_without_stable():
    """Entre dos volátiles, subir o bajar cuesta lo mismo: etapas 2 y 4 dan la misma idoneidad.
    Con una estable, la etapa 4 sigue siendo desfavorable (se acaba en el activo que cae)."""
    from etapas.analysis import TimeframeResult
    from etapas.liquidez import verdict

    def tf(stage):
        return TimeframeResult("1d", "Diario", "ok", stage=stage, stage_name="x", confidence_label="alta")
    assert verdict(tf(2), None, "ETH", "BTC")[0] == verdict(tf(4), None, "ETH", "BTC")[0] == "precaución"
    assert verdict(tf(4), None, "ETH", "USDC")[0] == "desfavorable"
    assert verdict(tf(1), tf(2), "ETH", "BTC")[0] == "precaución"      # marco superior en tendencia
    assert verdict(tf(1), tf(2), "ETH", "USDC")[0] == "favorable"


def test_inverted_pair_is_analyzed_in_canonical_orientation(fake_world, monkeypatch):
    monkeypatch.setattr(op, "canonical_pair", lambda b, q: ("ETH", "BTC"))
    monkeypatch.setattr(op, "_quote_usd", lambda q: 84000.0)
    seen = []
    monkeypatch.setattr(op, "discover", lambda b, q: (seen.append((b, q)) or [], []))
    rep = op.analyze("BTC/ETH", 5000)
    assert (rep.base, rep.quote) == ("ETH", "BTC") and seen == [("ETH", "BTC")]
    assert any(w.startswith("Se analiza como ETH/BTC") for w in rep.warnings)
    assert "> Se analiza como ETH/BTC" in op.render(rep)


def test_analyze_can_be_limited_to_some_networks(fake_world):
    rep = op.analyze("ETH/USDC", 5000, ["arbitrum"])
    assert {o.pool.chain for o in rep.opportunities} == {"arbitrum"}
    assert any("Solo se evalúan pools en: Arbitrum" in w for w in rep.warnings)
    assert all(o.pool.chain == "arbitrum" for o in rep.proposals)


def test_price_proxy_for_assets_without_history(fake_world, monkeypatch):
    """XAUT se analiza con el histórico de PAXG (mismo subyacente), y se avisa."""
    asked = []
    monkeypatch.setattr(op, "fetch_pair_candles", lambda b, q, cfg: asked.append(b))
    monkeypatch.setattr(op, "analyze_symbol", lambda sym, fetch=None: (fetch(sym, None), _fake_asset(sym))[1])
    rep = op.analyze("XAUT/USDT", 5000)
    assert asked == ["PAXG"] and rep.base == "XAUT"
    assert any("histórico de PAXG" in w for w in rep.warnings)
