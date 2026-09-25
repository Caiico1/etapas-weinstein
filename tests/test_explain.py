"""Columna "Qué significa" y sección "zona clave"."""
from etapas.analysis import AssetResult, TimeframeResult
from etapas.config import TIMEFRAMES
from etapas.explain import key_zone, meaning, next_close


def _r(tf="1w", stage=1, price=100.0, ma=90.0, confirm=110.0, invalid=80.0, atr=8.0, **kw):
    base = dict(timeframe=tf, label=TIMEFRAMES[tf].label, status="ok", stage=stage, ma_len=30,
                price=price, ma=ma, confirm_level=confirm, invalid_level=invalid, atr=atr,
                slope_label="plana", ma_run=0, structure="LH/HL", prior_change=-0.4,
                range_bottom=80.0, range_top=110.0, candle_time="2026-09-14")
    base.update(kw)
    return TimeframeResult(**base)


def _asset(tfs, last_price, kind="cripto"):
    a = AssetResult("X", {k: tfs.get(k, TimeframeResult(k, TIMEFRAMES[k].label, "error", "-"))
                          for k in ("1M", "1w", "1d")}, kind=kind)
    a.timeframes["1d"].last_price = last_price
    return a


def test_meaning_uses_the_readings_data():
    base = meaning(_r(stage=1))
    assert "se ha frenado" in base and "entre 80.00 y 110.00" in base and "sin marcar nuevos mínimos" in base
    bear = meaning(_r(stage=4, price=70, ma=90, ma_run=-6, structure="LH/LL"))
    assert "por debajo de su media de 30 semanas (90.00), que baja desde hace 6 semanas" in bear
    assert "decrecientes" in bear
    rebound = meaning(_r(stage=4, price=95, ma=90, ma_run=-8))
    assert "rebotando por encima" in rebound
    bull = meaning(_r(tf="1d", stage=2, price=120, ma=100, slope_label="positiva", structure="HH/HL"), "accion")
    assert "50" not in bull and "sesiones" in bull and "crecientes" in bull


def test_key_zone_crossed_near_and_far():
    daily = _r(tf="1d", stage=2, price=100, confirm=150, invalid=60, atr=3, candle_time="2026-09-24")
    weekly = _r(stage=1, confirm=110, invalid=80, atr=8)            # techo de la base en 110
    crossed = key_zone(_asset({"1w": weekly, "1d": daily}, last_price=112))
    assert len(crossed) == 1 and "ya está por encima del nivel que confirma (110.00)" in crossed[0]
    assert "domingo 27/09" in crossed[0] and "pasaría a etapa 2" in crossed[0]
    near = key_zone(_asset({"1w": weekly, "1d": daily}, last_price=107))   # a 3 < 0.5 × ATR(8)=4
    assert len(near) == 1 and "está a 2.8% del nivel que confirma" in near[0]
    assert key_zone(_asset({"1w": weekly, "1d": daily}, last_price=100)) == []


def test_next_close_dates():
    assert "miércoles 30/09" in next_close(_r(tf="1M", candle_time="2026-08-01"), "cripto")
    assert "viernes 25/09" in next_close(_r(tf="1w", candle_time="2026-09-14"), "accion")
