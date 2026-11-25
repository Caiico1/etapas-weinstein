"""Historial de simulaciones: guardar, evaluar al vencer y resumir (archivos temporales, sin BD)."""
import pandas as pd

from etapas import simulador as S
from etapas import simulador_historial as H
from tests.test_simulador import AV, POOL, _walk


def _res(monkeypatch, hora="2026-01-01 00:00:00"):
    monkeypatch.setattr(S, "referencia_sin_riesgo", lambda chain: {"nombre": "USDC", "apy": 0.04, "hora": hora})
    monkeypatch.setattr(S, "calibracion_pool", lambda *a, **k: None)
    monkeypatch.setattr(S, "etapa_del_par", lambda pool, dias: None)
    monkeypatch.setattr(S, "aave_hoy", lambda pool: dict(AV))
    h = S.features(POOL, _walk(n=24 * 60, g0=1e-20, g1=1e-12))
    res = S.run("x", "base", ["±5%", "±20%"], dias=7, escenarios=200, h=h)
    res["hora"] = hora
    return res


def test_registrar_sustituye_la_misma_simulacion_del_mismo_dia(tmp_path, monkeypatch):
    log = tmp_path / "h.jsonl"
    res = _res(monkeypatch)
    assert H.registrar(res, log) == 2
    H.registrar(res, log)                                     # repetida el mismo día: no duplica
    rows = H._leer(log)
    assert len(rows) == 2 and {r["texto"] for r in rows} == {"±5%", "±20%"}
    r = rows[0]
    assert r["tl"] < r["tu"] and r["p10"] <= r["p50"] <= r["p90"] and isinstance(r["compensa"], bool)
    H.registrar(res | {"hora": "2026-01-02 00:00:00"}, log)   # otro día: se añade
    assert len(H._leer(log)) == 4


def test_evaluar_y_resumen(tmp_path, monkeypatch):
    log, res_f = tmp_path / "h.jsonl", tmp_path / "r.jsonl"
    H.registrar(_res(monkeypatch), log)
    recs = H._leer(log)
    falsos = {recs[0]["key"]: recs[0]["p50"], recs[1]["key"]: recs[1]["p90"] + 100}   # uno dentro, otro fuera

    def real(rec):
        x = falsos[rec["key"]]
        return {"key": rec["key"], "real": x, "real_vs": 10.0, "error": x - rec["valor_media"],
                "dentro": rec["p10"] <= x <= rec["p90"], "lado": "dentro" if x <= rec["p90"] else "encima",
                "real_compensa": rec["compensa"], "evaluada": "hoy"}

    assert H.evaluar(log, res_f, real=real) == 2
    assert H.evaluar(log, res_f, real=real) == 0             # ya evaluadas
    r = H.resumen(log, res_f)
    assert r["evaluadas"] == 2 and r["pendientes"] == 0 and r["dentro_p10_p90"] == 0.5
    assert sum(v["n"] for v in r["veredicto"].values()) == 2 and all(v["acierto"] == 1 for v in r["veredicto"].values())
    assert "2 simulaciones vencidas" in H.texto(r) and len(r["ultimas"]) == 2


def test_no_evalua_las_que_no_han_vencido(tmp_path, monkeypatch):
    log, res_f = tmp_path / "h.jsonl", tmp_path / "r.jsonl"
    hoy = str(pd.Timestamp.now().floor("h"))
    H.registrar(_res(monkeypatch, hora=hoy), log)
    assert H.evaluar(log, res_f, real=lambda rec: 1 / 0) == 0
    r = H.resumen(log, res_f)
    assert r["evaluadas"] == 0 and r["pendientes"] == 2 and "ninguna ha vencido" in H.texto(r)
