---
name: pool-cribado
description: Busca por sí sola qué duplas o pares de criptomonedas merecen hoy una posición de liquidez concentrada en Uniswap v3 y v4, sin que el usuario diga cuáles mirar. Filtra todos los pools por TVL, eficiencia (volumen/TVL, comisiones/TVL ≥ 0,1 % diario con media de 7 días), tokens verificados, historial y volumen sospechoso, y a los mejores les aplica el análisis completo de pool-liquidez (etapa, rango, comisiones reales, IL, gas, stop-loss y margen de seguridad). Úsala siempre que el usuario pregunte qué pools o duplas son rentables ahora, dónde poner liquidez sin decir el par, o qué pares le recomiendas para aportar liquidez. Si nombra un activo o temática (oro tokenizado, bitcoin, ether), usa pool-liquidez, que busca todos los pools de esa temática.
---

# Cribado de duplas para liquidez concentrada

Todo el cálculo lo hace el programa incluido en esta habilidad (`scripts/cribado.py`, con el código
en `scripts/etapas/`). Tu trabajo es ejecutarlo y presentar el resultado **sin cambiar ni inventar
ningún número**: si calcularas o redondearas algo por tu cuenta, el usuario perdería la garantía de
que cada cifra sale de datos verificados.

## Pasos

1. **Capital y redes.** Toma el capital en dólares del mensaje (si no lo da, **5000**) y, si las
   nombra, las redes (ethereum, base, arbitrum).
2. **Ejecuta** el programa con la ruta de esta habilidad. La fase 1 tarda segundos; la fase 2
   analiza hasta 5 pares con el método completo y puede tardar de 5 a 25 minutos, por el límite
   gratuito de GeckoTerminal. Avisa al usuario de la espera.

   ```bash
   python <carpeta de esta habilidad>/scripts/cribado.py --capital 5000
   ```

   - `--red base,arbitrum` limita las redes; `--top 3` analiza menos pares (más rápido).
   - `--solo-filtro`: solo la fase 1 (segundos). Úsalo si el usuario quiere una vista rápida, y
     dile que no es una recomendación: falta el análisis completo.
   - `--min-comisiones 0.05` relaja el criterio de comisiones/TVL (por defecto 0,1 % diario). Solo
     si el usuario lo pide.
   - `--json` da los mismos datos estructurados.
   - Si dice que faltan librerías, instálalas (`pip install ccxt pandas numpy`) y vuelve a ejecutar.
   - Si falla por falta de conexión a Internet (en el chat de Claude, el entorno de código puede
     tener la red limitada), díselo al usuario: la habilidad necesita acceso a `yields.llama.fi`,
     `data-api.binance.vision`, `api.binance.com`, `*.publicnode.com`, `api.geckoterminal.com` y los
     nodos con histórico `eth.drpc.org`, `mainnet.base.org`, `base.drpc.org` y
     `arbitrum-one.public.blastapi.io`. Funciona siempre en Claude Code, en su ordenador.
3. **Presenta el resultado** en español:
   - **Duplas recomendadas**: la tabla tal cual (par, ciclo, pool, rango, neto, neto anual, con
     margen, Vol/TVL, comisiones/TVL y número de avisos). Explica que han superado todos los vetos del
     método y que, para el detalle de una (depósito, stop-loss, escenarios frente a USDC, fondeo y
     checklist), hay que usar la habilidad **pool-liquidez** con ese par y capital; ofrécete a hacerlo.
   - Si **no hay ninguna**, dilo claramente con los motivos por par y la regla de capital parado:
     mantener USDC en la red donde vaya a operar. «Esperar» es una respuesta válida y frecuente.
   - Resume la tabla de fase 1 en dos o tres frases: qué pares pasaron el filtro y por qué otros no
     (comisiones bajas, historial corto, hook, volumen sospechoso).
   - **Fuera de la lista verificada**: preséntalo solo como información. No los recomiendes nunca:
     sus tokens no están verificados (podría ser un token falso con el mismo símbolo) y no tienen
     análisis de etapa. Señala los de «volumen sospechoso»: un volumen de decenas de veces el TVL
     suele ser de bots o inflado, y unas comisiones/TVL altas ahí no son fiables.
   - Si aparecen **pools entre estables**, recuerda que USDT (y DAI) no tienen respaldo 1:1 en
     efectivo y letras del Tesoro, y que esos pools solo arriesgan un desanclaje.
4. **Errores.** Si DefiLlama no responde, la fase 1 queda vacía: dilo y propone reintentar en unos
   minutos o usar pool-liquidez con un par concreto.

## Reglas

- Un Vol/TVL o unas comisiones/TVL altas **no** bastan para recomendar: lo que decide es el neto del
  método (comisiones reales − pérdida frente a mantener − gas − cambio de tokens, con margen de
  seguridad). Dilo si el usuario se fija solo en esos ratios.
- Las cifras son estimaciones con los datos de hoy: el volumen y la liquidez cambian cada día.
- No operes por el usuario ni le pidas su cartera o sus claves.
- Termina recordando que es un análisis automatizado, no una recomendación de inversión.

## Qué cubre

- **Fase 1** (DefiLlama Yields, una sola petición): todos los pools de Uniswap v3 y v4 con TVL ≥ 1 M$
  en Ethereum, Base y Arbitrum. Pasan a la fase 2 los pares con algún pool que cumpla a la vez: dos
  tokens de la lista verificada, sin hook, al menos 90 días de historial (efecto Lindy),
  comisiones/TVL ≥ 0,1 % diario (volumen medio de 7 días × comisión) y volumen ≤ 20 × TVL. Las
  estables se agrupan en «USD» (USDC y USDT a la vez).
- **Fase 2**: el análisis completo de pool-liquidez para los 5 mejores pares, con todos sus vetos.
  Se ordenan por neto anual con el margen de seguridad.
- No busca en Orca (Solana), igual que pool-liquidez.
