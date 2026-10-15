---
name: defi-rendimiento
description: Rendimiento en DeFi combinando Aave V3 y la liquidez concentrada de Uniswap v3 y v4 (Ethereum, Base, Arbitrum). Lee de la blockchain los mercados de Aave V3 (APY de depósito y de préstamo, uso, LTV, umbral y bonus de liquidación, límites, Modo de Aislamiento, congelados) y explica sus mecanismos (E-Mode, aislamiento, factor de salud, liquidaciones). En la liquidez exige que la posición rinda más que depositar en Aave los mismos tokens y, si es mejor esperar, dice cuánto paga el USDC en Aave. Úsala cuando el usuario pregunte cuánto rinde depositar o cuánto cuesta pedir prestado en Aave, dónde rinde más su USDC o su capital parado, si le compensa más Aave o un pool de liquidez, cómo funcionan E-Mode, el Modo de Aislamiento o las liquidaciones de Aave V3, o quiera combinar Aave con un pool. Para un pool sin mencionar Aave, usa pool-liquidez; para buscar pares, pool-cribado.
---

# Rendimiento DeFi: Aave V3 + liquidez concentrada

Todo el cálculo lo hacen los programas de esta habilidad (`scripts/aave.py` y `scripts/pool.py`,
con el código en `scripts/etapas/`). Tu trabajo es ejecutarlos y presentar el resultado **sin
cambiar ni inventar ningún número**: los datos de Aave salen de sus contratos en la blockchain y los
de los pools, de comisiones realmente cobradas y de un método validado sobre el histórico.

## Qué preguntas resuelve

| Pregunta | Programa |
|---|---|
| ¿Cuánto rinde depositar USDC (u otro token) en Aave? ¿Cuánto cuesta pedir prestado? | `aave.py` |
| ¿Dónde rinde más mi capital parado? | `aave.py` (compara redes) |
| ¿Me compensa más un pool de liquidez o dejarlo en Aave? | `pool.py PAR` o `pool.py TEMÁTICA` |
| ¿Qué es E-Mode, el Modo de Aislamiento, el factor de salud...? | «Conceptos de Aave V3», abajo, y `aave.py` para los valores reales |

## Pasos

1. **Qué quiere saber.** Si pregunta solo por Aave, ejecuta `aave.py`. Si menciona un par (ETH/USDC,
   ETH/BTC...) o un activo o temática (oro, bitcoin, ether), ejecuta `pool.py`. Capital por defecto:
   **5000** $. Red: si la nombra, `--red base` (ethereum, base, arbitrum, separadas por comas).

   ```bash
   python <carpeta de esta habilidad>/scripts/aave.py --red base
   python <carpeta de esta habilidad>/scripts/pool.py ETH/BTC --capital 5000 --red base
   python <carpeta de esta habilidad>/scripts/pool.py oro --capital 1000
   ```

   - `aave.py` tarda segundos; `pool.py`, de 1 a 5 minutos (consulta la blockchain, Binance y
     GeckoTerminal respetando sus límites gratuitos). Con `--json`, datos estructurados.
   - Faltan librerías: `pip install ccxt pandas numpy` y vuelve a ejecutar.
   - Sin conexión (en el chat de Claude la red puede estar limitada): necesita `*.publicnode.com`,
     `data-api.binance.vision`, `api.binance.com`, `api.geckoterminal.com`, `eth.drpc.org`,
     `mainnet.base.org`, `base.drpc.org` y `arbitrum-one.public.blastapi.io`. Díselo al usuario.
2. **Presenta `aave.py`**: la tabla por red y token (APY de depósito, APY de préstamo, uso, depositado
   y límite, LTV, umbral y bonus de liquidación, estado). Señala:
   - el mejor rendimiento para lo que pregunta (normalmente una estable) y en qué red;
   - que el APY es **variable**: sube y baja con el uso del mercado;
   - los estados que no son «normal»: congelado (no admite depósitos ni préstamos nuevos), límite de
     depósito alcanzado, **LTV 0** (está listado pero no da poder de préstamo nuevo, como XAUt hoy),
     Modo de Aislamiento (con su techo de deuda).
3. **Presenta `pool.py`** como en pool-liquidez: primero la «Respuesta corta», el checklist y las
   tablas, copiando las cifras. Además:
   - La fila **«Rinde más que depositar los mismos tokens en Aave»**: compara el neto con margen de
     seguridad con lo que pagaría Aave por los mismos tokens, en la misma proporción. Es el listón
     correcto porque el neto se mide frente a mantener esos tokens; si no lo supera, la posición
     no se propone aunque su neto sea positivo.
   - Si no hay propuesta, la **alternativa sin riesgo de precio**: USDC en Aave en la red indicada,
     con su APY. Recuerda que no está libre de riesgo: contrato inteligente, oráculos, respaldo de la
     estable y, en Base y Arbitrum, caídas del secuenciador.
4. **Errores**: muéstralos tal cual. No calcules por otro camino.

## Conceptos de Aave V3 (Aave V3 Technical Paper, 2022)

Úsalos para explicar; los valores concretos, siempre de `aave.py`.

- **LTV y umbral de liquidación** son distintos: el LTV fija cuánto puedes pedir prestado al abrir
  el préstamo; el umbral, a partir de qué deuda te pueden liquidar. **Factor de salud (HF)** =
  Σ(garantía × umbral) / deuda; por debajo de 1 te liquidan, y por debajo de 0,95 la liquidación
  puede ser total (V3 quitó el límite del 50 %). El liquidador se queda la garantía con el **bonus de
  liquidación** de descuento.
- **E-Mode (Modo de Alta Eficiencia)**: categorías de activos correlacionados (estables entre sí, ETH
  y sus derivados...). Si eliges una categoría, solo puedes pedir prestado activos de ella, y las
  garantías de esa categoría usan su LTV y su umbral, más altos (en el documento, estables con 97 % de
  LTV). Para entrar, toda tu deuda debe ser de esa categoría; para salir, el HF debe seguir ≥ 1.
- **Modo de Aislamiento**: un activo con techo de deuda (en USD) es «aislado». Si lo usas como
  garantía, no puedes usar otras garantías y solo puedes pedir prestadas las estables autorizadas,
  hasta el techo. Solo sales si no tienes deuda.
- **Límites de depósito y de deuda (supply y borrow caps)**: si se alcanzan, no se admiten más.
- **Price Oracle Sentinel** (L2): si el secuenciador cae, hay un periodo de gracia sin liquidaciones
  (salvo HF < 0,95) y sin préstamos nuevos.
- **Gobernanza y roles**: parámetros como LTV o umbrales los cambian la gobernanza o administradores
  de riesgo; pueden bajar y acercar a la liquidación. Es un riesgo a mencionar en préstamos.

## Límites

- **Solo analiza**: nunca firma, envía ni prepara transacciones, ni pide la cartera o las claves.
  El usuario deposita, pide prestado o abre la posición en las aplicaciones de Aave y Uniswap.
- **Todavía no calcula** (no lo improvises): la cobertura de una posición de liquidez con un
  préstamo de Aave (factor de salud y precio de liquidación de esa estrategia), la descomposición
  del resultado en riesgo de mercado y pérdida por arbitraje (LVR), ni E-Mode con sus categorías
  reales. Si lo pide, explica el concepto y di que la habilidad aún no da esas cifras.
- Tokens: los verificados de la herramienta (ETH, BTC —WBTC, cbBTC—, USDC, USDT, LINK, UNI, AAVE,
  ARB, PAXG, XAUt según la red). Solo el mercado principal de Aave V3 de cada red.
- Son estimaciones con los datos de hoy; los tipos de Aave y las comisiones cambian cada día.
  Termina recordando que es un análisis automatizado, no una recomendación de inversión.
