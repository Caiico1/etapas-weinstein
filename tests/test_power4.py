"""Etapa Power4 (reglas de MetaPro): entradas, permanencia y salidas sobre series controladas."""
import numpy as np
import pandas as pd

from etapas import power4
from etapas.rutina import diff


def _df(close) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    idx = pd.date_range("2024-01-01", periods=len(close), freq="D", tz="UTC")
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": 1.0}, index=idx)


def test_needs_sma40():
    assert power4.latest(_df(np.linspace(100, 120, 39)), 3) == (None, "")


def test_uptrend_is_advance_and_downtrend_decline():
    assert power4.latest(_df(np.linspace(100, 200, 80)), 3)[0] == 2
    assert power4.latest(_df(np.linspace(200, 100, 80)), 3)[0] == 4


def test_decline_holds_until_three_closes_above_sma20():
    down = np.linspace(200, 100, 80)
    # Rebote corto: el cierre supera la SMA20 pero no 3 veces seguidas ni el último máximo
    one = np.concatenate([down, [140, 105]])
    st = power4.classify(_df(one), 3)
    assert st["p4"].iloc[-2] == 4 and st["above"].iloc[-2] == 1
    three = np.concatenate([down, [140, 141, 142]])
    assert power4.classify(_df(three), 3)["p4"].iloc[-1] == 1


def test_advance_ends_in_distribution_not_decline():
    up = np.linspace(100, 200, 80)
    st = power4.classify(_df(np.concatenate([up, [150, 149, 148]])), 3)["p4"]
    assert st.iloc[-1] == 3


def test_reason_mentions_rule():
    stage, why = power4.latest(_df(np.linspace(200, 100, 80)), 3)
    assert stage == 4 and "último máximo relevante" in why and "SMA20" in why


def test_power4_change_is_reported():
    prev = {"ETH": {"1d": {"etapa": 3, "transicion": "", "power4": 4}}}
    curr = {"ETH": {"1d": {"etapa": 3, "transicion": "", "power4": 1}}}
    assert [c["texto"] for c in diff(prev, curr)] == ["Power4 4 → 1"]
    # Instantáneas anteriores sin Power4: no es un cambio
    prev["ETH"]["1d"].pop("power4")
    assert diff(prev, curr) == []
