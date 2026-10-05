"""Liquidez concentrada: ticks, fórmulas de Uniswap v3, comisiones, idoneidad, posiciones y agentes."""
import json
import math
import sys

import numpy as np
import pandas as pd
import pytest

from etapas import agentes, liquidez as lq, rutina
from etapas.analysis import AssetResult, TimeframeResult, analyze_candles
from etapas.config import LP_QUANTILE, TIMEFRAMES
from etapas.data import Candles
from etapas.indicators import atr, expected_range
from etapas.pools import POOLS, PoolState, pools_for
from etapas.posiciones import Position, check, load_positions
from etapas.report import render_json, render_text, write_html, write_index
from synth import cycle_for, ohlcv_from_close

POOL = {p.id: p for p in POOLS}


def _tick(pool, price):
    return math.log(lq.human_to_raw(pool, price)) / math.log(lq.TICK_BASE)


# ---------------------------------------------------------------- ticks (anclas reales on-chain, sep-2026)

@pytest.mark.parametrize("pool_id, price, tick", [
    ("eth-eth-usdc-weth-005", 2680.34, 197383),     # USDC es token0: el tick sube cuando ETH baja
    ("eth-base-weth-usdc-005", 2680.07, -197385),   # WETH es token0
    ("btc-base-cbbtc-usdc-005", 83600.35, -67290),
    ("btc-arb-wbtc-usdc-005", 83580.07, 67287),
    ("sol-orca-sol-usdc-004", 119.0184, -21286),
])
def test_price_to_tick_matches_chain(pool_id, price, tick):
    assert abs(_tick(POOL[pool_id], price) - tick) < 1


def test_snap_is_outward_and_on_tick_grid():
    for pool in POOLS:
        lo, hi = 0.97 * 100, 1.05 * 100
        if pool.asset == "ETH":
            lo, hi = 2500.0, 2900.0
        elif pool.asset == "BTC":
            lo, hi = 80000.0, 90000.0
        slo, shi, tl, tu = lq.snap_range(pool, lo, hi)
        assert slo <= lo and shi >= hi                       # nunca se estrecha
        assert tl % pool.tick_spacing == 0 and tu % pool.tick_spacing == 0 and tl < tu
        assert shi / hi - 1 < pool.tick_spacing * 1e-4 * 1.01    # como mucho un tick spacing más
        assert 1 - slo / lo < pool.tick_spacing * 1e-4 * 1.01


def test_same_range_in_pools_with_opposite_token_order():
    """USDC/WETH (Ethereum) y WETH/USDC (Base) tienen el mismo spacing: el rango humano coincide."""
    a = lq.snap_range(POOL["eth-eth-usdc-weth-005"], 2584.82, 2787.25)
    b = lq.snap_range(POOL["eth-base-weth-usdc-005"], 2584.82, 2787.25)
    assert a[0] == pytest.approx(b[0], rel=1e-9) and a[1] == pytest.approx(b[1], rel=1e-9)
    assert (a[2], a[3]) == (-b[3], -b[2])


# ---------------------------------------------------------------- fórmulas de la posición

def test_divergence_closed_form_at_edges():
    p, a, b = 1.0, 0.962, 1.038
    hodl_b = (1 / math.sqrt(p) - 1 / math.sqrt(b)) * b + (math.sqrt(p) - math.sqrt(a))
    assert lq.divergence(p, b, a, b) == pytest.approx((math.sqrt(b) - math.sqrt(a)) / hodl_b - 1)
    assert lq.divergence(p, b, a, b) == pytest.approx(-0.0093, abs=2e-4)
    assert lq.divergence(p, p, a, b) == pytest.approx(0)


def test_full_range_matches_uniswap_v2_impermanent_loss():
    r = 1.5
    assert lq.divergence(1.0, r, 1e-12, 1e12) == pytest.approx(2 * math.sqrt(r) / (1 + r) - 1, abs=1e-5)


def test_composition_and_capital():
    a, b = 80.0, 125.0
    center = math.sqrt(a * b)                                  # centro geométrico → 50/50
    assert lq.composition(center, a, b) == pytest.approx(0.5)
    assert lq.composition(a * 1.001, a, b) > 0.95              # cerca del mínimo: casi todo activo
    L = lq.liquidity_for_capital(100.0, a, b, 1000)
    assert lq.position_value(100.0, a, b, L) == pytest.approx(1000)
    assert lq.amounts_per_liquidity(70, a, b)[1] == 0          # bajo el rango: 100 % activo
    assert lq.amounts_per_liquidity(130, a, b)[0] == 0         # sobre el rango: 100 % estable


def test_fee_estimate_by_hand():
    pool = POOL["eth-eth-usdc-weth-005"]
    state = PoolState(price=2684.0, liquidity=3_154_885_765_440_280_790, protocol_cut=0.25,
                      volume_24h=56_000_000, tvl=98_000_000)
    q = lq.quote_pool(pool, state, 2584.82, 2787.25, 2686.0, 1000)
    L = lq.liquidity_for_capital(2684.0, q.low, q.high, 1000) * 10 ** 12   # (6 + 18) / 2
    expected = L / (state.liquidity + L) * 56e6 * 0.0005 * 0.75
    assert q.fee_day == pytest.approx(expected)
    assert 3.0 < q.fee_day < 3.8                     # ≈ 3,4 $/día medido a mano el 28/09/2026
    assert q.fee_apr == pytest.approx(q.fee_day * 365 / 1000)
    worst = min(lq.divergence(2684.0, q.low, q.low, q.high), lq.divergence(2684.0, q.high, q.low, q.high))
    assert q.breakeven_days == pytest.approx(-worst * 1000 / q.fee_day)


def test_pool_price_far_from_market_is_not_estimated():
    state = PoolState(price=2500.0, liquidity=10 ** 18, volume_24h=1e6)
    q = lq.quote_pool(POOL["eth-base-weth-usdc-005"], state, 2400, 2800, 2686.0, 1000)
    assert q.fee_day is None and "se aleja" in q.error
    q = lq.quote_pool(POOL["eth-base-weth-usdc-005"], PoolState(error="caído"), 2400, 2800, 2686.0, 1000)
    assert q.low < 2400 and q.fee_day is None and q.error == "caído"   # el rango con ticks sigue ahí


# ---------------------------------------------------------------- idoneidad

def _tf(stage, transition="", conf="alta", key="1d"):
    return TimeframeResult(key, TIMEFRAMES[key].label, "ok", stage=stage, stage_name="x",
                           transition=transition, confidence_label=conf)


@pytest.mark.parametrize("stage, level", [(1, "favorable"), (3, "favorable"),
                                          (2, "precaución"), (4, "desfavorable")])
def test_verdict_by_stage(stage, level):
    assert lq.verdict(_tf(stage), None, "ETH")[0] == level


@pytest.mark.parametrize("stage, trans, level", [("1", "1→2", "precaución"), ("3", "3→4", "precaución"),
                                                 ("4", "4→1", "precaución"), ("2", "2→3", "precaución")])
def test_verdict_transitions(stage, trans, level):
    assert lq.verdict(_tf(int(stage), trans), None, "ETH")[0] == level


def test_verdict_higher_timeframe_and_confidence():
    level, reasons = lq.verdict(_tf(1, conf="baja"), _tf(4, key="1w"), "ETH")
    assert level == "precaución"
    assert any("marco superior" in r for r in reasons) and any("confianza" in r for r in reasons)
    assert lq.verdict(TimeframeResult("1d", "Diario", "error", "sin red"), None, "ETH")[0] == "sin datos"


# ---------------------------------------------------------------- rango y análisis completo

def test_expected_range_center():
    rng = np.random.default_rng(0)
    df = ohlcv_from_close(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 400))), rng)
    a = atr(df, 14)
    lo, hi = expected_range(df, a, LP_QUANTILE, 250)
    lo2, hi2 = expected_range(df, a, LP_QUANTILE, 250, center=df["close"].iloc[-1] + 5)
    assert lo2 - lo == pytest.approx(5) and hi2 - hi == pytest.approx(5)


def _asset(symbol="ETH"):
    results = {}
    for k, cfg in TIMEFRAMES.items():
        df, _ = cycle_for(cfg, seed=1)
        results[k] = analyze_candles(Candles(df, None, "sintético"), cfg)
    return AssetResult(symbol, results)


def _fake_states(pools):
    """Pools con el precio de mercado del activo sintético."""
    return {p.id: PoolState(price=_fake_states.price, liquidity=10 ** 20, protocol_cut=0.25,
                            volume_24h=5e7, tvl=1e8) for p in pools}


def test_analyze_liquidity_profiles():
    asset = _asset()
    _fake_states.price = asset.last_price
    res = lq.analyze_liquidity(asset, fetch=_fake_states)
    assert [p.name for p in res.profiles] == ["Diaria", "Semanal", "Mensual"]
    widths = []
    for p in res.profiles:
        assert p.low < asset.last_price < p.high
        assert p.level in lq.LEVELS and p.reasons
        assert len(p.quotes) == len(pools_for("ETH")[0])
        assert all(q.low <= p.low and q.high >= p.high for q in p.quotes)
        assert p.main_quote is not None and p.main_quote.fee_day > 0
        widths.append(p.high / p.low)
    assert widths[0] < widths[1] < widths[2]                   # diario < semanal < mensual
    d = res.to_dict()
    assert d["perfiles"][0]["pools"][0]["tick_inferior"] is not None
    assert lq.analyze_liquidity(AssetResult("NVDA", asset.timeframes, kind="accion")) is None


def test_asset_without_pools_keeps_ranges():
    asset = _asset("ADA")
    res = lq.analyze_liquidity(asset, fetch=lambda pools: {})
    assert not res.pools and "Cardano" in res.note
    assert all(p.low is not None and p.quotes == [] for p in res.profiles)
    assert "Cardano" in res.best


def test_report_shows_liquidity(tmp_path):
    asset = _asset()
    _fake_states.price = asset.last_price
    asset.liquidity = lq.analyze_liquidity(asset, fetch=_fake_states)
    assert "Liquidez concentrada" in render_text(asset)
    payload = json.loads(render_json([asset]))
    assert payload["activos"][0]["liquidez"]["perfiles"][2]["perfil"] == "Mensual"
    page = write_html(asset, tmp_path).read_text(encoding="utf-8")
    assert "Liquidez concentrada (Uniswap v3 y v4 / Orca)" in page and "ticks" in page
    assert "se muestran los de referencia" in page            # sin red: lista de referencia
    index = write_index([asset], [], None, [], tmp_path, pd.Timestamp("2026-09-28", tz="UTC"),
                        positions=[check(Position("ETH", 1, 2), asset.last_price)])
    html = index.read_text(encoding="utf-8")
    assert "Liquidez: idoneidad y rango" in html and "Mis posiciones de liquidez" in html


# ---------------------------------------------------------------- posiciones

def test_load_positions_formats(tmp_path):
    f = tmp_path / "posiciones.txt"
    f.write_text("# mis rangos\neth 2.500,5 2900  semanal Base\nBTC 80000 90000\nSOL 150 100\nmal\n",
                 encoding="utf-8")
    pos, warnings = load_positions(f)
    assert [(p.symbol, p.low, p.high, p.label) for p in pos] == [
        ("ETH", 2500.5, 2900.0, "semanal Base"), ("BTC", 80000.0, 90000.0, "")]
    assert len(warnings) == 2
    assert load_positions(tmp_path / "no.txt") == ([], [])


@pytest.mark.parametrize("price, estado, aviso", [
    (2700, "dentro", False), (2510, "cerca del mínimo", True), (2890, "cerca del máximo", True),
    (2400, "fuera por abajo", True), (3000, "fuera por arriba", True), (None, "sin precio", False)])
def test_check_position(price, estado, aviso):
    r = check(Position("ETH", 2500, 2900), price)
    assert (r["estado"], r["aviso"]) == (estado, aviso)


def test_position_alerts_only_on_change():
    item = check(Position("ETH", 2500, 2900), 2400)
    assert len(rutina.position_alerts({}, [item])) == 1
    assert rutina.position_alerts({"ETH 2500-2900": "fuera por abajo"}, [item]) == []
    back = check(Position("ETH", 2500, 2900), 2700)
    assert "dentro del rango" in rutina.position_alerts({"ETH 2500-2900": "fuera por abajo"}, [back])[0]["texto"]
    assert rutina.position_alerts({}, [back]) == []          # posición nueva y dentro: sin aviso


# ---------------------------------------------------------------- agentes y rutina

def test_dictamen_and_prompt(tmp_path, monkeypatch):
    asset = _asset()
    _fake_states.price = asset.last_price
    liq = lq.analyze_liquidity(asset, fetch=_fake_states)
    d = agentes.dictamen(asset, liq, None, [check(Position("ETH", 1, 2), asset.last_price)])
    assert set(d["agentes"]) == {"etapas", "contexto", "posiciones", "rangos", "riesgo"}
    assert d["coordinador"]["recomendacion"] == liq.best
    text = agentes.write_prompt(d, tmp_path).read_text(encoding="utf-8")
    assert "usa SOLO los números del dictamen" in text and '"Mensual"' in text
    monkeypatch.setitem(sys.modules, "anthropic", None)       # sin el paquete: aviso, no error
    assert agentes.ask_claude(d, tmp_path) == (None, "falta el paquete anthropic (pip install anthropic)")


def test_routine_with_liquidity_and_positions(tmp_path, monkeypatch):
    def fake_analyze(symbol, provisional=False):
        return _asset(symbol)

    asset = _asset()
    _fake_states.price = asset.last_price
    monkeypatch.setattr(rutina, "analyze_symbol", fake_analyze)
    monkeypatch.setattr("etapas.pools.fetch_states", _fake_states)
    lista = tmp_path / "watchlist.txt"
    lista.write_text("ETH\n", encoding="utf-8")
    p = asset.last_price
    (tmp_path / "posiciones.txt").write_text(f"ETH {p * 1.1:.2f} {p * 1.3:.2f}\nXRP 1 2\n", encoding="utf-8")
    out = tmp_path / "out"
    assert rutina.run(lista, out, now=pd.Timestamp("2026-09-28 06:00", tz="UTC")) == 0
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "fuera por abajo" in index and "Liquidez: idoneidad y rango" in index
    assert "XRP no está en la lista" in index
    assert "fuera del rango por abajo" in (out / "alertas.md").read_text(encoding="utf-8")
    assert (out / "ia" / "ETH.md").exists()
    hist = json.loads((out / "historial" / "2026-09-28.json").read_text(encoding="utf-8"))
    assert list(hist["posiciones"].values()) == ["fuera por abajo", "sin precio"]


# ---------------------------------------------------------------- búsqueda de pools en vivo

def _live(price):
    from etapas.pools import Pool
    base = Pool("base-uniswap-aa", "ETH/USDC", "Uniswap v3", "base", "0x" + "aa" * 20, "WETH/USDC",
                0.0005, 10, 18, 6, True)
    eth = Pool("ethereum-uniswap4-bb", "ETH/USDC", "Uniswap v4", "ethereum", "0x" + "bb" * 32, "ETH/USDC",
               0.0001, 1, 18, 6, True)
    thin = Pool("arbitrum-uniswap-cc", "ETH/USDC", "Uniswap v3", "arbitrum", "0x" + "cc" * 20, "WETH/USDC",
                0.003, 60, 18, 6, True)
    quiet = Pool("base-uniswap-dd", "ETH/USDT", "Uniswap v3", "base", "0x" + "dd" * 20, "WETH/USDT",
                 0.0005, 10, 18, 6, True)
    states = {base.id: PoolState(price=price, liquidity=10 ** 20, volume_24h=5e7, tvl=9e7),
              eth.id: PoolState(price=price, liquidity=10 ** 19, volume_24h=5e7, tvl=5e6),
              thin.id: PoolState(price=price, liquidity=10 ** 17, volume_24h=5e7, tvl=3e5),
              quiet.id: PoolState(price=price, liquidity=10 ** 19, volume_24h=1e3, tvl=2e6)}
    return base, eth, thin, quiet, states


def test_live_pools_filters_and_sorts(monkeypatch):
    base, eth, thin, quiet, states = _live(2500.0)
    by_quote = {"USDC": [eth, thin, base], "USDT": [quiet]}
    monkeypatch.setattr(lq, "discover", lambda sym, quote: (by_quote[quote], [f"aviso {quote}"]))
    monkeypatch.setattr(lq._pools, "fetch_states", lambda pools: states)
    monkeypatch.setattr(lq._pools, "median_volume", lambda pool: 1e3 if pool is quiet else 4e7)
    pools, st, vols, warn = lq.live_pools("ETH")
    assert pools == [base, eth]                      # sin el de poco TVL ni el de poco volumen; por TVL
    assert st is states and vols == {base.id: 4e7, eth.id: 4e7} and warn == ["aviso USDC", "aviso USDT"]


def test_analysis_uses_live_pools_and_daily_avoids_ethereum(tmp_path):
    asset = _asset()
    base, eth, _, _, states = _live(asset.last_price)
    res = lq.analyze_liquidity(asset, find=lambda s: ([base, eth], states, {base.id: 5e7, eth.id: 5e7}, []))
    assert res.live and res.pools == [base, eth]
    daily, weekly, monthly = res.profiles
    assert all(len(p.quotes) == 2 for p in res.profiles)
    assert weekly.main_quote.pool is eth and monthly.main_quote.pool is eth    # paga más por dólar
    assert daily.main_quote.pool is base                                        # pero no en el diario
    assert max(daily.quotes, key=lambda q: q.fee_day).pool is eth
    asset.liquidity = res
    page = write_html(asset, tmp_path).read_text(encoding="utf-8")
    assert "búsqueda de hoy" in page and "Uniswap v4 · Ethereum · ETH/USDC 0,01 %" in page


def test_live_search_failure_falls_back_to_reference_pools():
    asset = _asset()
    _fake_states.price = asset.last_price

    def broken(symbol):
        raise OSError("sin red")
    res = lq.analyze_liquidity(asset, fetch=_fake_states, find=broken)
    assert not res.live and res.pools == pools_for("ETH")[0] and res.warnings == ["búsqueda de pools: OSError"]
    assert res.profiles[0].main_quote is not None
