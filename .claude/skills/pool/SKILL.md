---
name: pool
description: Mejores oportunidades de liquidez concentrada (Uniswap v3, Uniswap v4 y Orca) para un par de criptomonedas. Dado un par como ETH/USDC, BTC/USDT o ETH/BTC y un capital, indica el mejor ciclo (diario, semanal o mensual), el pool concreto (red, DEX, comisión y dirección), el rango exacto de precios ajustado a los ticks del pool, el depósito de cada token, el stop-loss y los escenarios frente a USDC. También sigue una posición ya abierta (comisiones cobradas frente a lo que ha variado). Úsala cuando el usuario pida un pool, un par o una dupla para aportar liquidez, pregunte qué rango poner en Uniswap u Orca, cómo va su posición de liquidez, o escriba /pool. Si no dice qué par mirar, usa /pool-cribado.
---

# Oportunidades de liquidez concentrada para un par

Todo el cálculo lo hace `python -m etapas.oportunidad` (en la raíz de este proyecto). Tu trabajo es
ejecutarlo y presentar su resultado **sin cambiar ni inventar ningún número**.

## Pasos

1. **Par y capital.** Toma el par del mensaje del usuario (por ejemplo `ETH/USDC`, `WBTC/USDC`,
   `ETH/BTC`) y el capital en dólares. Si no da capital, usa **5000**. Si no da par y quiere que le
   digas cuáles merecen la pena, usa **/pool-cribado**; si no, pregúntalo. Si pregunta por una
   posición que **ya tiene abierta**, ve a «Seguimiento de una posición abierta».
2. **Ejecuta** desde la raíz del proyecto (tarda entre 1 y 3 minutos: consulta la blockchain,
   Binance y GeckoTerminal respetando sus límites gratuitos):

   ```bash
   python -m etapas.oportunidad ETH/USDC --capital 5000
   ```

   Con `--json` obtienes los mismos datos en JSON, si los necesitas para responder a una pregunta
   concreta. Si el usuario pide una red concreta («en Base»), añade `--red base` (admite varias
   separadas por comas: ethereum, base, arbitrum, solana).
3. **Presenta el resultado** al usuario, en español, siempre en **dos pasos** y en este orden (la
   misma lógica de toda la herramienta, para abrir, reajustar o salir):
   - **Paso 1 · ¿La etapa permite abrir?** La línea «Paso 1» tal cual: cada ciclo «apta» o
     «precaución». Solo la etapa 2 (alcista) en semanal o mensual da «precaución» (en el histórico
     fue la más floja para un rango, y su pérdida ya se estima solo con los ciclos de esa etapa).
     Ninguna etapa veta por sí sola: no digas que la etapa 4 impide abrir, porque se midió y no rinde peor.
   - **Paso 2 · ¿Es rentable?** La línea «Paso 2» con su neto o el motivo.
   - **Decisión**: abrir, abrir con precaución o vigilar (con cuántas veces las comisiones de hoy
     harían falta, y la opción de dejarlo en `oportunidades.txt` para que la rutina avise).
   - Si hay **propuestas** (una o dos): para cada una, el pool (DEX y **versión v3 o v4**, red,
     par, comisión, **dirección o identificador completo**, sin acortar, y el enlace a Uniswap;
     la «Respuesta corta» también lo trae en la línea «Dirección del pool»), el **mínimo y el máximo** a
     introducir, el **depósito** de cada token para su
     capital, la etapa (paso 1), el comportamiento histórico del método y la estimación
     neta por ciclo (con y sin margen de seguridad). Copia los números tal cual.
   - De cada propuesta, además: el **fondeo** (tener USDC en esa red; cuánto hay que cambiar para
     abrir, su coste y el neto del primer ciclo), los **escenarios sin comisiones** frente a USDC (qué
     pasa en dólares si el precio toca el mínimo o el máximo) y el **stop-loss** (precio, pérdida en
     dólares si salta y cuántas veces saltó en el histórico). Recuerda que Uniswap no tiene stop-loss
     nativo: alerta de precio y cierre a mano, o una automatización de salida.
   - Si **no hay propuesta**, da también la regla de **capital parado** que muestra la salida
     (mantener USDC en la red del pool mejor situado).
   - Si **no hay propuesta** (vigilar): dilo claramente y explica el motivo que da la salida (poca
     liquidez, comisiones que no compensan la pérdida frente a mantener los tokens o que rinde menos
     que Aave). Recomendar vigilar también es una respuesta válida.
   - Muestra el **checklist** de cada propuesta (o, si no hay ninguna, el de la mejor combinación de
     cada ciclo) tal cual lo da el programa, con sus ✅ ⚠️ ❌, el dato de cada fila y la lectura
     final. No lo rellenes ni lo corrijas tú. Un ❌ es un veto; un ⚠️ es un aviso: coméntalos, sobre
     todo «comisiones estimadas por volumen», «comisiones a la baja» y la etapa en precaución. Que todo esté en verde
     significa que la combinación encaja en el método, no que vaya a salir bien: dilo así.
   - Los bloques **Eficiencia** (volumen/TVL > 1; comisiones del pool ≥ 0,1 % diario del TVL) y
     **Riesgo** (protocolo consolidado, respaldo de la estable, tokens custodiados o puenteados) son
     solo avisos: nunca vetan, porque las comisiones reales y el neto ya miden la rentabilidad, y un
     volumen/TVL alto suele venir de volatilidad. Comenta los ⚠️ de USDT (reservas no solo en efectivo
     y letras del Tesoro) y de tokens custodiados (WBTC, cbBTC, oro tokenizado) o puenteados.
   - Resume en una frase la tabla de combinaciones evaluadas si ayuda a entender la elección. En esa
     tabla, «IL/ciclo» es la pérdida impermanente, «Swap/ciclo» el cambio de tokens esperado al
     reajustar, y Neto/ciclo = Comisiones/ciclo + IL/ciclo − Gas − Swap.
   - Recuerda qué pasa si el precio sale del rango (se queda 100 % en uno de los tokens y deja de
     cobrar comisiones) y cuándo volver a consultar (al cerrar la vela del ciclo propuesto o si el
     precio sale del rango).
4. **Errores.** Si el programa devuelve un error (par no admitido, sin datos), muéstralo tal cual
   con la lista de símbolos disponibles. No intentes calcular tú el rango por otro camino.

## Seguimiento de una posición abierta

Si el usuario pregunta cómo va una posición que ya tiene en Uniswap (v3 o v4, Ethereum, Base o
Arbitrum), pídele el **número de la posición** o la **URL** de app.uniswap.org
(`.../positions/v4/base/12345`), el **capital** que puso y el **precio** del par al abrirla, y ejecuta:

```bash
python -m etapas.seguimiento https://app.uniswap.org/positions/v4/base/12345 --capital 4500 --precio 2650
python -m etapas.seguimiento base 12345 --v4 --capital 4500 --precio 2650 --cobradas 12.5
```

Presenta el estado (dentro o fuera del rango), los tokens y su valor, las comisiones pendientes y la
tabla: variación de la posición sin comisiones, comisiones, resultado frente a dejarlo en USDC y
frente a mantener los dos tokens, con la lectura final («las comisiones compensan» o no). Las
comisiones ya cobradas antes solo cuentan si las indica con `--cobradas`. Solo lee datos públicos
de la blockchain: no necesita su cartera. Las posiciones de Orca (Solana) no se pueden seguir aún.

## Probar una estrategia contra el histórico (backtest)

Si el usuario quiere saber cómo habría ido una estrategia en el pasado («¿qué rango habría rendido
más en ETH/USDC?», «¿compensa reajustar?», «¿y con stop-loss?»), usa el histórico horario de
MetaProDB (`D:\MetaProDB`, leído de la blockchain; solo en este PC):

```bash
python -m etapas.backtest "WETH/USDC 0.05%" --red ethereum --desde 2025-01-01 --ancho 5,10,20 --reajuste nunca,fuera --espera 0,24 --stop 0,15
python -m etapas.backtest 0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640 --ancho 10,20 --reajuste nunca,fuera --ventana 30 --paso 7
```

Para comprobar **el propio método de /pool** (rango, stop y veto de etapa de cada ciclo, calculados
con los datos de cada fecha): `--metodo 1d,1w,1M`; con `--variantes` sale también sin veto de etapa
y sin stop, para ver qué aporta cada filtro. Se puede combinar con rangos fijos (`--ancho`) en la
misma tabla. No aplica los vetos de neto estimado ni de Aave.

`--ventana` abre la estrategia en muchas fechas y da la mediana, el peor 10 % y el % de veces con
ganancia: preséntalo antes que un único periodo. Muestra la tabla tal cual y di sus límites: las
comisiones usan el feeGrowth global del pool (sobrestima con rangos muy estrechos en horas muy
movidas) y no hay MEV ni deslizamiento. Si el pool no está en MetaProDB, dilo; se añade en
`C:\Users\javie\Proyectos\metapro-db\pools\<red>.csv`.

## Reglas

- **Nunca** inventes, redondees de otra forma ni «mejores» los números: todos salen del programa y
  están verificados (pools comprobados en la blockchain, fórmula de comisiones contrastada con las
  comisiones reales cobradas, método del rango validado fuera de muestra).
- Las comisiones y el resultado neto son **estimaciones**: dependen del volumen y la liquidez del
  pool, que cambian cada día. Dilo así.
- No operes por el usuario ni le pidas su cartera o claves: él crea la posición en la aplicación
  del DEX con los números que le das.
- Termina recordando que es un análisis automatizado, no una recomendación de inversión.

## Qué cubre

- Pares **activo / estable** (ETH, BTC, SOL, LINK, UNI, AAVE, ARB y oro tokenizado PAXG o XAUT con USDC
  o USDT; `USD` = ambas) y
  **activo / activo** (ETH/BTC, SOL/ETH, SOL/BTC, LINK/ETH...).
- **Uniswap v3** y **Uniswap v4** (pools sin hook) en Ethereum, Base y Arbitrum, y **Orca** en
  Solana. El ciclo diario no se propone en Ethereum por el coste del gas.
- v3 y v4 son pools distintos: recuerda al usuario que, al crear la posición en app.uniswap.org,
  elija la versión y la comisión de la propuesta (la aplicación propone v4 por defecto).
- Las comisiones salen de lo que pagó de verdad cada pool (dato on-chain, el menor entre 7 y 30
  días); solo si no hay ese dato se estiman por volumen, y la tabla lo indica en «Fuente».
- Stop-loss: el precio que el par no alcanzó en el 99 % de los ciclos pasados (el rango usa el 90 %),
  como mucho un 10 % más allá del borde del rango, validado en el mismo histórico que el rango. Su
  papel es cortar la cola de pérdidas frente a USDC; en la media frente a mantener apenas cambia. Con una estable solo hay stop por abajo; entre dos
  volátiles, por los dos lados.
- Detalle del método: README, sección «Habilidad /pool».
