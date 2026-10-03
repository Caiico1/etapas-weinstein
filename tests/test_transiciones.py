"""Aviso temprano de transición (1→2, 3→4) y giro de la tendencia previa tras una caída larga."""
import numpy as np
import pandas as pd
import pytest

from etapas.classifier import BREAK_RUN, EARLY_SIDE, classify
from etapas.config import TIMEFRAMES
from synth import cycle_for, ohlcv_from_close


@pytest.mark.parametrize("tf", ["1d", "1w", "1M"])
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_warning_1_to_2_before_stage_2(tf, seed):
    """Antes de pasar de la base (1) a la subida (2) aparece el aviso «1→2»."""
    cfg = TIMEFRAMES[tf]
    df, _ = cycle_for(cfg, seed)
    h = classify(df, cfg)
    st, tr = h["stage"].to_numpy(), h["transition"].to_numpy()
    first2 = next(i for i in range(1, len(st)) if st[i] == 2 and st[i - 1] == 1)
    assert "1→2" in tr[max(0, first2 - 4 * cfg.slope_window):first2]


def test_early_warning_rules():
    """El aviso temprano solo se marca en la etapa 1 (o 3), con la media girando al menos una
    ventana de pendiente y el precio del lado de la media."""
    cfg = TIMEFRAMES["1w"]
    df, _ = cycle_for(cfg, 7)
    h = classify(df, cfg)
    early = h[(h["transition"] == "1→2") & (h["stage"] == 1)]
    assert len(early)
    ok = ((early["ma_run"] >= cfg.slope_window) & (early["close"] > early["ma"])
          & (early["pct_above"] >= EARLY_SIDE)) | (early["second"] == 2)
    assert ok.all()
    assert set(h.loc[h["transition"] == "3→4", "stage"]) <= {3}


def _crash_then_base(seed=3):
    """Mensual cripto: subida ×10, caída de −70 % en 2 años y base de 14 meses, con volatilidad
    alta (ATR ≈ 40 % del precio, como SOL en 2026)."""
    rng = np.random.default_rng(seed)
    path = np.concatenate([np.linspace(np.log(20), np.log(200), 30),
                           np.linspace(np.log(200), np.log(60), 24),
                           np.full(14, np.log(60))])
    close = np.exp(path + rng.normal(0, 0.12, len(path)))
    return ohlcv_from_close(close, rng, wick=0.15, freq="MS")


def test_base_after_long_decline_is_stage_1_not_3():
    """Sin el criterio de giro, la tendencia previa seguía siendo la subida (+171 %) y la base se
    leía como distribución (etapa 3)."""
    cfg = TIMEFRAMES["1M"]
    h = classify(_crash_then_base(), cfg)
    last = h["stage"].to_numpy()[-6:]
    assert 3 not in last and (last == 1).sum() >= 5      # base; una vela puede salir como ruptura (2)
    assert h["prior_chg"].iloc[-1] < 0
    assert (h["atr"] / h["close"]).iloc[-1] > 0.3          # de verdad es volatilidad de cripto


def test_top_range_keeps_prior_uptrend():
    """En un techo, el vaivén de la media no basta para dar la vuelta a la tendencia previa."""
    cfg = TIMEFRAMES["1M"]
    for seed in (13, 18):                                    # semillas en las que un criterio laxo fallaba
        df, labels = cycle_for(cfg, seed)
        h = classify(df, cfg)
        top = np.flatnonzero(labels == 3)
        m = cfg.ma // 2
        stages = h["stage"].to_numpy()[top[0] + m:top[-1] + 1]
        assert 1 not in stages                               # un criterio laxo lo leía como base
        assert (stages == 3).mean() >= 0.75                  # 0,78 en la semilla 18, igual que antes


# ---------------------------------------------------------------- ruptura de la base y niveles

def _decline_base_breakout(cfg, seed=5, jump=0.22):
    """Caída larga, base estrecha y ruptura brusca: la media sigue casi plana tras la ruptura
    (como BTC, ETH y SOL semanal en agosto de 2026)."""
    rng = np.random.default_rng(seed)
    ma = cfg.ma
    path = np.concatenate([np.linspace(np.log(300), np.log(100), 3 * ma),            # caída
                           np.log(100) + 0.03 * np.sin(np.arange(2 * ma) / 3),       # base
                           np.log(100) + jump + np.linspace(0, 0.06, cfg.slope_window * 2)])  # ruptura
    close = np.exp(path + rng.normal(0, 0.004, len(path)))
    return ohlcv_from_close(close, rng, wick=0.004, freq={"1d": "D", "1w": "W-MON", "1M": "MS"}[cfg.key])


@pytest.mark.parametrize("tf", ["1d", "1w"])
def test_breakout_from_base_is_stage_2(tf):
    """Tras romper el techo de la base con el precio sobre una media que ya sube, la etapa es 2
    en pocas velas, sin esperar a que la media tenga pendiente fuerte."""
    cfg = TIMEFRAMES[tf]
    df = _decline_base_breakout(cfg)
    h = classify(df, cfg)
    after = h.iloc[-cfg.slope_window * 2:]
    assert (h["stage"].iloc[-(cfg.slope_window * 2 + 5):-(cfg.slope_window * 2)] == 1).all()   # antes: base
    assert (after["stage"].iloc[BREAK_RUN + 1:] == 2).all()
    first = after[after["stage"] == 2].iloc[0]
    assert first["by_break"] and first["second"] in (1, 4)           # la puntuación sola decía base
    assert first["break_up"] < first["close"] and first["break_up"] == pytest.approx(103, abs=3)


def test_small_excess_in_range_is_not_a_breakout():
    """Superar el techo de un rango lateral por poco no es una ruptura: hace falta BREAK_FRAC."""
    from etapas.classifier import _break_levels
    high, low = np.full(6, 110.0), np.full(6, 90.0)
    up, _ = _break_levels(np.array([100, 112, 119, 121, 115, 109.0]), high, low)
    assert np.isnan(up[:3]).all()                  # 112 y 119 no llegan a 110 + 0,5 × 20
    assert up[3] == 110 and up[4] == 110           # 121 rompe; sigue vigente mientras cierre ≥ 110
    assert np.isnan(up[5])                         # 109 vuelve al rango: ruptura fallida
    _, dn = _break_levels(np.array([100, 79.0, 85, 91]), high[:4], low[:4])
    assert np.isnan(dn[0]) and dn[1] == 90 and dn[2] == 90 and np.isnan(dn[3])
    # Una ruptura caduca cuando el precio pierde la media, aunque siga por encima del techo roto:
    # así una ruptura antigua no cuenta en la base siguiente.
    ma = np.array([100, 100, 100, 100, 118, 100.0])
    up, _ = _break_levels(np.array([100, 112, 119, 121, 115, 116.0]), high, low, ma)
    assert up[3] == 110 and np.isnan(up[4]) and np.isnan(up[5])


def test_levels_are_consistent():
    """El nivel que invalida nunca es uno que el precio ya ha superado, y en una base el nivel
    que confirma queda por encima del techo del rango (lo que exige la regla de ruptura)."""
    from etapas.classifier import BREAK_FRAC, levels
    for tf in ("1d", "1w", "1M"):
        cfg = TIMEFRAMES[tf]
        for seed in (1, 7):
            df, _ = cycle_for(cfg, seed)
            h = classify(df, cfg)
            for _, row in h[h["stage"] > 0].iterrows():
                confirm, invalid = levels(row)
                s, price = int(row["stage"]), row["close"]
                if s in (1, 2) and not np.isnan(invalid):
                    assert invalid < price or s == 1
                if s == 4 and not np.isnan(invalid):
                    assert invalid > price
                if s == 2 and not np.isnan(invalid):
                    assert invalid < price
                if s == 1:
                    width = row["range_top"] - row["range_bottom"]
                    assert confirm == pytest.approx(row["range_top"] + BREAK_FRAC * width)
                    assert confirm > row["close"]
