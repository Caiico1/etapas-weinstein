import json

import pytest

from etapas import contraste_revert as C


class Cliente:
    def __init__(self, fees):
        self.fees = fees

    def tool(self, name, **k):
        assert name == "discover_pools" and k["network"] == "mainnet"
        w = {"period_start": 1764115200, "period_end": 1795651200} if k["period"] == "year" else \
            {"period_start": 1793055600, "period_end": 1795647600}
        return {"data": [{"fees_usd": str(self.fees[k["period"]]), "metrics_window": w}]}


class H:
    picos_quitados = 0


def test_contrastar_y_leer_el_resultado(monkeypatch, tmp_path):
    vistos = []
    monkeypatch.setattr(C, "_mdb", lambda con, pool, t0, t1, picos: (vistos.append(picos), 85_000.0 if t1.year == 2026 and t0.year == 2025 else 9_000.0)[1])
    pool = {"chain": "ethereum", "pool_id": "0xabc", "label": "WETH/USDC 0.05%", "version": "v3"}
    r = C.contrastar(pool, Cliente({"year": 100_000, "month": 10_000}), con=None, cargar=lambda pid, ch: H())
    assert r["ano"]["ratio"] == pytest.approx(0.85) and r["mes"]["ratio"] == pytest.approx(0.9) and vistos == [False, False]
    C.guardar({"fecha": "x", "pools": {"ethereum|0xabc": r}}, tmp_path / "c.json")
    d = C.de_pool("0xabc", "ethereum", tmp_path / "c.json")
    assert d["ratio"] == pytest.approx(0.85) and C.de_pool("0xdef", "ethereum", tmp_path / "c.json") is None
    assert C.resumen(json.loads((tmp_path / "c.json").read_text()))["pools"] == 1


def test_pocas_comisiones_no_dan_cociente(monkeypatch):
    monkeypatch.setattr(C, "_mdb", lambda *a: 5000.0)
    pool = {"chain": "ethereum", "pool_id": "0xabc", "label": "x", "version": "v4"}
    r = C.contrastar(pool, Cliente({"year": 500, "month": 50}), con=None, cargar=lambda pid, ch: H())
    assert r["ano"]["ratio"] is None
