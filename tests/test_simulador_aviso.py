"""Aviso de calibración del simulador: decisión, mensaje y registro (sin BD ni GitHub)."""
import json

import pytest

from etapas import simulador_aviso as A


def _cal(pool, real, sim, pcts):
    rows = [{"fees_real": real / len(pcts), "fees_p50": sim / len(pcts), "pct": p} for p in pcts]
    return {"pool": pool, "red": "base", "pruebas": len(rows), "comisiones_real_vs_p50": real / sim,
            "dentro_p10_p90": sum(0.1 <= p <= 0.9 for p in pcts) / len(pcts), "detalle": rows}


GOOD = [0.05] + [0.5] * 8 + [0.95]                 # 80 % dentro del P10–P90


def _todos(mal=None):
    """Un veredicto por plazo; `mal` = plazo que se desvía (comisiones 0,70 y P10–P90 al 60 %)."""
    bad = [0.05] * 4 + [0.5] * 6
    return [A.combine([_cal("A", 70, 100, bad) if d == mal else _cal("A", 100, 100, GOOD)], 0.85, d)
            for d in A.PLAZOS]


def test_combine_junta_todas_las_pruebas():
    v = A.combine([_cal("A", 100, 100, GOOD), _cal("B", 80, 100, GOOD)], 0.85)
    assert v.ratio == pytest.approx(0.9)
    assert v.cobertura == pytest.approx(0.8)
    assert v.pruebas == 20
    assert v.ok and v.dias == A.PRINCIPAL
    assert v.ajuste_propuesto == pytest.approx(0.77)


def test_fuera_de_banda_por_comisiones_o_por_cobertura():
    assert not A.combine([_cal("A", 70, 100, GOOD)], 0.85).ok_comisiones
    assert not A.combine([_cal("A", 100, 100, [0.02] * 5 + [0.5] * 5)], 0.85).ok_cobertura
    with pytest.raises(ValueError):
        A.combine([], 0.85)


def test_plazos_largos_bandas_mas_anchas():
    v30 = A.combine([_cal("A", 85, 100, GOOD)], 0.85, 30)
    v180 = A.combine([_cal("A", 85, 100, GOOD)], 0.85, 180)
    assert not v30.ok_comisiones and v180.ok_comisiones
    assert not A.combine([_cal("A", 70, 100, GOOD)], 0.85, 180).ok_comisiones
    pcts = [0.05] * 3 + [0.5] * 7                                # 70 % dentro
    assert A.combine([_cal("A", 100, 100, pcts)], 0.85, 180).ok_cobertura
    assert not A.combine([_cal("A", 100, 100, [0.05] * 4 + [0.5] * 6)], 0.85, 180).ok_cobertura


def test_decide_abre_una_vez_y_cierra_al_volver():
    bad, ok = _todos(mal=180), _todos()
    assert A.decide(bad, None) == "abrir"            # basta con que un plazo se desvíe
    assert A.decide(bad, 7) is None                  # ya avisado
    assert A.decide(ok, 7) == "cerrar"
    assert A.decide(ok, None) is None


def test_mensaje_propone_el_ajuste_solo_en_el_plazo_principal():
    vs = [A.combine([_cal("WETH/USDC 0.05%", 80, 100, GOOD)], 0.85, 30),
          A.combine([_cal("WETH/USDC 0.05%", 80, 100, GOOD)], 0.85, 7)]
    t, b = A.title(vs), A.body(vs, "2026-12-01")
    assert "30 d comisiones 0.80" in t and "7 d comisiones 0.80" in t
    assert "**0.68**" in b and A.MARKER in b
    assert "No toques el ajuste común" in b          # a 7 días no se propone cambiarlo
    assert "0x" not in b                             # nada de direcciones: el repositorio es público


class FakeGH:
    def __init__(self, open_n=None):
        self.open_n, self.calls = open_n, []

    def open_issue(self):
        return self.open_n

    def create(self, title, body):
        self.calls.append(("create", title))

    def close(self, n, comment):
        self.calls.append(("close", n))


def test_run_registra_y_actua(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "LOG", tmp_path / "cal.jsonl")
    monkeypatch.setattr(A, "historial", lambda: None)          # sin el historial local
    gh = FakeGH()
    out = A.run(gh, vs=_todos(mal=30))
    assert "DESVIADO" in out and "ajuste propuesto" in out and gh.calls[0][0] == "create"
    gh2 = FakeGH(open_n=3)
    A.run(gh2, vs=_todos())
    assert gh2.calls == [("close", 3)]
    sim = FakeGH()
    A.run(sim, simulate=True, vs=_todos(mal=90))
    assert sim.calls == []
    lines = (tmp_path / "cal.jsonl").read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert len(lines) == 3 and first["ok"] is False and [p["dias"] for p in first["plazos"]] == list(A.PLAZOS)


def test_tus_simulaciones_desviadas_abren_aviso_sin_datos_personales(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "LOG", tmp_path / "cal.jsonl")
    bodies = []

    class GH(FakeGH):
        def create(self, title, body):
            bodies.append((title, body))

    poco = {"evaluadas": 5, "dentro_p10_p90": 0.2}
    assert A.historial_ok(poco) and A.historial_ok(None)      # pocas: no avisa todavía
    mal = {"evaluadas": 30, "dentro_p10_p90": 0.5, "pools": 3}
    assert not A.historial_ok(mal)
    out = A.run(GH(), vs=_todos(), hist=mal)
    assert "tus simulaciones: 30 vencidas, P10–P90 50% ⚠" in out
    title, body = bodies[0]
    assert "tus simulaciones" in title and "30 simulaciones" in body and "50%" in body
    gh = FakeGH(open_n=4)
    A.run(gh, vs=_todos(), hist={"evaluadas": 30, "dentro_p10_p90": 0.8})
    assert gh.calls == [("close", 4)]
