import numpy as np
import pandas as pd
import pytest

from etapas import plan as PL
from etapas import rendir as R
from etapas import simulador as S
from tests.test_simulador import POOL, _walk


def _h():
    return S.features(POOL, _walk(n=24 * 60, g0=1e-20, g1=1e-12))


def test_sin_rendimiento_es_guardar_los_tokens():
    h = _h()
    paths = S.bootstrap(h, 24 * 10, 100, seed=2)
    rg = S.parse_rango("10", h)
    assert np.allclose(R.simulate_rendir(h, paths, rg, 1.0, 1.0), S.simulate(h, paths, rg).hodl)
    # si los dos tokens crecen un 1 %, lo depositado (sin los costes) crece un 1 %
    lp = S.simulate(h, paths, rg)
    assert np.allclose(R.simulate_rendir(h, paths, rg, 1.01, 1.01) - lp.costs, (lp.hodl - lp.costs) * 1.01)


def test_crecimiento_y_tipo_de_aave_y_staking():
    idx = pd.date_range("2025-01-01", periods=24 * 400, freq="h")
    aave = ("aave", pd.Series(0.0365, index=idx))
    t0, t1 = pd.Timestamp("2025-06-01"), pd.Timestamp("2025-07-01")
    assert R.crecimiento(aave, t0, t1) == pytest.approx(1 + 0.0365 * 30 / 365, rel=1e-3)
    assert R.apy_reciente(aave, t1) == pytest.approx(0.0365)
    dias = pd.date_range("2025-01-01", periods=400, freq="D")
    st = ("staking", pd.Series(1.2 * 1.03 ** (np.arange(400) / 365), index=dias))
    assert R.crecimiento(st, t0, t1) == pytest.approx(1.03 ** (30 / 365), rel=1e-4)
    assert R.apy_reciente(st, t1) == pytest.approx(0.03, abs=1e-3)
    assert R.crecimiento(("aave", pd.Series(dtype=float)), t0, t1) == 1.0     # Aave no tiene el token
    assert R.via("WETH").startswith("staking") and R.via("USDC") == "Aave"


def test_alternativa_y_frase_del_plan():
    h = _h()
    paths = S.bootstrap(h, 24 * 10, 100, seed=2)
    rg = S.parse_rango("10", h)
    idx = pd.date_range("2024-01-01", periods=24 * 900, freq="h")
    sr = {POOL["symbol0"]: ("staking", pd.Series(1.2 * 1.03 ** (np.arange(900) / 365), index=pd.date_range("2024-01-01", periods=900))),
          POOL["symbol1"]: ("aave", pd.Series(0.04, index=idx))}
    a = R.alternativa(h, 10, paths, rg, sr=sr)
    assert a["vs_mantener"] > 0 and a["tokens"][1]["apy"] == pytest.approx(0.04)
    p = PL.planes(h, 10, paths, 5000, 0.05)
    p["rendir"] = a
    assert "rindiendo sin pool" in PL.frases(p, "ETH", 5000, 30)["rendir"]
