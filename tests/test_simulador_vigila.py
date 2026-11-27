"""Vigilancia mensual de stop, perpetuo, plan y contraste con Revert (sin BD ni GitHub)."""
import json

import pandas as pd

from etapas import simulador_aviso as A
from etapas import simulador_vigila as V
from tests.test_simulador_aviso import FakeGH, _todos


def _d(n=40, stop=(0.5, 0.45), fund=(6.0, 5.5), cub=(4950.0, 4960.0), plan=(4980.0, 4950.0)):
    return pd.DataFrame({"pool": ["A"] * n, "red": ["base"] * n, "fecha": [f"2026-01-{i % 28 + 1:02d}" for i in range(n)],
                         "stop_sim": stop[0], "stop_real": stop[1], "fund_sim": fund[0], "fund_real": fund[1],
                         "cub_sim": cub[0], "cub_real": cub[1], "plan_real": plan[0], "base_real": plan[1]})


def test_todo_en_banda():
    ps = V.comprobar(_d(), [])
    assert [p.ok for p in ps] == [True] * 5 and all(p.pruebas == 40 for p in ps[:4])
    assert "Stop (salta, simulado / real): 50% / 45%" in V.texto(ps)


def test_cada_pieza_avisa_fuera_de_banda():
    ps = {p.nombre: p for p in V.comprobar(_d(stop=(0.6, 0.4), fund=(9.0, 5.0), cub=(4800.0, 4950.0),
                                              plan=(4900.0, 4950.0)), ["X", "Y", "Z"])}
    assert not any(p.ok for p in ps.values())
    assert ps["Funding del corto (simulado ÷ real)"].valor == "×1.80"


def test_funding_pequeno_no_avisa_y_sin_perpetuo_no_hay_pieza():
    ps = {p.nombre: p for p in V.comprobar(_d(fund=(0.5, 0.1)))}
    assert ps["Funding del corto (simulado ÷ real)"].ok
    d = _d().drop(columns=["cub_sim", "cub_real", "fund_sim", "fund_real"])
    assert not any("corto" in p.nombre.lower() or "cubierto" in p.nombre.lower() for p in V.comprobar(d))


def test_nuevos_fuera_de_revert():
    def r(**ratios):
        return {"pools": {k: {"label": k.upper(), "ano": {"ratio": v}} for k, v in ratios.items()}}
    assert V.nuevos_fuera(r(a=1.0, b=3.0), r(a=0.3, b=3.0, c=1.1)) == ["A"]
    assert V.nuevos_fuera(None, r(a=0.3)) == []                    # sin contraste anterior no compara


def test_el_aviso_mensual_abre_por_una_pieza_y_lo_registra(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "LOG", tmp_path / "cal.jsonl")
    bodies = []

    class GH(FakeGH):
        def create(self, title, body):
            bodies.append((title, body))

    ps = V.comprobar(_d(stop=(0.7, 0.4)), [])
    out = A.run(GH(), vs=_todos(), hist={}, piezas=ps)
    assert "DESVIADO" in out and "Stop (salta, simulado / real): 70% / 40% ⚠" in out
    title, body = bodies[0]
    assert title.startswith("Simulador de pools: revisar stop") and "### Piezas del simulador" in body
    assert "| Stop (salta, simulado / real) | 70% / 40% |" in body
    linea = json.loads((tmp_path / "cal.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert linea["piezas"][0]["ok"] is False
    gh = FakeGH(open_n=7)
    A.run(gh, vs=_todos(), hist={}, piezas=V.comprobar(_d(), []))
    assert gh.calls == [("close", 7)]
