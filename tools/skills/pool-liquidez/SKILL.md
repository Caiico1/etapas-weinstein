---
name: pool-liquidez
description: Mejores oportunidades de liquidez concentrada en Uniswap v3 y v4 para un par de criptomonedas. Dado un par como ETH/USDC, BTC/USDT, ETH/BTC o LINK/ETH y un capital, indica el mejor ciclo (diario, semanal o mensual), el pool concreto (red, versión, comisión, dirección y enlace para abrirlo en Uniswap), el rango exacto de precio mínimo y máximo ajustado a los ticks del pool, el depósito de cada token, el stop-loss, los escenarios frente a USDC y la rentabilidad neta estimada, o dice que es mejor esperar. Empieza con una respuesta corta y da las comisiones reales en 24 h, 7 y 30 días (y la semana floja), cuántos días aguanta el precio dentro del rango, por qué lado sale y el resultado sin tocar frente a reajustar. También busca por temática («oro tokenizado», «bitcoin», «ether»): clasifica todos los pools de esa temática (XAUt, PAXG...) por TVL, volumen y APR y analiza cada par. Y sigue una posición ya abierta (comisiones cobradas frente a lo que ha variado). Úsala siempre que el usuario pregunte qué pool elegir o cuál es el más rentable, qué rango poner en Uniswap, dónde aportar liquidez con una dupla, un par o un activo (por ejemplo, oro tokenizado), cuánto depositar de cada token, si le conviene abrir una posición de liquidez ahora, si un APR que ha visto en otro sitio es real, o cómo va la que tiene abierta, aunque no diga «habilidad» ni «pool». Si no dice qué par ni qué activo mirar, usa la habilidad pool-cribado.
---

# Oportunidades de liquidez concentrada para un par

Todo el cálculo lo hace el programa incluido en esta habilidad (`scripts/pool.py`, con el código
en `scripts/etapas/`). Tu trabajo es ejecutarlo y presentar el resultado **sin cambiar ni inventar
ningún número**: cada cifra sale de datos verificados (pools comprobados en la blockchain, fórmula
de comisiones contrastada con las comisiones realmente cobradas, método del rango validado fuera de
muestra). Si tú calcularas o redondearas algo por tu cuenta, el usuario perdería esa garantía.

## Pasos

1. **Par y capital.** Toma el par del mensaje (ETH/USDC, WBTC/USDC, ETH/BTC, LINK/ETH...)
   y el capital en dólares. Si no da capital, usa **5000**. Si en lugar de un par nombra un **activo o
   temática** («oro tokenizado», «oro», «gold», «bitcoin», «ether»), pásalo tal cual en lugar del par
   (`oro`, `bitcoin`, `ether`): el programa busca todos los pools de esa temática. Si no da par ni
   temática y quiere que le digas cuáles merecen la pena, usa la habilidad **pool-cribado**; si no,
   pregúntalo. Si pregunta por una posición que **ya tiene abierta**, ve a «Seguimiento de una
   posición abierta».
2. **Ejecuta** el programa con la ruta de esta habilidad (tarda de 1 a 5 minutos, porque consulta
   la blockchain, Binance y GeckoTerminal respetando sus límites gratuitos):

   ```bash
   python <carpeta de esta habilidad>/scripts/pool.py ETH/USDC --capital 5000
   python <carpeta de esta habilidad>/scripts/pool.py oro --capital 1000
   ```

   - Con `--json` obtienes los mismos datos estructurados, útil para responder a preguntas concretas.
   - Si el usuario pide una red concreta («en Base»), añade `--red base` (admite varias separadas por
     comas: ethereum, base, arbitrum).
   - Si dice que faltan librerías, instálalas (`pip install ccxt pandas numpy`) y vuelve a ejecutar.
   - Si falla por falta de conexión a Internet (en el chat de Claude, el entorno de código puede
     tener la red limitada), díselo al usuario: la habilidad necesita acceso a
     `data-api.binance.vision`, `api.binance.com`, `*.publicnode.com`,
     `api.geckoterminal.com` y los nodos con histórico `eth.drpc.org`, `mainnet.base.org`,
     `base.drpc.org` y `arbitrum-one.public.blastapi.io`. Funciona siempre en Claude Code, en su ordenador.
3. **Presenta el resultado** en español:
   - **Empieza por la «Respuesta corta»** de la salida (campo `respuesta_corta` con `--json`), con
     sus cifras tal cual: el pool, si es rentable o mejor esperar, el APR de comisiones con el precio
     dentro del rango en 24 h → 7 d → 30 d y la semana floja, cuántos días aguanta el rango y por qué
     lado sale, y el resultado **sin tocar** frente a **reajustando**. Deja claro que el APR de
     comisiones (lo que enseñan los agregadores) no es la rentabilidad: el neto descuenta la pérdida
     frente a mantener los tokens. Si la salida avisa de que el dato de 24 h está inflado, dilo.
   - Con una **temática**: la respuesta corta dice qué par da el mejor neto y, si es otro, qué pool
     paga más comisiones y cuál es su neto. Después resume la **clasificación rápida** (pool, TVL,
     volumen de 24 h y APR del pool en 24 h, 7 y 30 días) y di en qué redes se ha buscado (la salida
     dice dónde no hay tokens verificados de esa temática). Los pares entre dos tokens del mismo
     activo (XAUt/PAXG, marcados con ¹) solo se clasifican. Luego, de cada par analizado, lo mismo
     que con un par (propuesta o «mejor esperar», checklist...), sin repetir lo que ya está arriba.
   - Si el usuario trae una cifra de otra herramienta (por ejemplo, un APR del 21 %), compárala con
     las ventanas de 24 h, 7 y 30 días y con el neto: explica qué cuenta cada una.
   - La tabla **«Comisiones, permanencia en el rango y estrategias»** da, para la mejor combinación
     de cada ciclo, esas mismas cifras: úsala si pregunta por otro ciclo o por dejarlo sin tocar.
   - Si hay **propuestas** (una o dos): para cada una, el pool (Uniswap **v3 o v4**, red, par,
     comisión y dirección o identificador), la **URL del pool en Uniswap** escrita completa y tal cual
     la da la línea «Abrir el pool en Uniswap» (campo `enlace_dex` con `--json`), el **mínimo y el máximo** a introducir (y los invertidos si el par no tiene estable), el
     **depósito** de cada token para su capital, la etapa y su idoneidad, el comportamiento histórico
     del método y la estimación neta por ciclo, con y sin margen de seguridad. Copia los números tal cual.
   - De cada propuesta, además: el **fondeo** (tener USDC en esa red; cuánto hay que cambiar para
     abrir, su coste y el neto del primer ciclo), los **escenarios sin comisiones** frente a USDC (qué
     pasa en dólares si el precio toca el mínimo o el máximo) y el **stop-loss** (precio, pérdida en
     dólares si salta y cuántas veces saltó en el histórico). Uniswap no tiene stop-loss nativo:
     alerta de precio y cierre a mano, o una automatización de salida.
   - Si **no hay propuesta**, dilo claramente con el motivo que da la salida: etapa desfavorable, poca
     liquidez o que las comisiones no compensan la pérdida frente a mantener los tokens. «Mejor
     esperar» es una respuesta válida y frecuente: en Uniswap v3, cerca de la mitad de los proveedores
     de liquidez pierden frente a mantener. Da también la regla de **capital parado** que muestra la
     salida (mantener USDC en la red del pool mejor situado).
   - Muestra el **checklist** de cada propuesta (o, si no hay ninguna, el de la mejor combinación de
     cada ciclo) tal cual lo da el programa, con sus ✅ ⚠️ ❌, el dato de cada fila y la lectura
     final. No lo rellenes ni lo corrijas tú. Un ❌ es un veto; un ⚠️ es un aviso: coméntalos, sobre
     todo «comisiones estimadas por volumen» y «ciclo superior en contra». Que todo esté en verde
     significa que la combinación encaja en el método, no que vaya a salir bien: dilo así.
   - Los bloques **Eficiencia** (volumen/TVL > 1; comisiones del pool ≥ 0,1 % diario del TVL) y
     **Riesgo** (protocolo consolidado, respaldo de la estable, tokens custodiados o puenteados) son
     solo avisos: nunca vetan, porque las comisiones reales y el neto ya miden la rentabilidad, y un
     volumen/TVL alto suele venir de volatilidad. Comenta los ⚠️ de USDT (reservas no solo en efectivo
     y letras del Tesoro) y de tokens custodiados (WBTC, cbBTC, oro tokenizado).
   - Si ayuda a entender la elección, resume en una frase la tabla de combinaciones evaluadas. En esa
     tabla, «IL/ciclo» es la pérdida impermanente (resultado esperado frente a mantener los tokens),
     «Swap/ciclo» el cambio de tokens esperado al reajustar, y
     Neto/ciclo = Comisiones/ciclo + IL/ciclo − Gas/reajuste − Swap/ciclo.
   - Recuerda qué pasa si el precio sale del rango (la posición queda 100 % en uno de los tokens y
     deja de cobrar comisiones) y cuándo volver a consultar: al cerrar la vela del ciclo propuesto o
     si el precio sale del rango.
4. **Errores.** Si el programa devuelve un error (par no admitido, sin datos), muéstralo tal cual con
   la lista de símbolos disponibles. No intentes calcular el rango por otro camino.

## Seguimiento de una posición abierta

Si el usuario pregunta cómo va una posición que ya tiene en Uniswap (v3 o v4, Ethereum, Base o
Arbitrum), pídele el **número de la posición** o la **URL** de app.uniswap.org
(`.../positions/v4/base/12345`), el **capital** que puso y el **precio** del par al abrirla, y ejecuta:

```bash
python <carpeta de esta habilidad>/scripts/seguimiento.py https://app.uniswap.org/positions/v4/base/12345 --capital 4500 --precio 2650
python <carpeta de esta habilidad>/scripts/seguimiento.py base 12345 --v4 --capital 4500 --precio 2650 --cobradas 12.5
```

Presenta el estado (dentro o fuera del rango), los tokens y su valor, las comisiones pendientes y la
tabla: variación de la posición sin comisiones, comisiones, resultado frente a dejarlo en USDC y
frente a mantener los dos tokens, con la lectura final («las comisiones compensan» o no). Las
comisiones ya cobradas antes solo cuentan si las indica con `--cobradas`. Solo lee datos públicos de
la blockchain: no necesita su cartera.

## Límites

- Las comisiones y el resultado neto son **estimaciones**: dependen del volumen y la liquidez del
  pool, que cambian cada día. Dilo así.
- No operes por el usuario ni le pidas su cartera o sus claves: él crea la posición en la aplicación
  del DEX con los números que le das.
- Termina recordando que es un análisis automatizado, no una recomendación de inversión.

## Qué cubre

- Pares **activo / estable** (ETH, BTC, LINK, UNI, AAVE y ARB con USDC o USDT; `USD` = ambas) y
  **activo / activo** (ETH/BTC, LINK/ETH...). Admite `WETH`, `WBTC`, `cbBTC` y la
  estable delante (`USDC/ETH`).
- **Oro tokenizado**: XAUt (Tether) y PAXG (Paxos), en Ethereum (no hay tokens de oro verificados en
  Base ni Arbitrum). Pares `XAUT/USD`, `PAXG/USD` o la temática `oro`, que incluye también XAUt/PAXG
  en la clasificación. Las etapas de XAUt se calculan con el histórico de PAXG (mismo precio).
- **Temáticas**: `oro`, `bitcoin` (BTC/USD y ETH/BTC) y `ether` (ETH/USD y ETH/BTC).
- **Uniswap v3 y Uniswap v4** (pools sin hook), en Ethereum, Base y Arbitrum. No busca en Orca
  (Solana), así que un par sin pool en Uniswap (por ejemplo, los de SOL) devuelve «sin pools»: díselo
  tal cual. El ciclo diario no se propone en Ethereum por el coste del gas; los pools con menos de
  1 M$ de TVL se descartan.
- v3 y v4 son pools distintos. La URL de cada propuesta abre el pool exacto en app.uniswap.org; aun
  así, recuerda al usuario que al crear la posición compruebe la **versión** y la **comisión** de la
  propuesta (la aplicación propone v4 por defecto).
- No construyas tú la URL: usa la que da el programa. Si una propuesta no trae URL, dilo.
- Para volver a incluir Orca, basta con añadir `"orca"` a `FUENTES` en `scripts/etapas/descubrir.py`.
- Las comisiones salen de lo que pagó de verdad cada pool (dato de la blockchain, el menor entre 7 y
  30 días). Solo si no hay ese dato se estiman por volumen; la tabla lo indica en «Fuente». Si el
  volumen de GeckoTerminal no cuadra con lo cobrado on-chain, manda la blockchain (sale en «Avisos»).
- Permanencia en el rango y estrategia sin tocar: el rango del método abierto al cierre de cada ciclo
  pasado y dejado 30 días sin tocar, sobre velas diarias (días hasta salir, tiempo dentro, lado de
  salida y resultado frente a mantener).
- Cómo se calcula (resumen al final de la salida del programa): etapa de Weinstein del par, rango del
  método ajustado a ticks, comprobación histórica sin ver el futuro, comisiones realmente pagadas por
  el pool, gas y cambio de tokens de un reajuste por ciclo, margen de seguridad del 25 % sobre la
  pérdida frente a mantener y stop-loss en el precio que el par no alcanzó en el 99 % de los ciclos
  pasados, como mucho un 10 % más allá del borde del rango (validado en el mismo histórico; corta la
  cola de pérdidas frente a USDC, no mejora la media).
