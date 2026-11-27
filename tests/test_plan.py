import numpy as np
import pytest

from etapas import perpetuo as P
from etapas import plan as PL
from etapas import simulador as S
from tests.test_perpetuo import _f
from tests.test_simulador import POOL, POOL_INV, _walk


def _h(pool=POOL):
    return S.features(pool, _walk(pool=pool, n=24 * 60, g0=1e-20, g1=1e-12))


@pytest.mark.parametrize("pool", [POOL, POOL_INV])
def test_el_plan_reproduce_cada_pieza(pool):
    h = _h(pool)
    paths = S.bootstrap(h, 24 * 10, 200, seed=3)
    rg = S.parse_rango("10", h)
    a0 = P.simbolo(pool)[1]
    m = P.margen(1.3)
    assert np.allclose(PL.simulate_plan(h, paths, rg, None, 0.0, 5000, 0.05, a0)["final"],
                       S.simulate(h, paths, rg, 5000, 0.05).final)
    assert np.allclose(PL.simulate_plan(h, paths, rg, 0.05, 0.0, 5000, 0.05, a0, apy=0.04)["final"],
                       S.simulate_salida(h, paths, rg, 0.05, 5000, 0.05, apy=0.04).final)
    a = PL.simulate_plan(h, paths, rg, None, 1.0, 5000, 0.05, a0, m, apr=0.05)
    b = P.cubierta(h, paths, rg, 1.0, 5000, 0.05, a0, m, apr=0.05)
    assert np.allclose(a["final"], b["final"]) and (a["liquidada"] == b["liquidada"]).all()


@pytest.mark.parametrize("pool", [POOL, POOL_INV])
def test_si_salta_el_stop_se_cierra_tambien_el_corto(pool):
    h = _h(pool)
    paths, info = S._paths_estres(h, 24 * 10, 1.0)
    cae = next(i for i, x in enumerate(info) if x["caso"] == "Cae 30 %")
    rg = S.parse_rango("10", h)
    a0 = P.simbolo(pool)[1]
    m = P.margen(1.3)
    r = PL.simulate_plan(h, paths, rg, 0.0, 1.0, 5000, 0.05, a0, m)
    assert r["salta"][cae] and not r["liquidada"][cae]
    sin_corto = PL.simulate_plan(h, paths, rg, 0.0, 0.0, 5000, 0.05, a0)
    # con stop y corto cerrado a la vez, la caída casi no pasa del precio del stop
    assert r["final"][cae] > sin_corto["final"][cae]
    assert abs(r["final"][cae] - 5000) < 0.1 * 5000


def test_planes_elige_y_compara_con_la_partida():
    h = _h()
    paths = S.bootstrap(h, 24 * 10, 200, seed=5)
    p = PL.planes(h, 10, paths, 5000, 0.05, apy=0.04, f=_f(0.0001, desde="2024-01-01", dias=900))
    assert p["perpetuo"] == "ETHUSDT" and p["base"]["ancho"] == 0.10
    assert {f["cobertura"] for f in p["filas"]} == {0.0, 0.5, 1.0}
    md, pr = p["elegido"]["media"], p["elegido"]["prudente"]
    assert md["valor_media"] == max(f["valor_media"] for f in p["filas"])
    assert pr["valor_media"] >= md["valor_media"] - PL.TOLERANCIA_PRUDENTE * 5000
    assert pr["valor_p10"] >= md["valor_p10"]
    assert all((f["vs_mantener"] is None) == (f["cobertura"] > 0) for f in p["filas"])
    fr = PL.frases(p, "ETH", 5000, 30)
    assert "Frente a guardar los mismos tokens" in fr["media"] or md["cobertura"] > 0
    assert fr["media"].startswith("Para el mejor resultado medio") and fr["validado"]
    assert not PL.frases(p, "ETH", 5000, 7)["validado"]


def test_sin_perpetuo_no_cubre():
    h = _h({**POOL, "symbol1": "WBTC"})
    paths = S.bootstrap(h, 24 * 10, 100, seed=5)
    p = PL.planes(h, 10, paths, 5000, 0.05)
    assert p["perpetuo"] is None and {f["cobertura"] for f in p["filas"]} == {0.0}
