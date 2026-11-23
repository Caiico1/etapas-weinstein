"""Posiciones abiertas de Revert simuladas desde hoy (sin red ni BD: Revert falso y series sintéticas)."""
import math

import pytest

from etapas import backtest as B
from etapas import posiciones_lp as P
from etapas import simulador as S
from tests.test_simulador import POOL, _walk


def _fila(nft=1, tl=None, tu=None, valor=9.3, red="arbitrum", proto="uniswapv3", exited=False):
    return {"nft_id": nft, "network": red, "protocol": proto, "pool": "0x0ABC", "fee_tier": "500",
            "tick_lower": tl, "tick_upper": tu, "underlying_value": str(valor), "fees_value": "0.1",
            "in_range": True, "age_days": 12.5, "exited": exited, "token0": "0xa", "token1": "0xb",
            "tokens": {"0xa": {"symbol": "WETH"}, "0xb": {"symbol": "USDC"}},
            "performance": {"hodl": {"pnl": "0.03"}}}


def _ticks(h, lo_pct, hi_pct):
    p = S.price_now(h)
    r = S.parse_rango(f"{p * (1 + lo_pct):.4f}-{p * (1 + hi_pct):.4f}", h)
    return r.tl, r.tu


def test_normalizar():
    n = P.normalizar(_fila(tl=-10, tu=20))
    assert n["id"] == 1 and n["red"] == "arbitrum" and n["version"] == "v3" and n["pool_id"] == "0x0abc"
    assert n["par"] == "WETH/USDC" and n["valor"] == 9.3 and n["vs_mantener_desde_apertura"] == 0.03
    assert P.normalizar(_fila(tl=-10, tu=20, exited=True)) is None
    assert P.normalizar(_fila(tl=-10, tu=20, red="polygon")) is None
    assert P.normalizar(_fila(tl=-10, tu=20, red="mainnet", proto="uniswapv4"))["red"] == "ethereum"


def test_simular_posicion_abierta_sin_costes():
    df = _walk(n=24 * 200)
    h = S.features(POOL, df)
    tl, tu = _ticks(h, -0.10, 0.10)
    pos = P.normalizar(_fila(tl=tl, tu=tu, valor=1000))
    r = P.simular(pos, dias=30, escenarios=300, cargar=lambda p, c, hh: S.features(POOL, df))
    assert r["en_rango_hoy"] and r["lo"] < r["precio"] < r["hi"]
    assert 0 < r["sale_del_rango"] <= 1 and 0 < r["en_rango"] <= 1
    assert r["comisiones"] > 0 and r["mejor_ancho"]["texto"].startswith("±")
    assert r["fiabilidad"]["nivel"] in ("alta", "media", "baja") and r["lectura"]
    # sin gas ni cambio inicial: igual que simular a mano con abierta=True
    paths = S.bootstrap(h, 30 * 24, 300)
    s = S.simulate(h, paths, S.rango_de_ticks(h, tl, tu), capital=1000, abierta=True)
    assert s.costs == 0
    assert math.isclose(r["lo"], S.rango_de_ticks(h, tl, tu).lo)


def test_posicion_fuera_de_rango_y_pool_que_no_esta():
    df = _walk(n=24 * 200)
    h = S.features(POOL, df)
    tl, tu = _ticks(h, -0.40, -0.25)
    r = P.simular(P.normalizar(_fila(tl=tl, tu=tu)), 7, 200, cargar=lambda p, c, hh: S.features(POOL, df))
    assert not r["en_rango_hoy"] and r["lectura"].startswith("Fuera de rango")

    def falla(p, c, hh):
        raise B.BacktestError("no hay ningún pool")
    r = P.simular(P.normalizar(_fila(tl=tl, tu=tu)), 7, 200, cargar=falla)
    assert "no está en MetaProDB" in r["error"]


def test_cruzar_con_posiciones_txt(tmp_path):
    txt = tmp_path / "posiciones.txt"
    txt.write_text("# comentario\nETH  1808.09  2347.28  semanal · Base · n.º 1000001\n"
                   "ETH/BTC  0.0265741  0.0297531  semanal · Arbitrum\n# ETH 1 2 · n.º 99\n", encoding="utf-8")
    lineas = P.vigiladas(txt)
    assert [l["ids"] for l in lineas] == [{1000001}, set()]
    filas = [{"id": 2000002, "par": "WBTC/WETH", "red": "arbitrum", "lo": 0.026574, "hi": 0.029753},
             {"id": 3000003, "par": "WBTC/WETH", "red": "arbitrum", "lo": 0.02419, "hi": 0.031341}]
    av = P.cruzar(filas, lineas)
    assert any("n.º 1000001 está en posiciones.txt" in a for a in av)       # cerrada en Revert
    assert any("n.º 3000003" in a and "no la vigila" in a for a in av)      # abierta sin vigilar
    assert not any("2000002" in a for a in av)                               # emparejada por rango


def test_dinero_con_centimos_y_porcentaje():
    assert P.dinero(-0.11, 9.32) == "−0,11 $ (−1,2 %)"
    assert P.dinero(0.06, 8.87, signo=False) == "0,06 $ (0,7 %)"
    assert P.dinero(1234.5) == "+1.234 $"


class FakeRevert:
    def __init__(self, filas):
        self.filas, self.llamadas = filas, []

    def posiciones(self, cuenta):
        self.llamadas.append(cuenta)
        return self.filas


def test_informe_con_revert_falso(monkeypatch):
    df = _walk(n=24 * 200)
    h = S.features(POOL, df)
    tl, tu = _ticks(h, -0.1, 0.1)
    fake = FakeRevert([_fila(1, tl, tu, 50), _fila(2, tl, tu, 80), _fila(3, tl, tu, 10, exited=True)])
    inf = P.informe(14, 200, cuentas=["0xABC"], cliente=fake, lineas=[],
                    cargar=lambda p, c, hh: S.features(POOL, df))
    assert fake.llamadas == ["0xabc"]
    assert [p["id"] for p in inf["posiciones"]] == [2, 1]                    # por valor, sin la cerrada
    assert inf["total_valor"] == 130 and len(inf["avisos"]) == 2
    monkeypatch.setattr(P, "addresses_from_config", lambda: [])         # configuración sin carteras
    with pytest.raises(ValueError):
        P.leer([], FakeRevert([]))
