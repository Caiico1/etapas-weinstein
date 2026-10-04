# etapas — Etapas de Weinstein y rangos de liquidez concentrada (Uniswap / Orca)

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

## Liquidez concentrada (Uniswap v3 / Orca)

Para cada criptomoneda, el informe añade tres perfiles de rango para aportar liquidez, con los
**números exactos** de precio mínimo y máximo que hay que escribir en Uniswap u Orca:

| Perfil | Etapa y volatilidad de | Horizonte | Para qué |
|---|---|---|---|
| Diaria | diario | 1 día | rango estrecho: más comisiones por dólar, revisión diaria |
| Semanal | semanal | 7 días | equilibrio entre comisiones y margen |
| Mensual | mensual | 1 mes | rango amplio y pasivo que tolera la volatilidad |

Código: `etapas/liquidez.py` (cálculo), `etapas/pools.py` (pools y datos on-chain),
`etapas/report_liquidez.py` (presentación), `etapas/posiciones.py` y `etapas/agentes.py`.

**Rango.** Es el mismo método validado que el rango extremo, centrado en el precio actual: cada
borde es el percentil 90 (`LP_QUANTILE`) de lo que se alejaron el máximo y el mínimo de las velas
pasadas, en ATR, multiplicado por el ATR actual. Comprobación fuera de muestra (sep-2026), velas
en las que el precio se quedó dentro todo el periodo:

| | Diario | Semanal | Mensual |
|---|---|---|---|
| BTC | 81 % (±3,6 %) | 81 % (±15 %) | 76 % (±40 %) |
| ETH | 79 % (±5,4 %) | 82 % (±20 %) | 76 % (±52 %) |
| SOL | 78 % (±5,9 %) | 79 % (±31 %) | 78 % (±57 %) |
| ADA | 80 % (±6,6 %) | 82 % (±32 %) | 88 % (±104 %, n = 60) |

**Ajuste a ticks.** Uniswap y Orca solo admiten precios 1,0001^tick con el tick múltiplo del
*tick spacing* del pool. El rango se ajusta **hacia fuera** (nunca se estrecha), teniendo en
cuenta el orden de los tokens y sus decimales. El resultado es el rango que hay que introducir.

**Idoneidad según la etapa** (del marco del perfil):

| Etapa | Idoneidad | Por qué |
|---|---|---|
| 1 y 3 | favorable | el precio oscila en un rango: es donde la liquidez concentrada rinde más |
| 2 | precaución | si sale por arriba, acabas 100 % en USDC y ganas menos que manteniendo el activo |
| 4 | desfavorable | si sale por abajo, acabas 100 % en un activo que sigue cayendo |

En una transición "X→Y" se toma el punto medio del riesgo de X y de Y, redondeando hacia el
lado prudente. Una etapa favorable con el marco superior en etapa 4 baja a precaución.

**Por pool** (fórmulas del whitepaper de Uniswap v3):
- *Depósito*: qué parte va en el activo y qué parte en la moneda estable.
- *Si toca el mín./máx.*: el resultado frente a mantener lo depositado (pérdida impermanente).
- *Comisiones/día para 1.000 $*: tu parte de la liquidez activa (L / (L_pool + L)) × volumen de 24 h
  × comisión del pool × (1 − parte del protocolo). La parte del protocolo se lee on-chain: Uniswap
  tiene activada su comisión de protocolo en estos pools (1/4 de las comisiones; 1/6 en WETH/USDC
  0,30 % de Base) y Orca se queda un 13 %. Supone que el volumen y la liquidez siguen como en las
  últimas 24 h y que el precio sigue dentro del rango. No incluye el gas.
- *Pérdida en el borde = días*: días de comisiones que compensan la pérdida si el precio acaba en
  el borde más desfavorable.

**Pools de referencia** (verificados on-chain en sep-2026; `tests/test_pools_red.py` los vuelve a
comprobar con `ETAPAS_TEST_RED=1`):

| Activo | Pools |
|---|---|
| BTC | Uniswap v3 cbBTC/USDC 0,05 % (Base), WBTC/USDC 0,05 % (Arbitrum), WBTC/USDT 0,05 % (Ethereum) |
| ETH | Uniswap v3 WETH/USDC 0,05 % y 0,30 % (Base), 0,05 % (Arbitrum) y USDC/WETH 0,05 % (Ethereum) |
| SOL | Orca SOL/USDC 0,04 % (Solana). En Uniswap solo hay un pool WETH/SOL de ~1 M$ |
| ADA | ninguno: no está en Uniswap ni en Orca, y Minswap (Cardano) no usa liquidez concentrada |

Para añadir un pool, se añade una línea en `POOLS` de `etapas/pools.py` y se comprueba con el
test de red. Datos gratuitos, sin claves: nodos RPC públicos de publicnode.com y de Solana para
el estado del pool (precio, liquidez activa, comisión de protocolo), y la API de GeckoTerminal
(una petición por red) para el volumen y el TVL. Si alguna fuente falla, el rango sigue
apareciendo y solo falta la estimación de comisiones. El precio de cada pool se compara con el de
Binance, y si difiere más de un 2 % no se estima nada.

### Posiciones abiertas (`posiciones.txt`)

Una línea por posición: `ETH 2410 2930 semanal Base`. El índice muestra si cada una está dentro,
cerca de un borde (a menos del 10 % de su anchura, `LP_EDGE_WARN`) o fuera. La rutina avisa por
correo **cuando cambia de estado**, no cada día: el estado se guarda en el historial. Editar
`posiciones.txt` en GitHub relanza la rutina. **Ojo**: si el repositorio es público, las
posiciones también lo son, tanto en el archivo como en la web publicada.

### Agentes e IA

La rutina funciona como un equipo de agentes con reglas fijas: etapas, rangos, riesgo, contexto y
vigilante de posiciones, más un coordinador que junta sus informes en un dictamen
(`etapas/agentes.py`). Todo se calcula con reglas explícitas, sin coste. El dictamen se guarda
en `out/ia/<SÍMBOLO>.md` con instrucciones para una IA:

- **Gratis**: pega ese archivo en claude.ai y pide la explicación.
- **Con la API de Anthropic** (de pago, opcional): `pip install anthropic`, define
  `ANTHROPIC_API_KEY` y ejecuta `python -m etapas --rutina --ia`. En GitHub: Settings → Secrets and
  variables → Actions, crea el *secret* `ANTHROPIC_API_KEY` y la variable `ETAPAS_IA` = `1`. La respuesta queda en `out/ia/<SÍMBOLO>_claude.md`. El
  modelo solo explica: todos los números salen de los agentes.

Opciones de la línea de comandos: `--posiciones ARCHIVO`, `--ia` y `--sin-liquidez` (no consulta
los pools).

## Habilidad /pool: la mejor oportunidad para un par

En Claude Code, dentro de este proyecto: **`/pool ETH/USDC`** (o «¿qué pool y qué rango para
ETH/BTC con 5.000 $?»). La habilidad (`.claude/skills/pool/SKILL.md`) ejecuta:

```bash
python -m etapas.oportunidad ETH/USDC --capital 5000        # añade --json para datos estructurados
```

y devuelve hasta **dos propuestas** (de ciclos distintos si es posible), cada una con el pool
(red, DEX, comisión y dirección), el **mínimo y el máximo** ajustados a los ticks del pool, el
**depósito** de cada token para el capital y la estimación neta. Si nada compensa, lo dice. Tarda
de 1 a 3 minutos. Código: `etapas/oportunidad.py`, `etapas/descubrir.py`.

**Dónde se crea la posición**: en app.uniswap.org (Pool → New). La aplicación propone v4 por
defecto y tiene un selector para v3. Hay que elegir la versión y la comisión que indica la propuesta:
v3 y v4 son pools distintos, con su propia liquidez y sus propias comisiones.

**Pares**: activo/estable (ETH, BTC, SOL, LINK, UNI, AAVE, ARB y oro tokenizado PAXG/XAUT con USDC o
USDT; `USD` = ambas) y
activo/activo (ETH/BTC, SOL/ETH...). Acepta `WETH`, `WBTC`, `cbBTC` y la estable delante.

**Pools**: solo tokens con dirección verificada on-chain (`TOKENS` en `descubrir.py`), así que un
token falso con el mismo símbolo nunca entra. Uniswap v3 en Ethereum, Base y Arbitrum (se le
pregunta a la fábrica oficial por cada token y comisión), **Uniswap v4** en las mismas redes y
Orca en Solana (candidatos de GeckoTerminal comprobados on-chain).

*Uniswap v4.* Un pool v4 no tiene dirección: se identifica por keccak256(token0, token1, comisión,
tick spacing, hook) y su estado se lee del contrato StateView (direcciones de la documentación
oficial). Se buscan los pools **sin hook** de los cuatro niveles estándar (0,01 %, 0,05 %, 0,30 % y
1 %), con ETH nativo o WETH; los que tienen hook o parámetros a medida no se analizan, porque un
hook cambia las reglas del pool. Comprobado: los identificadores calculados devuelven pools cuyo
precio coincide con el de mercado y que Uniswap muestra con el par esperado. En v4 la comisión del
protocolo se suma a la del pool en lugar de descontarse: el proveedor de liquidez cobra
prácticamente la comisión íntegra (en v3 cede 1/4 o 1/6). Cada propuesta lleva el enlace al pool
en app.uniswap.org e indica si hay que elegir v3 o v4 al crear la posición. Se descartan los pools con menos de 1 M$ de TVL o 50 k$ de
volumen diario: con 5.000 $ en un pool pequeño, la estimación depende demasiado de ti mismo.

**Cálculo, por ciclo (diario, semanal, mensual) y pool**:

1. Etapa del **par** (ETH/BTC se analiza como precio de ETH en BTC) e idoneidad (etapa 4 = descartado).
   Entre dos activos volátiles no hay moneda «de cuenta»: salir del rango por arriba o por abajo
   cuesta lo mismo frente a mantener ambos, así que las etapas 2 y 4 valen «precaución». Además,
   esos pares se analizan siempre en la orientación en que cotizan (ETH/BTC aunque se pida BTC/ETH),
   porque el método del rango mide en precio: así el mismo pool da siempre el mismo rango, que la
   propuesta muestra también invertido.
2. Rango del método validado, ajustado a los ticks del pool. El ciclo diario no se propone en Ethereum.
3. **Histórico del método en el par** (365 días, 104 semanas o 48 meses, sin ver el futuro): fracción
   de ciclos en que el precio no salió del rango y resultado frente a mantener los tokens. Ese
   resultado se mide por unidad de anchura del rango y se reescala a la anchura de hoy, porque el
   método ensancha el rango con la volatilidad. Medido por anchura, varía entre mitades del
   histórico un 1-11 % (ETH, SOL diario, ETH/BTC), frente al 15-51 % de la media sin reescalar.
4. **Comisiones reales** (`pools.realized_fees`): lo que cobró de verdad cada unidad de liquidez
   del pool, leído de la blockchain (`feeGrowthGlobal` en un bloque de hace 7 y 30 días, por nodos
   públicos con histórico), por la liquidez que aportas. Se usa **el menor** de los dos periodos. No
   depende de datos de volumen ni de suponer constante la liquidez activa. Si no hay dato on-chain
   (Solana, o fallo del nodo), se estima: tu liquidez / (liquidez activa + la tuya) × mediana del
   volumen de 30 días × comisión × (1 − parte del protocolo); la tabla indica la fuente de cada fila.
   - La fórmula por volumen se validó día a día contra `feeGrowthGlobal` (real/modelo): v3, 0,996
     (Ethereum 0,05 %), 1,011 (Base 0,05 %) y 1,011 (Base 0,30 %); v4, 1,012 (Ethereum 0,30 %) y
     1,004 (Base 0,30 %). Pero en v4 Ethereum 0,05 % dio 0,55: la liquidez activa cambia mucho
     dentro del día y la estimación con una foto de la liquidez casi dobla lo cobrado. Por eso se
     pasó a las comisiones reales.
   - Lo cobrado por unidad de liquidez es casi igual en todos los pools grandes de un par (ETH/USDC,
     oct-2026: 4,4–5,1 × 10⁻¹⁵ $ al día a 7 días, v3 y v4, en las tres redes), como cabe esperar: si
     un pool pagara más, entraría liquidez. Con la estimación por volumen se desviaba entre 0,6 y
     1,5 veces según el pool, y un pool v3 llegó a proponerse con comisiones sobreestimadas.
5. **Gas** de un reajuste por ciclo: 900.000 unidades de gas al precio actual de cada red (más 0,05 $
   de margen en Base y Arbitrum), o 0,001 SOL en Solana.
6. **Neto por ciclo** = comisiones × fracción de ciclos dentro + resultado esperado frente a mantener
   × capital − gas. **Margen de seguridad**: solo se propone lo que sigue siendo positivo con una
   pérdida frente a mantener un 25 % peor (`SAFETY_LOSS`).

**Qué esperar.** Con estos datos, la liquidez concentrada suele cobrar en comisiones algo parecido
a lo que pierde frente a mantener los tokens. Los estudios sobre Uniswap v3 encuentran lo mismo:
buena parte de las posiciones pierde frente a mantener: en «Impermanent Loss in Uniswap v3»
(Loesch et al., 2021, arXiv:2111.09192), los pools analizados generaron 199,3 M$ de comisiones y
260,1 M$ de pérdida impermanente, y el 49,5 % de los proveedores tuvo rentabilidad negativa. Por
eso la habilidad a menudo responde
«mejor esperar», y cuando propone algo es porque el volumen del pool compensa con margen.
Tests: `tests/test_oportunidad.py` (sin red) y `tests/test_pools_red.py` (con `ETAPAS_TEST_RED=1`).

### Vigilancia de oportunidades (`oportunidades.txt`)

Una línea por par vigilado: `BTC/USDC 5000 base` (par, capital en dólares y, si se quiere, las redes).
La rutina diaria analiza cada uno con `etapas.oportunidad` y lo muestra en el índice. Avisa por
correo **cuando aparece una propuesta**, cuando cambia de pool o de ciclo y cuando deja de cumplir
los criterios; si nunca la hubo, no avisa. El estado se guarda en el historial. Editar
`oportunidades.txt` en GitHub relanza la rutina. Código: `etapas/vigilancia.py`.

Otras opciones de `python -m etapas.oportunidad`: `--red base,arbitrum` limita las redes. Oro
tokenizado: `PAXG/USDC` y `XAUT/USDT` (Ethereum); XAUT se analiza con el histórico de PAXG, porque
los dos son una onza de oro y XAUT cotiza en Binance solo desde marzo de 2026.

**Aerodrome (Base) no está incluido.** Se midió en oct-2026 para cbBTC/USDC: sin stake paga lo mismo
que Uniswap por unidad de liquidez (3,4–3,6 frente a 3,5–3,7 $/día para 5.000 $ en el rango
semanal); con stake paga en el token AERO (4,8–5,5 $/día medidos a 7 y 30 días, 2,6 $/día al ritmo
de emisión de ese momento), que hay que reclamar y vender. Entre el 93 % y el 99 % de su liquidez
está en stake, tiene tres fábricas de pools y sus recompensas cambian cada semana: no mejora el
resultado lo bastante para justificar esa complejidad.

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
- **Avisos por correo** (como mucho uno al día, en la primera ejecución; añadir o quitar valores
  de la lista no genera aviso): si hay novedades, la rutina abre una *issue* en el repositorio y te la
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
- Historial descargado: 1.095 días, 400 semanas y 240 meses (≥ 3 × media + ventana previa). Se
  cuenta en velas, no en fechas, porque cada marco calcula con sus propias velas. Descargar más
  no cambia las etapas (comprobado en diario con 3 y con 7 años: 0 diferencias); los 3 años del
  diario son para que el gráfico tenga contexto.
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
   magnitud (`|MA_t/MA_{t−W} − 1| > 0,8 × ATR% × √W`), por persistencia (2*n* velas seguidas
   con pendiente) o por giro (la media lleva 2*n* velas en contra de la tendencia anterior y ha
   devuelto al menos la mitad de ella, en escala logarítmica). Con la media plana, decide entre
   la etapa 1 (venía de caer) y la 3 (venía de subir).
5. **Rango**: máximo y mínimo de las últimas N velas. Son los niveles de ruptura y de pérdida.
6. **Volumen**: una ruptura del rango con más de 1,5 × el volumen medio (20) suma puntos.
7. **Ruptura del rango**: cierre más allá del techo (o suelo) del rango previo en más de media
   anchura, con el precio del lado de la media y la media girada al menos 2 velas ⇒ etapa 2 (o 4)
   aunque la puntuación diga otra cosa. Ver «Reglas añadidas».
8. **Puntuación** (0–100 por etapa): pendiente 30 %, posición 25 %, estructura 25 %,
   tendencia previa 15 %, volumen 5 %. Se promedia en las últimas *n* velas para que la etapa
   no cambie por una sola vela. **Confianza** = 1ª − 2ª puntuación: alta > 30, media 15–30, baja < 15.
9. **Transición**: si la 2ª etapa es la siguiente del ciclo y la confianza no es alta → "X→Y".
   **Aviso temprano**: en etapa 1, si la media lleva al menos *n* velas seguidas subiendo, el
   precio cierra por encima de ella y lo ha hecho en al menos el 60 % de las últimas N velas → "1→2"
   (y al revés en etapa 3 → "3→4"). La etapa oficial no cambia hasta que se rompe el rango.

**Niveles**

| Etapa | Nivel que confirma | Nivel que invalida |
|---|---|---|
| 1 | Techo del rango + media anchura del rango: el cierre que exige la regla de ruptura (→ etapa 2) | Suelo del rango |
| 2 | Último máximo pivote (o máximo reciente) | Soporte más cercano por debajo: último mínimo pivote, media o techo roto de la base |
| 3 | Suelo del rango − media anchura del rango (pérdida → etapa 4) | Techo del rango |
| 4 | Último mínimo pivote (o mínimo reciente) | Resistencia más cercana por encima: último máximo pivote, media o suelo roto del techo |

Si no hay ningún nivel del lado correcto (por ejemplo, etapa 4 con el precio ya por encima de la
media y del último máximo), el nivel que invalida se muestra como «—»: un nivel que el precio ya
ha superado no puede invalidar nada.

La línea "Métricas" de la salida muestra todos los valores que justifican cada etapa.

### Contexto de mercado (no modifica la etapa)

Al final de cada informe (`etapas/contexto.py`), sin claves ni coste:
- **Titulares** de CoinDesk, Cointelegraph, Decrypt y The Block (RSS): hasta 5 de los últimos 7
  días. Se filtran por nombre (sin distinguir mayúsculas) o por ticker (solo en mayúsculas).
- **Índice de miedo y codicia** cripto (alternative.me): hoy y hace 7 días, 1 mes y 2 meses. Cada valor se busca por su fecha; si falta ese día, se usa el anterior más cercano (hasta 2 días antes).
- **Reuniones de la Reserva Federal**, leídas de su calendario oficial, con un aviso si la
  decisión cae dentro de una vela en curso.
- En el índice, una franja con el sentimiento y la próxima reunión. En los correos, 2 o 3
  titulares por valor con novedades.
- Si una fuente falla, se indica y el resto se genera igual. Los tests no acceden a la red.

### Precio actual, cierre de referencia y "Si cerrara hoy"

- **Precio actual**: el último precio conocido. Es el mismo para los tres marcos y aparece arriba
  de cada informe y en el índice.
- **Cierre de referencia**: la etapa oficial de cada marco se calcula solo con velas cerradas
  (el cierre de ayer, de la semana pasada o del mes pasado), para que no cambie con los vaivenes
  de una vela a medio formar ni genere avisos falsos. Por eso cada marco indica de qué cierre sale.
- **Si cerrara hoy**: la etapa que saldría incluyendo la vela en curso. Es provisional y se
  muestra en cursiva. Da una lectura temprana sin tocar la oficial.
- La etapa se muestra con su color: 1 azul, 2 verde, 3 amarillo y 4 rojo.

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

- **Etapa por ruptura del rango** (`BREAK_FRAC`, `BREAK_RUN`, oct-2026). La puntuación solo daba
  etapa 2 cuando la media tenía pendiente fuerte, y tras una caída larga la media de 30 semanas tarda
  meses en tenerla. BTC, ETH y SOL semanal rompieron su base el 17/08/2026 (BTC: base 57.800–67.300,
  cierre en 77.734 con 1,7 × su volumen) y seis semanas después seguían en «etapa 1, confianza alta».
  Regla: si el cierre supera el techo del rango previo (máximo de las N velas anteriores) en más de
  media anchura de ese rango, el precio está sobre la media y la media lleva al menos 2 velas
  subiendo, la etapa es 2 (confianza «media»), y lo sigue siendo mientras el cierre no vuelva por
  debajo del techo roto ni de la media. Simétrica para la etapa 4. El margen de media anchura es lo
  que distingue una ruptura de un exceso dentro del rango: sin margen, la precisión sintética
  bajaba al 90-91 %; con él queda en 94,2 / 95,5 / 93,1 % (antes 94,2 / 95,5 / 93,2 %). Que caduque
  al perder la media evita que una ruptura antigua cuente en la base siguiente y que el resultado
  dependa del histórico descargado. Cambios en datos reales: BTC, ETH y SOL semanal pasan a etapa 2
  desde el 31/08/2026; SPY semanal a etapa 4 en marzo de 2020 y abril de 2025 (QQQ, en abril de
  2025); ADA semanal a
  etapa 2 en noviembre de 2024; el oro mensual a etapa 2 en junio de 2019. En el diario cambian
  entre 0 y 11 velas de 663 por activo, todas arranques o caídas reales.
- **Niveles coherentes** (oct-2026). El nivel que confirma de una base era el máximo de las últimas N
  velas, incluida la actual: subía con cada nuevo máximo y coincidía con el aviso «ruptura alcista
  del rango». Ahora es el cierre que exige la regla de ruptura. Y el nivel que invalida ya no puede
  ser uno que el precio haya superado (BTC mensual mostraba 82.850 con el cierre en 83.624).

- **Giro de la tendencia previa** (`PRIOR_RETRACE`, sep-2026). En el mensual cripto, el criterio de
  magnitud exige que la media se mueva un 70-220 % en 18 meses, algo imposible en una caída (no
  pasa del −100 %). Tras la caída de 2025-26, la herramienta seguía «recordando» la subida anterior
  (SOL: +112 %) y habría llamado distribución (3) a la base siguiente (1). Con el giro, la tendencia
  previa pasa a bajista cuando la media lleva 2*n* velas en contra y ha devuelto la mitad de la
  subida. Cambios en el histórico: BTC abr-sep 2023, SOL sep-nov 2023 y ADA 2023-24 en mensual, y
  SPY sep-2022 a may-2023 en semanal, pasan de «3» a «1» (fueron bases tras un mercado bajista).
  Precisión en la serie sintética (20 semillas) igual que antes: 94,2 / 95,5 / 93,2 % de media en
  diario, semanal y mensual. Con un umbral más laxo (6 velas seguidas o devolver el 25-33 %), los
  techos sintéticos se leían como bases (peor tramo: 13 %), así que se descartó.
- **Aviso temprano 1→2 y 3→4** (`EARLY_SIDE`, sep-2026). Antes, la transición solo se marcaba si
  la etapa siguiente era la 2ª puntuación. En BTC semanal (sep-2026), con el precio un 18 % sobre
  una media que ya subía, la 2ª era la etapa 4 y no había aviso. Comprobación en BTC, ETH, SOL, ADA,
  SPY, QQQ y GLD: cambios reales a etapa 2 sin aviso previo: 4 de 28 antes, 0 de 38 ahora (hay más porque las bases
  corregidas acaban en etapa 2); precisión del aviso 1→2
  del 100 % en semanal y mensual (76 % en diario, antes 82 %). En el 3→4 semanal la precisión baja
  del 76 % al 65 %, pero los techos sin aviso pasan de 4/21 a 1/19 y el aviso llega con el doble
  de antelación (6 semanas).

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
