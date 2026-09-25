# etapas — Detector de etapas de Weinstein para criptoactivos

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
  Windows ejecuta `rutina.bat` cada día a las 08:00. Si el PC está apagado a esa hora, se lanza
  al encenderlo; necesita conexión a Internet. Para cambiar la hora o quitarla, usa el
  Programador de tareas. Si mueves la carpeta del proyecto, hay que actualizar la ruta de la tarea.

## Versión en la nube (GitHub)

El flujo [`.github/workflows/rutina.yml`](.github/workflows/rutina.yml) hace lo mismo que la
tarea de Windows, pero en los servidores de GitHub, así que funciona con el PC apagado:

- Se ejecuta cada día a las 06:00 UTC, cuando se modifica `watchlist.txt` o el código, y cuando
  se pulsa **Actions → Rutina diaria de etapas → Run workflow**.
- Pasa los tests, ejecuta la rutina y publica `out/` en **GitHub Pages**. El índice queda en la
  raíz de la web.
- Guarda `out/historial/` en el repositorio con un commit automático, para detectar los cambios
  del día siguiente.
- La lista se edita desde la web de GitHub: abre `watchlist.txt`, pulsa el lápiz y luego
  "Commit changes". La web se actualiza en un par de minutos.
- Si la ejecución falla, GitHub envía un correo a la cuenta.
- Los servidores de GitHub están en EE. UU., donde `api.binance.com` está bloqueada. Por eso la
  herramienta usa `data-api.binance.vision`, la API pública de solo datos de Binance, antes de
  recurrir a Kraken.

## Datos

- `ccxt`: Binance `<SÍMBOLO>/USDT`; si falla, la API de solo datos de Binance
  (`data-api.binance.vision`, mismas velas); si también falla, Kraken `<SÍMBOLO>/USD`. Si fallan
  todas, se muestra el error de cada fuente y el código de salida es 1.
- Kraken no ofrece velas mensuales: se construyen agregando el diario (Kraken solo da
  unas 720 velas diarias, así que el mensual suele pasar a SMA12 y se avisa).
- **Solo velas cerradas**. La vela en curso se descarta; con `--provisional` se muestra
  aparte, marcada como PROVISIONAL.
- Historial descargado: 730 días, 400 semanas y 240 meses (≥ 3 × media + ventana previa).
- Mensual con poco historial → SMA12 con aviso. Si tampoco alcanza → "datos insuficientes".
  Nunca se inventan datos.

## Algoritmo (por marco temporal y por vela)

Parámetros en [`etapas/config.py`](etapas/config.py):

| Parámetro | Diario | Semanal | Mensual |
|---|---|---|---|
| Media clave (SMA) | 50 | 30 | 20 (alternativa 12) |
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

### Reglas añadidas al diseño original (y por qué)

Todas se descubrieron en las pruebas y se pueden ajustar en `classifier.py`/`config.py`:

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
- `tests/test_edge_cases.py`: historial insuficiente, mensual con SMA12, serie totalmente plana,
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
