Hoy a 03 de Noviembre del 2026:

He leído tu posición en Revert: **#475067, ETH/USDC, Uniswap v4 en Ethereum, comisión 0,01 %**, abierta hace 14 días. El rango es 2.159,9 – 2.690,6 y ETH está a 2.294, así que está dentro del rango, cerca del límite inferior. He rehecho las cuentas a mano y salen los mismos números que muestra Revert.

## Los datos de partida

| | ETH | USDC | Valor hoy (ETH a 2.293,8) |
|---|---|---|---|
| Lo que depositaste | 0,006997 | 20,49 | 36,54 $ |
| Lo que hay ahora en la posición | 0,011367 | 10,16 | 36,24 $ |
| Comisiones cobradas (aún sin reclamar) | 0,000134 | 0,31 | 0,62 $ |
| Gas al abrir | 0,000336 | — | 0,77 $ |

Al bajar ETH, el pool ha ido **comprando ETH con tus USDC**: tenías 0,0070 ETH y ahora tienes 0,0114, y los USDC han pasado de 20,49 a 10,16. Así funciona la liquidez concentrada, y de ahí sale la pérdida por divergencia.

## Ref: HOLD → "¿me ha ido mejor que no hacer nada?"
<img width="2156" height="1254" alt="image" src="https://github.com/user-attachments/assets/76d93841-bb11-4cd6-8e27-ae82e1430f3f" />

Compara la posición con haber dejado **los mismos tokens quietos en la wallet**, todo valorado al precio de **hoy**. Por eso la caída de ETH no le afecta: también habría afectado a los tokens guardados.

| Campo | Valor | Cómo sale |
|---|---|---|
| **Divergence loss** | −0,31 $ | 36,24 (posición) − 36,54 (tokens guardados). Es lo que pierdes solo por cómo el pool cambia unos tokens por otros. |
| **Pool PnL** | +0,31 $ | −0,31 de divergencia + 0,62 de comisiones. Antes del gas. |
| **PnL** | −0,46 $ | 0,31 − 0,77 de gas. |
| **Pool ROI / ROI** | +0,83 % / −1,23 % | Esas cantidades divididas entre lo aportado con el gas incluido (37,31 $). |

**Cómo leerlo:** las comisiones (0,62) pagan de sobra la divergencia (0,31), así que el pool como estrategia va ganando a guardar los tokens. Pero el gas de abrir (0,77) se lo come todo y el resultado neto es −0,46 $.

## Ref: USD → "¿tengo más o menos dólares que cuando entré?"
<img width="2216" height="1268" alt="image" src="https://github.com/user-attachments/assets/004c9bdf-fef0-4fc1-910a-919eff2faba2" />


Compara con el **valor en dólares el día que depositaste**. Por los datos, ETH estaba entonces a unos **2.444 $**, así que lo aportado valía 37,59 $. Aquí sí cuenta la bajada de ETH.

| Campo | Valor | Cómo sale |
|---|---|---|
| **Divergence loss** | −1,36 $ | 36,24 − 37,59. Ojo: con esta referencia este campo **no es solo la pérdida por divergencia**. Junta la caída de ETH (−6 %, unos −1,05 $ sobre el ETH que depositaste) con la divergencia de verdad (−0,31 $). |
| **Pool PnL** | −0,74 $ | −1,36 + 0,62 de comisiones. |
| **PnL** | −1,56 $ | −0,74 − 0,82 de gas, valorado al precio de ETH del día de entrada. |
| **ROI** | −4,05 % | −1,56 / 38,41. |

## Resumen para no confundirte

- **HOLD** contesta si el pool lo hace mejor que guardar los tokens. Es la referencia buena para juzgar **la estrategia de liquidez**. Ahí sí, lo que pone en Divergence loss es la pérdida por divergencia de verdad.
- **USD** contesta cuánto dinero has ganado o perdido. Mezcla la estrategia con lo que ha hecho ETH: aunque el pool lo hiciera perfecto, si ETH baja un 6 % saldrás en negativo.
- **La diferencia entre las dos** (−1,56 frente a −0,46 ≈ −1,10 $) es casi toda la bajada de ETH. Lo que falta es que el gas se valora a precios distintos en cada referencia.
- **Los APR** (fee APR 43 %, APR −108 %) son lo de estos 14 días pasado a un año. Con una posición tan pequeña y tan reciente no dicen mucho.

**Para tener en cuenta:** en Ethereum, con 36 $, el gas pesa más que las comisiones. Cerrar costará otro gas parecido, y en ese caso la referencia HOLD seguiría en negativo aunque el pool funcione bien. Sobre la alternativa sin riesgo: con 37 $ en un T-bill o un monetario en dólares habrías sacado unos céntimos en estas dos semanas, sin riesgo y sin gas. No he mirado los tipos de hoy, así que la cifra exacta no te la doy.
