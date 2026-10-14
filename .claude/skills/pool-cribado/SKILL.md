---
name: pool-cribado
description: Busca por sí sola qué duplas o pares de criptomonedas merecen hoy una posición de liquidez concentrada (Uniswap v3, Uniswap v4 y Orca), sin que el usuario diga cuáles mirar. Filtra todos los pools por TVL, eficiencia (volumen/TVL, comisiones/TVL ≥ 0,1 % diario con media de 7 días), tokens verificados, historial y volumen sospechoso, y a los mejores les aplica el análisis completo de /pool (etapa, rango, comisiones reales, IL, gas, stop-loss y margen de seguridad). Úsala cuando el usuario pregunte qué pools o duplas son rentables ahora, dónde poner liquidez, qué pares le recomiendas, o escriba /pool-cribado.
---

# Cribado de duplas para liquidez concentrada

Todo el cálculo lo hace `python -m etapas.cribado` (en la raíz de este proyecto). Tu trabajo es
ejecutarlo y presentar su resultado **sin cambiar ni inventar ningún número**.

## Pasos

1. **Capital y redes.** Toma el capital en dólares del mensaje (si no lo da, **5000**) y, si las
   nombra, las redes (ethereum, base, arbitrum, solana).
2. **¿Hay un cribado de hoy?** Si existe `out/cribado.json` con `fecha` de hoy y el mismo capital y
   redes (lo deja la rutina diaria si existe `cribado.txt`), úsalo y dilo («cribado de hoy a las
   HH:MM UTC»). Si el usuario pide datos de ahora mismo, ejecuta de nuevo.
3. **Ejecuta** desde la raíz del proyecto, **en segundo plano**: la fase 1 tarda segundos, pero la
   fase 2 analiza hasta 5 pares con /pool y puede tardar de 5 a 25 minutos por el límite gratuito de
   GeckoTerminal.

   ```bash
   python -m etapas.cribado --capital 5000 --guardar out
   ```

   - `--red base,arbitrum` limita las redes; `--top 3` analiza menos pares (más rápido).
   - `--solo-filtro`: solo la fase 1 (segundos). Úsalo si el usuario quiere una vista rápida, y
     dile que no es una recomendación: falta el análisis completo.
   - `--min-comisiones 0.05` relaja el criterio de comisiones/TVL (por defecto 0,1 % diario). Solo
     si el usuario lo pide.
   - `--json` da los mismos datos estructurados.
4. **Presenta el resultado** en español:
   - **Duplas recomendadas**: la tabla tal cual (par, ciclo, pool, rango, neto, neto anual, con
     margen, Vol/TVL, comisiones/TVL y número de avisos). Explica que han superado todos los vetos
     de /pool y que para el detalle de una (depósito, stop-loss, escenarios frente a USDC, fondeo y
     checklist) hay que ejecutar **/pool** con ese par y capital; ofrécete a hacerlo.
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
5. **Errores.** Si DefiLlama no responde, la fase 1 queda vacía: dilo y propone reintentar en unos
   minutos o usar /pool con un par concreto.

## Reglas

- **Nunca** inventes, redondees de otra forma ni «mejores» los números: salen del programa.
- Un Vol/TVL o unas comisiones/TVL altas **no** bastan para recomendar: lo que decide es el neto
  de /pool (comisiones reales − pérdida frente a mantener − gas − cambio de tokens, con margen de
  seguridad). Dilo si el usuario se fija solo en esos ratios.
- Las cifras son estimaciones con los datos de hoy: el volumen y la liquidez cambian cada día.
- No operes por el usuario ni le pidas su cartera o claves.
- Termina recordando que es un análisis automatizado, no una recomendación de inversión.

## Qué cubre

- **Fase 1** (DefiLlama Yields, una sola petición): todos los pools de Uniswap v3, Uniswap v4 y Orca
  con TVL ≥ 1 M$ en Ethereum, Base, Arbitrum y Solana. Pasan a la fase 2 los pares con algún pool
  que cumpla a la vez: dos tokens de la lista verificada (`etapas/descubrir.py`), sin hook, al menos
  90 días de historial, comisiones/TVL ≥ 0,1 % diario (volumen medio de 7 días × comisión) y
  volumen ≤ 20 × TVL. Las estables se agrupan en «USD» (USDC y USDT a la vez).
- **Fase 2**: `etapas.oportunidad.analyze` (el motor de /pool) para los 5 mejores pares, con todos
  sus vetos. Se ordenan por neto anual con el margen de seguridad.
- **Rutina diaria**: si existe `cribado.txt` junto a `watchlist.txt` con una línea
  `CAPITAL [redes]` (por ejemplo `4500 base,arbitrum`), la rutina deja `out/cribado.md` y
  `out/cribado.json` cada día.
