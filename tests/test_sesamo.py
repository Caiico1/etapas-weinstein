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
