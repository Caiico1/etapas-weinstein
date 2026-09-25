"""Rango estimado de fluctuación (mín./máx. de la vela en curso)."""
import numpy as np

from etapas.analysis import analyze_candles
from etapas.config import RANGE_QUANTILE, TIMEFRAMES
from etapas.data import Candles
from etapas.indicators import atr, expected_range
from synth import ohlcv_from_close

D = TIMEFRAMES["1d"]


def _random_walk(n, seed, vol=0.02):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, vol, n)))
    return ohlcv_from_close(close, rng, wick=vol / 2)


def test_coverage_matches_quantile_on_random_walk():
    """Fuera de muestra, cada extremo se respeta ~RANGE_QUANTILE de las veces."""
    lo_ok = hi_ok = n = 0
    for seed in range(3):
        df = _random_walk(700, seed)
        a = atr(df, 14)
        for t in range(300, len(df) - 1):
            lo, hi = expected_range(df.iloc[:t + 1], a.iloc[:t + 1], RANGE_QUANTILE, 250)
            nxt = df.iloc[t + 1]
            lo_ok += nxt["low"] >= lo
            hi_ok += nxt["high"] <= hi
            n += 1
    assert abs(lo_ok / n - RANGE_QUANTILE) < 0.04
    assert abs(hi_ok / n - RANGE_QUANTILE) < 0.04


def test_range_scales_with_volatility():
    calm, wild = _random_walk(400, 1, vol=0.01), _random_walk(400, 1, vol=0.04)
    w = []
    for df in (calm, wild):
        lo, hi = expected_range(df, atr(df, 14), RANGE_QUANTILE, 250)
        w.append((hi - lo) / df["close"].iloc[-1])
    assert w[1] > 2 * w[0]


def test_short_history_gives_no_range():
    df = _random_walk(25, 0)
    lo, hi = expected_range(df, atr(df, 14), RANGE_QUANTILE, 250)
    assert np.isnan(lo) and np.isnan(hi)


def test_result_includes_range_around_price():
    res = analyze_candles(Candles(_random_walk(400, 5), None, "test"), D)
    assert res.est_period == "hoy"
    assert 0 < res.est_low < res.price < res.est_high
