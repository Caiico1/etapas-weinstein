"""Aviso de calibración del simulador: decisión, mensaje y registro (sin BD ni GitHub)."""
import json

import pytest

from etapas import simulador_aviso as A


def _cal(pool, real, sim, pcts):
    rows = [{"fees_real": real / len(pcts), "fees_p50": sim / len(pcts), "pct": p} for p in pcts]
    return {"pool": pool, "red": "base", "pruebas": len(rows), "comisiones_real_vs_p50": real / sim,
            "dentro_p10_p90": sum(0.1 <= p <= 0.9 for p in pcts) / len(pcts), "detalle": rows}


GOOD = [0.05] + [0.5] * 8 + [0.95]                 # 80 % dentro del P10–P90


def test_combine_junta_todas_las_pruebas():
    v = A.combine([_cal("A", 100, 100, GOOD), _cal("B", 80, 100, GOOD)], 0.85)
    assert v.ratio == pytest.approx(0.9)
    assert v.cobertura == pytest.approx(0.8)
    assert v.pruebas == 20
    assert v.ok
    assert v.ajuste_propuesto == pytest.approx(0.77)


def test_fuera_de_banda_por_comisiones_o_por_cobertura():
    assert not A.combine([_cal("A", 70, 100, GOOD)], 0.85).ok_comisiones
    assert not A.combine([_cal("A", 100, 100, [0.02] * 5 + [0.5] * 5)], 0.85).ok_cobertura
    with pytest.raises(ValueError):
        A.combine([], 0.85)


def test_decide_abre_una_vez_y_cierra_al_volver():
    bad = A.combine([_cal("A", 70, 100, GOOD)], 0.85)
    ok = A.combine([_cal("A", 100, 100, GOOD)], 0.85)
    assert A.decide(bad, None) == "abrir"
    assert A.decide(bad, 7) is None                 # ya avisado
    assert A.decide(ok, 7) == "cerrar"
    assert A.decide(ok, None) is None


def test_mensaje_propone_el_ajuste_nuevo():
    v = A.combine([_cal("WETH/USDC 0.05%", 80, 100, GOOD)], 0.85)
    t, b = A.title(v), A.body(v, "2026-12-01")
    assert "comisiones 0.80" in t
    assert "**0.68**" in b and A.MARKER in b
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
    gh = FakeGH()
    out = A.run(gh, v=A.combine([_cal("A", 70, 100, GOOD)], 0.85))
    assert "DESVIADO" in out and gh.calls[0][0] == "create"
    gh2 = FakeGH(open_n=3)
    A.run(gh2, v=A.combine([_cal("A", 100, 100, GOOD)], 0.85))
    assert gh2.calls == [("close", 3)]
    sim = FakeGH()
    A.run(sim, simulate=True, v=A.combine([_cal("A", 70, 100, GOOD)], 0.85))
    assert sim.calls == []
    lines = (tmp_path / "cal.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["ok"] is False
