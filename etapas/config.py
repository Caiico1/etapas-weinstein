"""Parámetros del detector de etapas. Todo lo ajustable vive aquí."""
from dataclasses import dataclass


@dataclass(frozen=True)
class TimeframeConfig:
    key: str               # timeframe de ccxt: '1d', '1w', '1M'
    label: str             # nombre para la salida
    ma: int                # media clave (SMA)
    ma_alt: int | None     # media alternativa si no hay historial suficiente
    slope_window: int      # velas para medir la pendiente de la media
    pivot_window: int      # velas a cada lado para confirmar un pivote
    prior_window: int      # ventana de "tendencia previa"
    history: int           # velas a descargar (>= 3 × media + tendencia previa)
    atr: int = 14
    volume_ma: int = 20
    range_lookback: int = 250  # velas pasadas para estimar el rango de fluctuación
    period_name: str = ""      # cómo se llama la vela en curso ("hoy", "esta semana"...)

    @property
    def position_window(self) -> int:
        """N = ventana de pendiente × 2 (posición del precio y rango)."""
        return self.slope_window * 2

    def min_bars(self, ma_len: int) -> int:
        """Velas mínimas para clasificar con una media dada: media + pendiente + posición."""
        return ma_len + self.position_window


TIMEFRAMES: dict[str, TimeframeConfig] = {
    "1d": TimeframeConfig("1d", "Diario", ma=50, ma_alt=None, slope_window=10,
                          pivot_window=5, prior_window=60, history=730,
                          range_lookback=250, period_name="hoy"),
    "1w": TimeframeConfig("1w", "Semanal", ma=30, ma_alt=None, slope_window=5,
                          pivot_window=3, prior_window=30, history=400,
                          range_lookback=104, period_name="esta semana"),
    # Mensual: SMA10 en lugar de la SMA20 original. La SMA20 (≈87 semanas) reaccionaba con más de un
    # año de retraso: no vio la etapa 4 de SPY en 2022 ni el giro de BTC en 2023. SMA10 ≈ 43 semanas,
    # más cerca de la media de 30 semanas de Weinstein.
    "1M": TimeframeConfig("1M", "Mensual", ma=10, ma_alt=6, slope_window=3,
                          pivot_window=2, prior_window=18, history=240,
                          range_lookback=48, period_name="este mes"),
}

# Orden de presentación: contexto → tendencia principal → momento de entrada
ORDER = ["1M", "1w", "1d"]

# La media es "plana" si |pendiente| < FLAT_ATR_FACTOR × ATR%
FLAT_ATR_FACTOR = 0.5

# Tendencia previa "significativa": la media se movió más de PRIOR_SIGNIFICANCE × ATR% × √W
# en la ventana de tendencia previa (W velas)
PRIOR_SIGNIFICANCE = 0.8
# ...o bien la media llevó PRIOR_RUN × ventana de pendiente velas seguidas con pendiente
# no plana en la misma dirección
PRIOR_RUN = 2

# Volumen: una ruptura con más de VOLUME_SPIKE × su media suma confianza
VOLUME_SPIKE = 1.5

# Ponderación de las señales en la puntuación (suma 1)
WEIGHTS = {
    "pendiente": 0.30,
    "posicion": 0.25,
    "estructura": 0.25,
    "previa": 0.15,
    "volumen": 0.05,
}

# Rango estimado de fluctuación: cada extremo usa el cuantil RANGE_QUANTILE de los movimientos
# pasados (0.9 ⇒ cada extremo se supera ~1 de cada 10 velas; ~80 % de velas dentro del rango)
RANGE_QUANTILE = 0.9

# Confianza = puntuación 1ª − puntuación 2ª
CONF_HIGH = 30
CONF_MEDIUM = 15

# Fuentes de datos, en orden de preferencia
# binance_data = API pública de solo datos de Binance (data-api.binance.vision), útil donde
# api.binance.com está bloqueada.
SOURCES = [("binance", "{sym}/USDT"), ("binance_data", "{sym}/USDT"), ("kraken", "{sym}/USD")]

DISCLAIMER = "Análisis técnico automatizado, no es recomendación de inversión."
