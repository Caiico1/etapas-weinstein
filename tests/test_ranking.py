import json

import pytest

from etapas import ranking as R
from etapas import simulador as S
from tests.test_simulador import POOL, _walk


def _p(label, s0="WETH", s1="USDC", tvl=3e6, pid=None, chain="base"):
    return {"chain": chain, "version": "v3", "pool_id": pid or label, "label": label, "fee": 500, "tick_spacing": 10,
            "symbol0": s0, "symbol1": s1, "dec0": 18, "dec1": 6, "tvl": tvl}


def test_candidatos_quita_poco_tvl_estables_y_anomalos():
    from etapas.simulador_aviso import ANOMALOS
    ch, pid = next(iter(ANOMALOS))
    todos = [_p("a"), _p("b", tvl=1e5), _p("c", "USDC", "USDT"), _p("d", pid=pid, chain=ch)]
    assert [p["label"] for p in R.candidatos(todos)] == ["a"]


def test_calcular_ordena_por_vs_mantener_y_excluye(monkeypatch):
    monkeypatch.setattr(S, "calibracion_pool", lambda *a, **k: None)
    def W(**k):
        d = _walk(**k)
        d["tvl_usd"] = 5e6
        return d
    hs = {"bueno": S.features(POOL, W(n=24 * 400, g0=1e-20, g1=4e-11, seed=1)),
          "malo": S.features(POOL, W(n=24 * 400, g0=1e-20, g1=2e-12, seed=2)),
          "joven": S.features(POOL, W(n=24 * 100, seed=3)),
          "raro": S.features(POOL, W(n=24 * 400, g0=1e-20, g1=1e-6, seed=4))}
    todos = [_p(k) for k in hs]
    r = R.calcular(30, 5000, 150, todos=todos, cargar=lambda pid, ch: hs[pid])
    assert [p["label"] for p in r["pools"]] == ["bueno", "malo"]
    assert r["pools"][0]["vs_media"] > r["pools"][1]["vs_media"]
    exc = {x["label"]: x["excluido"] for x in r["excluidos"]}
    assert "días de histórico" in exc["joven"] and "sospechosos" in exc["raro"]
    json.dumps(r)


def test_lanzar_en_segundo_plano(monkeypatch, tmp_path):
    import threading
    sigue, espera = threading.Event(), threading.Event()      # time.sleep está anulado en las pruebas
    monkeypatch.setattr(R, "guardar", lambda r, path=tmp_path / "r.json": path.write_text(json.dumps(r)))
    monkeypatch.setattr(R, "calcular", lambda dias, capital, progreso=None: (sigue.wait(5), {"fecha": "x", "pools": []})[1])
    assert R.lanzar() and not R.lanzar()                      # el segundo no arranca otro
    assert R.estado()["calculando"]
    sigue.set()
    for _ in range(100):
        if not R.estado()["calculando"]:
            break
        espera.wait(0.05)
    assert not R.estado()["calculando"] and json.loads((tmp_path / "r.json").read_text())["fecha"] == "x"


def test_excluye_comisiones_recientes_infladas_frente_a_revert(monkeypatch):
    from etapas import contraste_revert as CR
    monkeypatch.setattr(S, "calibracion_pool", lambda *a, **k: None)
    d = _walk(n=24 * 400, g0=1e-20, g1=4e-11, seed=1)
    d["tvl_usd"] = 5e6
    h = S.features(POOL, d)
    monkeypatch.setattr(CR, "de_pool", lambda pid, ch, path=None: {"ratio": 1.0, "ratio_mes": 2.4})
    r = R.puntuar(_p("x"), 30, 5000, 100, cargar=lambda pid, ch: h)
    assert "último mes" in r["excluido"] and "×2,40" in r["excluido"]
