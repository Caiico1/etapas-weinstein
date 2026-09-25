"""Explicaciones en lenguaje llano, generadas solo a partir de las métricas de cada lectura:
la columna "Qué significa" y la sección "Lo más importante: zona clave"."""
import pandas as pd

from .analysis import AssetResult, TimeframeResult
from .config import ORDER
from .data import candle_close_time

_UNITS = {"1d": ("día", "días"), "1w": ("semana", "semanas"), "1M": ("mes", "meses")}
_UNITS_STOCK = {**_UNITS, "1d": ("sesión", "sesiones")}
_CANDLE = {"1d": "diaria", "1w": "semanal", "1M": "mensual"}
_WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# Qué pasaría si se cruza cada nivel, según la etapa
_ON_CONFIRM = {1: "rompería la base y pasaría a etapa 2 (alcista)",
               2: "confirmaría que la subida continúa",
               3: "confirmaría la ruptura del techo y pasaría a etapa 4 (bajista)",
               4: "confirmaría que la caída continúa"}
_ON_INVALID = {1: "la base fallaría y la caída podría continuar",
               2: "debilitaría la tendencia alcista",
               3: "anularía el techo y podría volver la etapa 2",
               4: "pondría en duda la tendencia bajista"}
NEAR_ATR = 0.25  # "cerca" = a menos de un cuarto de ATR del marco


def _p(x) -> str:
    from .report import fmt_price
    return fmt_price(x)


def _pct(x: float) -> str:
    return f"{x:+.0%}"


def _ma_dir(r: TimeframeResult, units) -> str:
    n = abs(r.ma_run)
    if r.slope_label == "positiva" or (r.ma_run > 0 and n >= 3):
        return "que sube" + (f" desde hace {n} {units[1] if n != 1 else units[0]}" if n >= 3 else "")
    if r.slope_label == "negativa" or (r.ma_run < 0 and n >= 3):
        return "que baja" + (f" desde hace {n} {units[1] if n != 1 else units[0]}" if n >= 3 else "")
    return "prácticamente plana"


def meaning(r: TimeframeResult, kind: str = "cripto") -> str:
    """Frase que explica la etapa de un marco con sus propios datos."""
    if r.status != "ok":
        return r.message
    units = (_UNITS_STOCK if kind == "accion" else _UNITS)[r.timeframe]
    media = f"su media de {r.ma_len} {units[1]} ({_p(r.ma)})"
    above = r.price > r.ma
    struct = r.structure or ""
    prior = f" ({_pct(r.prior_change)} en la media)" if r.prior_change is not None else ""
    rango = f"entre {_p(r.range_bottom)} y {_p(r.range_top)}"
    if r.stage == 1:
        text = f"La caída previa{prior} se ha frenado: el precio se mueve de lado {rango}"
        if struct.endswith("HL"):
            text += " sin marcar nuevos mínimos"
    elif r.stage == 2:
        pos = "por encima de" if above else "algo por debajo de"
        text = f"Tendencia alcista: el precio ({_p(r.price)}) está {pos} {media}, {_ma_dir(r, units)}"
        if struct == "HH/HL":
            text += ", con máximos y mínimos crecientes"
    elif r.stage == 3:
        text = f"La subida previa{prior} pierde fuerza: la media se aplana y el precio se mueve {rango}"
        if not above:
            text += f", ya por debajo de {media}"
    else:
        pos = "por debajo de" if not above else "rebotando por encima de"
        text = f"Tendencia bajista: el precio ({_p(r.price)}) está {pos} {media}, {_ma_dir(r, units)}"
        if struct == "LH/LL":
            text += ", con máximos y mínimos decrecientes"
    return text + "."


def next_close(r: TimeframeResult, kind: str) -> str:
    """Cuándo cierra la vela en curso de ese marco (texto)."""
    last_open = pd.Timestamp(r.candle_time, tz="UTC")
    if kind == "accion":
        if r.timeframe == "1d":
            return "la vela cierra al final de la próxima sesión"
        if r.timeframe == "1w":
            friday = last_open + pd.Timedelta(days=11)
            return f"la vela cierra el viernes {friday:%d/%m}"
        end = (last_open + pd.DateOffset(months=2)) - pd.Timedelta(days=1)
        return f"la vela cierra el último día de bolsa del mes, hacia el {end:%d/%m}"
    close = candle_close_time(candle_close_time(last_open, r.timeframe), r.timeframe)
    day = close - pd.Timedelta(seconds=1)
    return f"la vela cierra el {_WEEKDAYS[day.weekday()]} {day:%d/%m} a las 24:00 UTC"


def key_zone(asset: AssetResult) -> list[str]:
    """Niveles que el precio actual ya ha cruzado o tiene muy cerca (a menos de NEAR_ATR × ATR),
    con su consecuencia."""
    price = asset.last_price
    if asset.error or price is None:
        return []
    lines = []
    for k in ORDER:
        r = asset.timeframes[k]
        if r.status != "ok" or r.atr is None:
            continue
        up = r.stage in (1, 2)
        marco = _CANDLE[r.timeframe]
        for level, name, consequences, cross_up in (
                (r.confirm_level, "que confirma", _ON_CONFIRM, up),
                (r.invalid_level, "que invalida", _ON_INVALID, not up)):
            if level is None:
                continue
            crossed = price > level if cross_up else price < level
            dist = abs(price - level)
            if not crossed and dist > NEAR_ATR * r.atr:
                continue
            side = "por encima" if cross_up else "por debajo"
            when = next_close(r, asset.kind)
            if crossed:
                head = (f"{r.label}: el precio actual ({_p(price)}) ya está {side} del nivel {name} "
                        f"({_p(level)}).")
            else:
                head = (f"{r.label}: el precio actual ({_p(price)}) está a {dist / price:.1%} del nivel "
                        f"{name} ({_p(level)}).")
            lines.append(f"{head} Solo cuenta si la vela {marco} cierra {side} de ese nivel "
                         f"({when}). Si lo hace, {consequences[r.stage]}.")
    return lines


KEY_ZONE_TITLE = "Lo más importante: el precio está en la zona clave"
KEY_ZONE_NOTE = ("Los niveles solo cuentan al cierre de la vela de cada marco; un cruce durante la "
                 "vela puede deshacerse antes del cierre. Interpretación del método de Weinstein, "
                 "no recomendación de inversión.")
