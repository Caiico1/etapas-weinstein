import numpy as np
import pandas as pd

from etapas import sesamo as S


def _candles(closes):
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c})


def test_zonas():
    assert S.zone(0.3) == "cerca" and S.zone(-0.99) == "cerca"
    assert S.zone(1.5) == "intermedio" and S.zone(-1.0) == "intermedio"
    assert S.zone(2.0) == "lejos" and S.zone(-3.5) == "lejos"


def test_precio_en_la_media_permite_abrir():
    t = S.timing(_candles([100.0] * 40))
    assert t is not None and t.zone == "cerca" and t.can_open and not t.close_zone
    assert S.open_verdict(t) == "OPERACIÓN VÁLIDA"


def test_precio_lejos_no_abre_y_es_zona_de_cierre():
    df = _candles([100.0] * 40)
    t = S.timing(df, price=110.0)            # ATR ≈ 2 → 5 ATR por encima
    assert t.zone == "lejos" and not t.can_open and t.close_zone
    assert t.side == "por encima" and t.dist_pct > 0
    assert S.open_verdict(t).startswith("MANTENERSE AL MARGEN")
    assert "lejos de la media de 20" in S.wait_reason(t)
    assert "zona de cierre" in S.close_note(t)


def test_simetrico_bajista():
    t = S.timing(_candles([100.0] * 40), price=90.0)
    assert t.zone == "lejos" and t.side == "por debajo" and not t.can_open


def test_sin_velas_suficientes():
    assert S.timing(_candles([100.0] * 10)) is None
    assert S.describe(None) == "sin datos para la SMA20"


def test_rentable_pero_lejos_es_vigilar():
    from types import SimpleNamespace
    from etapas import oportunidad as op
    t = S.timing(_candles([100.0] * 40), price=110.0)
    o = SimpleNamespace(timing_wait=True, excluded=S.wait_reason(t))
    state, why = op.decision(o, 5000)
    assert state == op.VIGILAR and "media de 20" in why


def test_ciclo_de_la_nota():
    assert S.parse_cycle("semanal · v3 Base WETH/USDC") == ("1w", True)
    assert S.parse_cycle("Mensual · Arbitrum") == ("1M", True)
    assert S.parse_cycle("v4 Ethereum n.º 475067") == ("1w", False)


def test_salida_lejos_y_mantener_cerca():
    df = _candles([100.0] * 40)
    accion, why = S.exit_signal(S.timing(df, price=110.0))
    assert accion == S.SALIR and "zona de cierre" in why and "no reabras" in why
    accion, why = S.exit_signal(S.timing(df, price=110.0), outside=True)
    assert accion == S.SALIR and "no reajustes" in why
    t = S.timing(df, price=101.0)
    accion, why = S.exit_signal(t)
    lo, hi = S.exit_band(t)
    assert accion == S.MANTENER and lo < 100 < hi and "La salida salta" in why
    assert S.exit_signal(None)[0] == S.SIN_DATOS


def test_rutina_avisa_al_saltar_la_salida():
    from types import SimpleNamespace
    from etapas import rutina as R
    df = _candles([100.0] * 40)
    tf = SimpleNamespace(status="ok", candles=df, history=None)
    asset = SimpleNamespace(timeframes={"1w": tf})
    item = {"estado": "dentro", "precio": 110.0, "texto": "dentro", "aviso": False,
            "simbolo": "ETH", "min": 90.0, "max": 120.0}
    R.exit_hint(item, "semanal · Base", asset)
    assert item["salida"] == "SALIR" and item["aviso"] and "ciclo semanal" in item["texto"]
    alerts = R.position_alerts({"ETH 90-120": "dentro"}, [item])
    assert len(alerts) == 1 and "SALIR" in alerts[0]["texto"]
    assert R.position_alerts({"ETH 90-120": R.alert_state(item)}, [item]) == []
