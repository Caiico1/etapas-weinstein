---
name: pool
description: Mejores oportunidades de liquidez concentrada (Uniswap v3, Uniswap v4 y Orca) para un par de criptomonedas. Dado un par como ETH/USDC, BTC/USDT o ETH/BTC y un capital, indica el mejor ciclo (diario, semanal o mensual), el pool concreto (red, DEX, comisión y dirección), el rango exacto de precios ajustado a los ticks del pool y el depósito de cada token. Úsala cuando el usuario pida un pool, un par o una dupla para aportar liquidez, pregunte qué rango poner en Uniswap u Orca, o escriba /pool.
---

# Oportunidades de liquidez concentrada para un par

Todo el cálculo lo hace `python -m etapas.oportunidad` (en la raíz de este proyecto). Tu trabajo es
ejecutarlo y presentar su resultado **sin cambiar ni inventar ningún número**.

## Pasos

1. **Par y capital.** Toma el par del mensaje del usuario (por ejemplo `ETH/USDC`, `WBTC/USDC`,
   `ETH/BTC`) y el capital en dólares. Si no da capital, usa **5000**. Si no da par, pregúntalo.
2. **Ejecuta** desde la raíz del proyecto (tarda entre 1 y 3 minutos: consulta la blockchain,
   Binance y GeckoTerminal respetando sus límites gratuitos):

   ```bash
   python -m etapas.oportunidad ETH/USDC --capital 5000
   ```

   Con `--json` obtienes los mismos datos en JSON, si los necesitas para responder a una pregunta
   concreta. Si el usuario pide una red concreta («en Base»), añade `--red base` (admite varias
   separadas por comas: ethereum, base, arbitrum, solana).
3. **Presenta el resultado** al usuario, en español:
   - Si hay **propuestas** (una o dos): para cada una, el pool (DEX y **versión v3 o v4**, red,
     par, comisión, dirección o identificador y el enlace a Uniswap), el **mínimo y el máximo** a
     introducir, el **depósito** de cada token para su
     capital, la etapa y su idoneidad, el comportamiento histórico del método y la estimación
     neta por ciclo (con y sin margen de seguridad). Copia los números tal cual.
   - Si **no hay propuesta**: dilo claramente y explica el motivo que da la salida (etapa
     desfavorable, poca liquidez o que las comisiones no compensan la pérdida frente a mantener
     los tokens). Recomendar esperar también es una respuesta válida.
   - Muestra el **checklist** de cada propuesta (o, si no hay ninguna, el de la mejor combinación de
     cada ciclo) tal cual lo da el programa, con sus ✅ ⚠️ ❌, el dato de cada fila y la lectura
     final. No lo rellenes ni lo corrijas tú. Un ❌ es un veto; un ⚠️ es un aviso: coméntalos, sobre
     todo «comisiones estimadas por volumen» y «ciclo superior en contra». Que todo esté en verde
     significa que la combinación encaja en el método, no que vaya a salir bien: dilo así.
   - Resume en una frase la tabla de combinaciones evaluadas si ayuda a entender la elección. En esa
     tabla, «IL/ciclo» es la pérdida impermanente y Neto/ciclo = Comisiones/ciclo + IL/ciclo − Gas.
   - Recuerda qué pasa si el precio sale del rango (se queda 100 % en uno de los tokens y deja de
     cobrar comisiones) y cuándo volver a consultar (al cerrar la vela del ciclo propuesto o si el
     precio sale del rango).
4. **Errores.** Si el programa devuelve un error (par no admitido, sin datos), muéstralo tal cual
   con la lista de símbolos disponibles. No intentes calcular tú el rango por otro camino.

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
- Detalle del método: README, sección «Habilidad /pool».
