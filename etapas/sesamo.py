"""Timing con la media de 20 («Barrio Sésamo», Píldora 3 de La Semana del Trading Rentable).

Reglas de la fuente: las aperturas (A) se hacen en el entorno de la media de 20 y los cierres (C)
lejos de ella; si el precio está lejos de la media, mantenerse al margen. Valen igual para el lado
alcista y el bajista.

Adaptación a la liquidez concentrada (un rango no es largo ni corto):
  - Abrir o reajustar un rango = apertura: solo con el precio en el entorno de la SMA20 del marco
    del ciclo (diaria para el ciclo diario, semanal para el semanal, mensual para el mensual).
  - Cerrar o recoger = cierre: la zona natural es lejos de la SMA20.

«Cerca» y «lejos» son visuales en la fuente; aquí se miden en ATR(14) del mismo marco, para que
signifiquen lo mismo en BTC que en LINK y en el diario que en el mensual:
  cerca < NEAR_ATR ≤ intermedio < FAR_ATR ≤ lejos.

Calibrado (nov-2026, tools/estudio_sma20.py, ~10.000 aperturas del método de /pool en BTC, ETH,
ETH/BTC, SOL y LINK, diario/semanal/mensual): hasta 2 ATR el resultado del rango no cambia con la
distancia; desde 2 ATR cae la fracción de ciclos dentro (semanal 84-87 % → 71 %, y 38 % desde
3 ATR) y empeora la pérdida frente a mantener (semanal −1,1 % → −3,2 % desde 3 ATR). Por eso
«lejos» empieza en 2 ATR y es lo único que frena una apertura.
"""
from dataclasses import dataclass

import pandas as pd

from .indicators import atr as _atr, sma

PERIOD = 20
NEAR_ATR = 1.0
FAR_ATR = 2.0
SOURCE = "Barrio Sésamo, Píldora 3"


@dataclass
class Timing:
    sma: float
    price: float
    atr: float
    dist_atr: float            # (precio − SMA20) / ATR, con signo
    dist_pct: float            # precio / SMA20 − 1
    zone: str                  # "cerca" | "intermedio" | "lejos"

    @property
    def side(self) -> str:
        return "por encima" if self.dist_atr >= 0 else "por debajo"

    @property
    def can_open(self) -> bool:
        return self.zone != "lejos"

    @property
    def close_zone(self) -> bool:
        return self.zone == "lejos"


def zone(dist_atr: float) -> str:
    d = abs(dist_atr)
    return "cerca" if d < NEAR_ATR else "intermedio" if d < FAR_ATR else "lejos"


def timing(candles: pd.DataFrame | None, atr_series: pd.Series | None = None,
           price: float | None = None) -> Timing | None:
    """Distancia del último cierre (o de `price`) a la SMA20 de esas velas, en ATR y en %."""
    if candles is None or len(candles) < PERIOD:
        return None
    s20 = sma(candles["close"], PERIOD).iloc[-1]
    a = (atr_series if atr_series is not None else _atr(candles)).iloc[-1]
    if pd.isna(s20) or pd.isna(a) or a <= 0 or s20 <= 0:
        return None
    p = float(price if price is not None else candles["close"].iloc[-1])
    d = (p - s20) / a
    return Timing(float(s20), p, float(a), float(d), p / s20 - 1, zone(d))


def of_result(r, price: float | None = None) -> Timing | None:
    """Timing de un analysis.TimeframeResult (usa sus velas y su ATR)."""
    if r is None or getattr(r, "status", "") != "ok" or r.candles is None:
        return None
    hist = getattr(r, "history", None)
    return timing(r.candles, hist["atr"] if hist is not None and "atr" in hist else None, price)


def _es(s: str) -> str:
    """'2,951.20' → '2.951,20' (formato español)."""
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def _num(x: float, d: int = 1) -> str:
    return _es(f"{x:.{d}f}")


def _pct(x: float) -> str:
    return _es(f"{x:+.1%}").replace("%", " %")


def describe(t: Timing | None) -> str:
    """'cerca de la SMA20 (2.951,20; +0,4 ATR, +1,2 %)'."""
    if t is None:
        return "sin datos para la SMA20"
    s = _es(f"{t.sma:,.2f}" if t.sma >= 1000 else f"{t.sma:.6g}")
    where = {"cerca": "cerca de", "intermedio": "a media distancia de", "lejos": "lejos de"}[t.zone]
    return f"{where} la SMA20 ({s}; {_es(f'{t.dist_atr:+.1f}')} ATR, {_pct(t.dist_pct)})"


def open_verdict(t: Timing | None) -> str:
    """Dictamen de apertura: 'OPERACIÓN VÁLIDA' o 'MANTENERSE AL MARGEN / NO ENTRAR'."""
    if t is None:
        return "sin datos"
    return "OPERACIÓN VÁLIDA" if t.can_open else "MANTENERSE AL MARGEN / NO ENTRAR"


def wait_reason(t: Timing) -> str:
    return (f"rentable, pero el precio está lejos de la media de 20 ({_num(abs(t.dist_atr))} ATR "
            f"{t.side}, {_pct(t.dist_pct)}): la apertura se hace en su entorno ({SOURCE}); esperar a "
            f"que vuelva a menos de {_num(FAR_ATR, 0)} ATR")


def close_note(t: Timing | None) -> str:
    """Lectura de cierre para una posición abierta."""
    if t is None:
        return ""
    if t.close_zone:
        return (f"precio lejos de la media de 20 ({_num(abs(t.dist_atr))} ATR {t.side}): zona de cierre "
                f"según {SOURCE}; si piensas cerrar o recoger, es buen momento")
    return (f"precio en el entorno de la media de 20 ({_num(abs(t.dist_atr))} ATR {t.side}): no es zona de "
            f"cierre ({SOURCE}); si vas a cerrar, que sea por el stop-loss o por los dos pasos, no por timing")


# ---------------------------------------------------------------- salida de una posición abierta

SALIR, MANTENER, SIN_DATOS = "SALIR", "MANTENER", "sin datos"
CYCLE_WORDS = {"diario": "1d", "diaria": "1d", "semanal": "1w", "mensual": "1M"}
CYCLE_NAMES = {"1d": "diario", "1w": "semanal", "1M": "mensual"}
DEFAULT_CYCLE = "1w"


def parse_cycle(text: str) -> tuple[str, bool]:
    """Ciclo de una posición a partir de su nota ('semanal · v3 Base...'): (clave, indicado)."""
    low = (text or "").lower()
    for word, key in CYCLE_WORDS.items():
        if word in low:
            return key, True
    return DEFAULT_CYCLE, False


def exit_band(t: Timing) -> tuple[float, float]:
    """Precios a los que, con la SMA20 y el ATR de hoy, el precio estaría «lejos» (salida)."""
    return t.sma - FAR_ATR * t.atr, t.sma + FAR_ATR * t.atr


def _price(x: float) -> str:
    return _es(f"{x:,.2f}" if x >= 1000 else f"{x:.6g}")


def exit_signal(t: Timing | None, outside: bool = False) -> tuple[str, str]:
    """Regla de cierre de Barrio Sésamo para una posición abierta: (acción, explicación).
    Lejos de la media de 20 (≥ FAR_ATR) es zona de cierre: SALIR; y reabrir o reajustar es una
    apertura, que espera a que el precio vuelva a su entorno. Si no, MANTENER, con los precios a los
    que saltaría la salida (la banda se mueve con la SMA20 y el ATR de cada vela)."""
    if t is None:
        return SIN_DATOS, "sin velas suficientes para la media de 20"
    lo, hi = exit_band(t)
    if t.close_zone:
        txt = (f"precio lejos de la media de 20 ({_num(abs(t.dist_atr))} ATR {t.side}, {_pct(t.dist_pct)}): "
               f"zona de cierre ({SOURCE}). Cierra la posición o recoge")
        txt += (", y no reajustes hasta que el precio vuelva al entorno de la media "
                f"({_price(lo)} – {_price(hi)})" if outside else
                f"; no reabras hasta que vuelva al entorno de la media ({_price(lo)} – {_price(hi)})")
        return SALIR, txt
    where = "a media distancia de" if t.zone == "intermedio" else "en el entorno de"
    txt = (f"precio {where} la media de 20 ({_num(abs(t.dist_atr))} ATR {t.side}): aún no es zona de cierre. "
           f"La salida salta por debajo de {_price(lo)} o por encima de {_price(hi)}")
    if outside:
        txt += "; el timing permite reajustar (si /pool lo da por rentable)"
    return MANTENER, txt
