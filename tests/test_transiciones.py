"""Aviso temprano de transición (1→2, 3→4) y giro de la tendencia previa tras una caída larga."""
import numpy as np
import pandas as pd
import pytest

from etapas.classifier import EARLY_SIDE, classify
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
    assert (h["stage"].to_numpy()[-6:] == 1).all()
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
