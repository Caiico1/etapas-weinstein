import json

import numpy as np
import pandas as pd
import pytest

from etapas import perpetuo as P
from etapas import simulador as S
from tests.test_simulador import POOL, POOL_INV, _walk


def _f(rate=0.0001, desde="2025-01-01", dias=400):
    idx = pd.date_range(desde, periods=dias * 3, freq="8h")
    return pd.Series(rate, index=idx)


def test_simbolo_solo_activo_estable():
    assert P.simbolo(POOL) == ("ETHUSDT", True) and P.simbolo(POOL_INV) == ("ETHUSDT", False)
    assert P.simbolo({**POOL, "symbol1": "WBTC"}) is None


def test_apr_reciente_y_funding_por_hora():
    f = _f(0.0001)
    assert P.apr_reciente(f, pd.Timestamp("2025-12-01")) == pytest.approx(0.0001 * 3 * 365, rel=0.02)
    horas = pd.Series(pd.date_range("2025-03-01", periods=25, freq="h"))
    fh = P.funding_horas(f, horas)
    assert len(fh) == 24 and np.count_nonzero(fh) == 3 and fh.sum() == pytest.approx(0.0003)


@pytest.mark.parametrize("pool", [POOL, POOL_INV])
def test_corto_compensa_la_caida_y_sin_cubrir_es_el_pool(pool):
    h = S.features(pool, _walk(pool=pool, n=24 * 60, g0=1e-20, g1=1e-12))
    paths, info = S._paths_estres(h, 24 * 10, 1.0)
    j = {x["caso"]: i for i, x in enumerate(info)}
    rg = S.parse_rango("10", h)
    a0 = P.simbolo(pool)[1]
    m = P.margen(1.3)
    sin = P.cubierta(h, paths, rg, 0.0, 5000, 0.05, a0, m)
    con = P.cubierta(h, paths, rg, 1.0, 5000, 0.05, a0, m)
    assert np.allclose(sin["final"], S.simulate(h, paths, rg, 5000, 0.05).final)
    assert con["V"] + con["M"] == pytest.approx(5000) and con["N"] == pytest.approx(con["s"] * con["V"])
    cae, sube = j["Cae 30 %"], j["Sube 30 %"]
    assert con["final"][cae] - 5000 > 0.5 * (sin["final"][cae] - 5000)      # recupera buena parte de la caída
    assert con["final"][sube] < sin["final"][sube]                           # y pierde si sube
    assert not con["liquidada"].any()
    poco = P.cubierta(h, paths, rg, 1.0, 5000, 0.05, a0, 0.1)                  # margen del 10 %: la subida lo liquida
    assert poco["liquidada"][sube] and not poco["liquidada"][cae]


def test_funding_positivo_lo_cobra_el_corto():
    h = S.features(POOL, _walk(n=24 * 60, vol=0.0, g0=1e-20, g1=1e-12))
    paths = S.replay(S.features(POOL, _walk(n=24 * 30 + 1, vol=0.0, g0=1e-20, g1=1e-12)))
    rg = S.parse_rango("10", h)
    r = P.cubierta(h, paths, rg, 1.0, 5000, 0.05, True, 0.5, apr=0.10)
    assert r["funding"][0] == pytest.approx(r["N"] * 0.10 * 30 / 365, rel=0.01)


def test_descarga_pagina_hacia_atras_y_guarda_cache(tmp_path):
    t0 = P.DESDE_MS + 10 * 8 * 3600_000
    pag = [[{"fundingRateTimestamp": str(t0 - k * 8 * 3600_000), "fundingRate": "0.0001"} for k in range(5)],
           [{"fundingRateTimestamp": str(t0 - k * 8 * 3600_000), "fundingRate": "0.0002"} for k in range(5, 12)], []]
    llamadas = []

    def get(url):
        llamadas.append(url)
        return {"result": {"list": pag[len(llamadas) - 1]}}

    f = P.funding("ETHUSDT", path=tmp_path / "f.json", get=get)
    assert len(f) == 12 and f.index.is_monotonic_increasing
    assert len(llamadas) == 2                                         # la 2.ª página baja de DESDE_MS
    f2 = P.funding("ETHUSDT", path=tmp_path / "f.json", get=lambda u: pytest.fail("debía usar la caché"))
    assert len(f2) == 12


def test_run_incluye_la_cobertura_con_perpetuo(monkeypatch):
    from tests.test_simulador import AV
    h = S.features(POOL, _walk(n=24 * 200))
    monkeypatch.setattr(S, "referencia_sin_riesgo", lambda chain: None)
    monkeypatch.setattr(S, "calibracion_pool", lambda *a, **k: None)
    monkeypatch.setattr(S, "etapa_del_par", lambda pool, dias: None)
    monkeypatch.setattr(S, "aave_hoy", lambda pool: dict(AV))
    monkeypatch.setattr(P, "funding", lambda sym: _f(0.0001, desde=str(h.hour_now - pd.Timedelta(days=120))))
    d = S.run("x", "base", ["±10%"], dias=14, escenarios=200, h=h)
    cp = d["cobertura_perp"]
    assert cp["perpetuo"] == "ETHUSDT" and cp["apr_funding"] == pytest.approx(0.1095, rel=0.05)
    assert [f["cobertura"] for f in cp["rangos"][0]["filas"]] == list(S.COBERTURAS)
    assert any("perpetuo ETHUSDT" in x for x in d["recomendacion"]["mejorar"])
    json.dumps(d)
