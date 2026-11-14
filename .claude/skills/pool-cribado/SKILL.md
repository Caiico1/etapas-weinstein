---
name: pool-cribado
description: Busca por sí sola qué duplas o pares de criptomonedas merecen hoy una posición de liquidez concentrada (Uniswap v3, Uniswap v4 y Orca), sin que el usuario diga cuáles mirar. Filtra todos los pools por TVL, eficiencia (volumen/TVL, comisiones/TVL con media de 7 días) en dos grupos —pools pequeños con mucho volumen para ciclos cortos y pools grandes con volumen para ciclos largos—, tokens verificados, historial y volumen sospechoso, y a los mejores les aplica el análisis completo de /pool (etapa, rango, comisiones reales, IL, gas, stop-loss y margen de seguridad). Úsala cuando el usuario pregunte qué pools o duplas son rentables ahora, dónde poner liquidez, qué pares le recomiendas, o escriba /pool-cribado.
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
   - `--min-comisiones-grandes 0.03` relaja el criterio de los pools grandes (por defecto 0,05 %
     diario). Solo si el usuario lo pide.
   - `--min-comisiones 0.05` relaja el criterio de los pools de corto plazo (por defecto 0,1 % diario). Solo
     si el usuario lo pide.
   - `--json` da los mismos datos estructurados.
4. **Presenta el resultado** en español:
   - Toda la decisión sigue los **dos pasos** de /pool: paso 1, si la etapa del ciclo permite abrir
     («apta» o «precaución»: solo la etapa 2 en semanal o mensual; ninguna veta por sí sola) y paso 2,
     si es rentable. Resultado: abrir, abrir con precaución o vigilar.
   - **Duplas recomendadas**: la tabla tal cual (par, ciclo, decisión, pool, rango, neto, neto anual, con
     margen, pérdida máxima si salta el stop, días de recuperación, Vol/TVL, comisiones/TVL y número de
     avisos), ordenada por el **menor periodo de recuperación** y, de cada dupla recomendada, la
     **dirección del pool** completa (en v4, el identificador) y su enlace, tal cual los da la lista
     «Direcciones de los pools recomendados». Explica que han pasado los dos pasos de /pool y que
     para el detalle de una (depósito, stop-loss, escenarios frente a USDC, fondeo y checklist) hay
     que ejecutar **/pool** con ese par y capital; ofrécete a hacerlo.
   - **Duplas para vigilar**: las que pasaron el filtro pero no el paso 2, con su paso 1 y el motivo
     (cuántas veces las comisiones de hoy harían falta). Sugiere añadirlas a `oportunidades.txt`.
   - Si **no hay ninguna para abrir**, dilo claramente con los motivos por par y la regla de capital
     parado: mantener USDC en la red donde vaya a operar. «Vigilar» es una respuesta válida y frecuente.
   - **Dos grupos** (criterio de Javier): **corto plazo** = pool pequeño (TVL < 20 M$) con
     volumen ≥ TVL; **largo plazo** = pool grande (TVL ≥ 20 M$) con volumen ≥ 0,3 × TVL. El análisis
     de /pool no propone el ciclo diario en un pool grande; un pool pequeño admite todos los ciclos
     (en el histórico el mensual también funcionó). Di de qué grupo es cada dupla (columna «Grupo»
     de la fase 1). Un ciclo descartado «por tamaño del pool» no es falta de comisiones.
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
  90 días de historial, el criterio de su grupo (comisiones = volumen medio de 7 días × comisión) y volumen
  ≤ 20 × TVL. Corto plazo (TVL < 20 M$): comisiones/TVL ≥ 0,1 % diario y volumen ≥ TVL. Largo
  plazo (TVL ≥ 20 M$): comisiones/TVL ≥ 0,05 % diario y volumen ≥ 0,3 × TVL. La mitad de los pares
  sale de cada grupo; si uno no llena su parte, la completa el otro. Las estables se agrupan en «USD» (USDC y USDT a la vez).
- **Fase 2**: `etapas.oportunidad.analyze` (el motor de /pool) para los 5 mejores pares, con todos
  sus vetos. Se ordenan por el menor periodo de recuperación de la pérdida máxima (días de
  comisiones que cubren lo perdido si salta el stop); a igualdad, por el neto anual con margen.
- **Rutina diaria**: si existe `cribado.txt` junto a `watchlist.txt` con una línea
  `CAPITAL [redes]` (por ejemplo `4500 base,arbitrum`), la rutina deja `out/cribado.md` y
  `out/cribado.json` cada día.
