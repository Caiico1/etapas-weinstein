# etapas — Detector de etapas de Weinstein para criptoactivos y acciones

Determina en qué etapa del ciclo de mercado (Stan Weinstein) está un criptoactivo en
**diario, semanal y mensual**, con reglas explícitas y puntuaciones que se pueden auditar.

| Etapa | Nombre | Rasgo principal |
|---|---|---|
| 1 | Inicio del movimiento (base) | Media plana tras una caída; el precio oscila alrededor |
| 2 | Alcista | Precio sobre una media que sube; máximos y mínimos crecientes |
| 3 | Distribución (techo) | Media plana tras una subida; el precio cruza la media |
| 4 | Bajista | Precio bajo una media que baja; máximos y mínimos decrecientes |

## Instalación y uso

```bash
pip install -r requirements.txt
python -m etapas ETH
python -m etapas BTC SOL ETH
python -m etapas BTC --html informes      # informes/etapas_BTC.html con gráficos Plotly
python -m etapas BTC --json               # salida estructurada
python -m etapas BTC --provisional        # añade la lectura con la vela en curso
```

Con `pip install -e .` queda disponible el comando `etapas BTC SOL ETH`.

Tests: `python -m pytest`.

## Lista de seguimiento y rutina automática

- **`watchlist.txt`**: una moneda por línea (símbolo de Binance sin `/USDT`). Para añadir una,
  escríbela; para quitarla, bórrala o ponle `#` delante. Los cambios se aplican en la siguiente
  ejecución.
- `python -m etapas` sin símbolos analiza la lista en la consola.
- **`python -m etapas --rutina`** (o doble clic en `rutina.bat`) analiza toda la lista y deja en `out/`:
  - `index.html`: resumen de todos los activos, con los **cambios de etapa desde la ejecución
    anterior** y enlaces a los informes;
  - `etapas_<SÍMBOLO>.html`: el informe de cada activo;
  - `historial/AAAA-MM-DD.json`: una instantánea por día, que se usa para detectar cambios;
  - `rutina.log`: una línea por ejecución, con los cambios y los avisos;
  - `ultima_ejecucion.txt`: la salida de la última ejecución de `rutina.bat`.
- Si una moneda falla (no existe en Binance ni en Kraken, o hay un error de red), aparece como
  error en el índice y las demás se analizan con normalidad.
- **Automatización**: la tarea "Etapas Weinstein - rutina diaria" del Programador de tareas de
  Windows (opcional; ya no se usa) ejecuta `rutina.bat` cada día a las 08:00. Si el PC está apagado a esa hora, se lanza
  al encenderlo; necesita conexión a Internet. Para cambiar la hora o quitarla, usa el
  Programador de tareas. Si mueves la carpeta del proyecto, hay que actualizar la ruta de la tarea.

## Acciones y ETF (y tokens de Ondo)

En `watchlist.txt` o en la línea de comandos, con el prefijo `accion:` y el ticker de Yahoo
Finance: `accion:NVDA`, `accion:SPY`, `accion:SAN.MC` (bolsa española con `.MC`).

- **Datos**: Yahoo Finance (`yfinance`), con décadas de historial en diario, semanal y mensual.
  Los precios se ajustan por dividendos y splits, es decir, son de rentabilidad total.
- **Mismos parámetros que en cripto**: en acciones, una vela diaria es una sesión de bolsa, así
  que la SMA50 abarca unas 10 semanas, que es su uso clásico. Las fechas son las del mercado local.
- **Token de Ondo**: si la acción tiene token de Ondo (NVDA → NVDAon) cotizando en MEXC o BingX,
  el informe muestra su precio y la diferencia con la acción. Ondo replica la rentabilidad total
  (reinvierte dividendos), así que el token cotiza algo por encima de la acción. Por ejemplo,
  SPYon estaba un +1 % sobre SPY en sep-2026, un año después de su lanzamiento.
- **Por qué se analiza la acción y no el token**: los tokens solo tienen ~13 meses de
  historial (no alcanza para el mensual), su vela diaria se distorsiona los fines de semana (la
  bolsa está cerrada y el token apenas se mueve) y su liquidez es baja (~200 k$/día). Como el
  token sigue a la acción, las etapas y niveles de la acción valen para el token.

## Versión en la nube (GitHub)

El flujo [`.github/workflows/rutina.yml`](.github/workflows/rutina.yml) hace lo mismo que la
tarea de Windows, pero en los servidores de GitHub, así que funciona con el PC apagado:

- Se ejecuta cada día a las 06:07 UTC (08:07 en España en verano), con dos respaldos a las 07:37
  y 10:07 UTC que solo trabajan si ese día aún no se ha ejecutado, porque GitHub no garantiza las
  ejecuciones programadas. También se ejecuta cuando se modifica `watchlist.txt` o el código, y cuando
  se pulsa **Actions → Rutina diaria de etapas → Run workflow**.
- Pasa los tests, ejecuta la rutina y publica `out/` en **GitHub Pages**. El índice queda en la
  raíz de la web.
- Guarda `out/historial/` en el repositorio con un commit automático, para detectar los cambios
  del día siguiente.
- La lista se edita desde la web de GitHub: abre `watchlist.txt`, pulsa el lápiz y luego
  "Commit changes". La web se actualiza en un par de minutos.
- Si la ejecución falla, GitHub envía un correo a la cuenta.
- **Avisos por correo** (como mucho uno al día, en la primera ejecución): si hay novedades, la rutina abre una *issue* en el repositorio y te la
  asigna, y GitHub la envía por correo. Cuenta como novedad un cambio de etapa, una transición
  nueva o un cierre que cruce el nivel que confirma o que invalida. Solo se miran velas nuevas
  cerradas, así que el semanal y el mensual avisan como mucho una vez por vela. Si no hay
  novedades, no llega nada. Para probarlo: Actions → Rutina diaria de etapas → Run workflow →
  marca "Enviar un aviso de prueba por correo". Los correos se configuran en
  github.com/settings/notifications.
- Los servidores de GitHub están en EE. UU., donde `api.binance.com` está bloqueada. Por eso la
  herramienta usa `data-api.binance.vision`, la API pública de solo datos de Binance, antes de
  recurrir a Kraken.

## Datos

- `ccxt`: Binance `<SÍMBOLO>/USDT`; si falla, la API de solo datos de Binance
  (`data-api.binance.vision`, mismas velas); si también falla, Kraken `<SÍMBOLO>/USD`. Si fallan
  todas, se muestra el error de cada fuente y el código de salida es 1.
- Kraken no ofrece velas mensuales: se construyen agregando el diario (Kraken solo da
  unas 720 velas diarias, unos 23 meses, suficiente para la SMA10 mensual).
- **Solo velas cerradas**. La vela en curso se descarta; con `--provisional` se muestra
  aparte, marcada como PROVISIONAL.
- Historial descargado: 730 días, 400 semanas y 240 meses (≥ 3 × media + ventana previa).
- Mensual con poco historial → SMA6 con aviso. Si tampoco alcanza → "datos insuficientes".
  Nunca se inventan datos.

## Algoritmo (por marco temporal y por vela)

Parámetros en [`etapas/config.py`](etapas/config.py):

| Parámetro | Diario | Semanal | Mensual |
|---|---|---|---|
| Media clave (SMA) | 50 | 30 | 10 (alternativa 6) |
| Ventana de pendiente *n* | 10 | 5 | 3 |
| Ventana de pivotes | 5 | 3 | 2 |
| Ventana de tendencia previa *W* | 60 | 30 | 18 |
| ATR | 14 | 14 | 14 |

1. **Pendiente**: `slope = MA_t / MA_{t−n} − 1`. Es *plana* si `|slope| < 0,5 × ATR%`.
   En la puntuación se usa `z = slope / umbral` con una transición gradual centrada en |z| = 1.
   **Persistencia**: si la media lleva más de *n* velas seguidas en la misma dirección y el
   precio está de ese lado, cuenta como pendiente aunque no llegue al umbral.
2. **Posición**: % de cierres sobre la media y número de cruces en las últimas N = 2*n* velas.
   Muchos cruces o un precio repartido a ambos lados indican rango, salvo que el precio esté
   a más de 3 ATR de la media.
3. **Estructura**: pivotes de máximos y mínimos confirmados (sin mirar al futuro). Se comparan
   los dos últimos: HH/HL, LH/LL o mixta. Un cierre que rompe el último pivote cuenta al momento.
4. **Tendencia previa**: la última vela en la que la media tuvo una tendencia significativa, por
   magnitud (`|MA_t/MA_{t−W} − 1| > 0,8 × ATR% × √W`) o por persistencia (2*n* velas seguidas
   con pendiente). Con la media plana, decide entre la etapa 1 (venía de caer) y la 3 (venía de subir).
5. **Rango**: máximo y mínimo de las últimas N velas. Son los niveles de ruptura y de pérdida.
6. **Volumen**: una ruptura del rango con más de 1,5 × el volumen medio (20) suma puntos.
7. **Puntuación** (0–100 por etapa): pendiente 30 %, posición 25 %, estructura 25 %,
   tendencia previa 15 %, volumen 5 %. Se promedia en las últimas *n* velas para que la etapa
   no cambie por una sola vela. **Confianza** = 1ª − 2ª puntuación: alta > 30, media 15–30, baja < 15.
8. **Transición**: si la 2ª etapa es la siguiente del ciclo y la confianza no es alta → "X→Y".

**Niveles**

| Etapa | Nivel que confirma | Nivel que invalida |
|---|---|---|
| 1 | Techo del rango (ruptura → etapa 2) | Suelo del rango |
| 2 | Último máximo pivote (o máximo reciente) | Soporte más cercano por debajo: último mínimo pivote o media |
| 3 | Suelo del rango (pérdida → etapa 4) | Techo del rango |
| 4 | Último mínimo pivote (o mínimo reciente) | Resistencia más cercana por encima: último máximo pivote o media |

La línea "Métricas" de la salida muestra todos los valores que justifican cada etapa.

### "Qué significa" y "Lo más importante: zona clave"

- **Qué significa**: una frase por marco, generada solo con los datos de esa lectura (precio,
  media y su dirección, rango, estructura y tendencia previa). Código en `etapas/explain.py`.
- **Zona clave**: un recuadro destacado cuando el precio actual ya ha cruzado un nivel que
  confirma o que invalida, o está a menos de un cuarto de ATR de uno. Indica qué pasaría si la vela
  cierra así y cuándo cierra. Aparece solo en el informe de cada producto, no en el índice. Los niveles
  solo cuentan al cierre de la vela.

### Rango de fluctuación: típico y extremo (columnas Mín./Máx. típico y Mín./Máx. extremo)

- **Típico**: la mediana de los movimientos pasados. Es hasta dónde suele llegar el precio en una
  vela normal; la mitad de las velas se quedan antes y la otra mitad van más lejos. En la
  comprobación fuera de muestra, BTC lo respetó el 48-50 % de las veces en los tres marcos, y ETH
  mensual algo menos (35-43 %).
- **Extremo**: el percentil 90, que solo 1 de cada 10 velas supera. Es el que se describe abajo.
  Antes se llamaba "estimado".


Es el rango en el que se espera que se mueva el precio durante la **vela en curso** (hoy, esta
semana, este mes), contado desde el último cierre. Mide cuánto se mueve el precio, no hacia dónde:

1. Para cada vela pasada (250 días, 104 semanas o 48 meses) se mide cuántos ATR se alejaron su
   máximo y su mínimo del cierre anterior.
2. Mín. = último cierre − percentil 90 de las bajadas × ATR actual.
   Máx. = último cierre + percentil 90 de las subidas × ATR actual.
3. Así se adapta a la volatilidad actual y a la asimetría real entre subidas y bajadas. El
   percentil se ajusta con `RANGE_QUANTILE` en `config.py`.

Comprobación fuera de muestra (el rango de cada vela se calcula solo con los datos anteriores
a ella), sep-2026:

| | Diario | Semanal | Mensual |
|---|---|---|---|
| Velas dentro del rango (BTC / ETH / SOL) | 81 / 80 / 79 % | 77 / 78 / 77 % | 75 / 75 / 76 % |
| Anchura media del rango | ±4–6 % | ±11–23 % | ±39–57 % |

En semanal y mensual el precio se sale algo más de lo previsto, porque las criptomonedas tienen
movimientos extremos más frecuentes que los que refleja el pasado reciente.

### Reglas añadidas al diseño original (y por qué)

Todas se descubrieron en las pruebas y se pueden ajustar en `classifier.py`/`config.py`:

- **Mensual con SMA10 en lugar de SMA20** (sep-2026). La SMA20 abarca unas 87 semanas y
  reaccionaba con más de un año de retraso: en SPY nunca marcó etapa 4 en 2022 y no volvió a
  etapa 2 hasta 2024; en BTC marcaba etapa 4 durante todo el rebote de 2023. Con SMA10 (≈ 43
  semanas, más cerca de las 30 semanas de Weinstein), SPY pasa por etapa 4 entre octubre de 2022
  y febrero de 2023 y vuelve a 2 en agosto de 2023, y BTC vuelve a 2 en agosto de 2023. En la
  serie sintética acierta algo menos (91-97 % de media frente a 93-100 %).

- **Tendencia previa "con memoria"**: si se mide solo en la ventana inmediata, una base larga
  "olvida" la caída que la precedió y alterna entre 1 y 3.
- **Umbral de tendencia previa ∝ √W y criterio de persistencia**: con un umbral fijo, la caída
  semanal de BTC de 2026 (SMA30 −35 %) no contaba como tendencia y la base posterior salía
  como etapa 3.
- **Atenuación de posición y estructura para las etapas 2 y 4 con la media plana**: por
  definición, esas etapas exigen pendiente.
- **Distancia a la media (> 3 ATR)**: un precio muy alejado de una media aún plana no está en rango.
- **Persistencia de la pendiente**: en el mensual, el ATR% de las criptos llega al 30–60 %, y
  con la regla de 0,5 × ATR% una SMA20 que bajó 11 meses seguidos en 2022 salía "plana".
- **Puntuación suavizada** (media de *n* velas).

## Tests

- `tests/test_synthetic.py`: una serie sintética base → subida → techo → caída → base con
  ruido, para cada marco y con 3 semillas. Se exige ≥ 80 % de acierto en cada tramo y la
  secuencia 1→2→3→4→1. En validación con 20–30 semillas, el peor caso es del 83 %.
  Se excluyen las primeras SMA/2 velas de cada tramo: es el retraso inevitable de una media.
  - La oscilación del rango tiene un periodo más corto que la media. Si dura lo mismo que la
    media, cada vaivén es una mini tendencia según las propias definiciones de etapa.
  - El ruido de cada marco se escala para que la tendencia sea igual de clara en su escala.
- `tests/test_edge_cases.py`: historial insuficiente, mensual con SMA6, serie totalmente plana,
  volumen cero, pico aislado (en tendencia y al final de una base), separación de la vela en
  curso, fallo de la API en ambos exchanges, símbolo inexistente y fallback a Kraken.

## Limitaciones conocidas

- **Retraso de las medias**: el método ve los cambios de etapa con el retraso de su media. En
  BTC semanal, el techo de octubre de 2025 pasó a etapa 3 a finales de noviembre, se marcó
  "3→4" a mediados de enero de 2026 y pasó a 4 a principios de febrero. En mensual, la etapa 4 de 2022 no se confirmó hasta enero de 2023.
- En los marcos altos, las oscilaciones de rango tan largas como la media se leen como mini
  tendencias. Es coherente con las definiciones, pero produce alternancias 1↔2 o 3↔4.
- El umbral de "plana" (0,5 × ATR%) es muy exigente en el mensual cripto: úsalo como contexto.

---
Análisis técnico automatizado, no es recomendación de inversión.
