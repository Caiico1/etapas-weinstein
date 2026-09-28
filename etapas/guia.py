"""Guía de lectura: qué significa cada dato de los informes y cómo interpretarlo (out/guia.html)."""
from pathlib import Path

from .config import DISCLAIMER

_CSS = """
 body { font-family: system-ui, sans-serif; margin: 0; color: #111; background: #fff; line-height: 1.55; }
 main { max-width: 920px; margin: 0 auto; padding: 24px 16px 48px; }
 h1 { margin-bottom: 4px; } h2 { margin-top: 40px; border-bottom: 2px solid #e5e7eb; padding-bottom: 4px; }
 h3 { margin: 24px 0 6px; }
 table { border-collapse: collapse; width: 100%; font-size: 14px; margin: 10px 0 16px; }
 th, td { border: 1px solid #ddd; padding: 7px 10px; text-align: left; vertical-align: top; }
 th { background: #f4f4f5; }
 .wrap { overflow-x: auto; }
 code { background: #f4f4f5; padding: 1px 5px; border-radius: 4px; font-size: 13px; }
 .chip { display: inline-block; min-width: 18px; text-align: center; color: #fff; border-radius: 4px;
         font-weight: 600; padding: 0 5px; }
 .box { background: #f8fafc; border-left: 4px solid #7c3aed; padding: 10px 14px; margin: 12px 0; }
 .warn { background: #fffbeb; border-left: 4px solid #f59e0b; padding: 10px 14px; margin: 12px 0; }
 .toc { columns: 2; font-size: 14px; } .toc a { color: #1d4ed8; }
 .muted { color: #666; font-size: 13px; }
 @media (max-width: 640px) { .toc { columns: 1; } }
"""

_BODY = """
<p><a href="index.html">← Todos los activos</a></p>
<h1>Guía de lectura</h1>
<p class="muted">Qué significa cada dato de los informes y cómo interpretarlo. Los ejemplos usan
valores reales de BTC a finales de septiembre de 2026.</p>

<div class="toc">
<ol>
<li><a href="#idea">La idea en 1 minuto</a></li>
<li><a href="#etapas">Las 4 etapas</a></li>
<li><a href="#marcos">Los 3 marcos temporales</a></li>
<li><a href="#precios">Precio actual, cierre de referencia y «Si cerrara hoy»</a></li>
<li><a href="#tabla">Columnas de la tabla</a></li>
<li><a href="#niveles">Nivel que confirma y nivel que invalida</a></li>
<li><a href="#rangos">Rango típico y rango extremo</a></li>
<li><a href="#tecnicos">Datos técnicos: media, pendiente, estructura, confianza</a></li>
<li><a href="#metricas">Sección «Métricas que justifican cada etapa»</a></li>
<li><a href="#zona">Recuadro «Zona clave»</a></li>
<li><a href="#alineacion">Alineación entre marcos</a></li>
<li><a href="#graficos">Gráficos</a></li>
<li><a href="#indice">Página resumen, cambios y avisos por correo</a></li>
<li><a href="#acciones">Acciones, ETF y token de Ondo</a></li>
<li><a href="#contexto">Contexto de mercado: titulares, sentimiento, Fed</a></li>
<li><a href="#como">Cómo leer un informe paso a paso</a></li>
<li><a href="#limites">Limitaciones</a></li>
</ol>
</div>

<h2 id="idea">1. La idea en 1 minuto</h2>
<p>El método de Stan Weinstein dice que todo activo pasa por un ciclo de 4 etapas:
<b>base → subida → techo → caída</b>, y vuelta a empezar. La herramienta calcula en qué etapa está
cada valor en tres escalas de tiempo (mensual, semanal y diaria) y qué precios harían cambiar esa
lectura.</p>
<div class="box">La pregunta que responde cada informe es: <b>¿en qué fase del ciclo está este valor,
en cada plazo, y qué precio lo confirmaría o lo desmentiría?</b> No dice qué comprar ni cuándo:
es una lectura técnica objetiva.</div>

<h2 id="etapas">2. Las 4 etapas</h2>
<div class="wrap"><table>
<tr><th>Etapa</th><th>Qué pasa</th><th>La media clave…</th><th>El precio…</th></tr>
<tr><td><span class="chip" style="background:#2563eb">1</span> Inicio (base)</td>
<td>Tras una caída, el precio deja de bajar y se mueve de lado. Se forma un suelo.</td>
<td>se aplana después de bajar</td><td>oscila alrededor de la media, sin nuevos mínimos</td></tr>
<tr><td><span class="chip" style="background:#16a34a">2</span> Alcista</td>
<td>Tendencia al alza.</td><td>sube</td><td>está por encima, con máximos y mínimos crecientes</td></tr>
<tr><td><span class="chip" style="background:#eab308">3</span> Distribución (techo)</td>
<td>Tras una subida, el precio deja de subir y se mueve de lado. Se forma un techo.</td>
<td>se aplana después de subir</td><td>cruza la media arriba y abajo, sin nuevos máximos</td></tr>
<tr><td><span class="chip" style="background:#dc2626">4</span> Bajista</td>
<td>Tendencia a la baja.</td><td>baja</td><td>está por debajo, con máximos y mínimos decrecientes</td></tr>
</table></div>
<p>Las etapas 1 y 3 se parecen (media plana, precio de lado). Lo que las distingue es de dónde viene
el precio: si viene de caer es una base (1), si viene de subir es un techo (3).</p>
<p><b>Transición «X→Y»</b> (etiqueta morada, por ejemplo <code>4→1</code>): la etapa oficial es X,
pero las señales ya apuntan a la siguiente etapa del ciclo, Y. Es un aviso temprano, no un cambio
confirmado.</p>

<h2 id="marcos">3. Los 3 marcos temporales</h2>
<div class="wrap"><table>
<tr><th>Marco</th><th>Cada vela es…</th><th>Papel</th><th>Responde a…</th></tr>
<tr><td>Mensual</td><td>un mes</td><td><b>Contexto</b></td><td>¿Cómo está el fondo, a varios años vista?</td></tr>
<tr><td>Semanal</td><td>una semana</td><td><b>Tendencia principal</b></td><td>¿Hay tendencia de verdad? Es el marco que Weinstein usaba para decidir.</td></tr>
<tr><td>Diario</td><td>un día (una sesión en bolsa)</td><td><b>Momento</b></td><td>¿Qué está haciendo ahora mismo?</td></tr>
</table></div>
<p>Cuando cambia la tendencia, los marcos giran siempre en el mismo orden: <b>primero el diario,
después el semanal y el último el mensual</b>. Por eso es normal que no coincidan.</p>

<h2 id="precios">4. Precio actual, cierre de referencia y «Si cerrara hoy»</h2>
<div class="wrap"><table>
<tr><th>Dato</th><th>Qué es</th><th>Cómo interpretarlo</th></tr>
<tr><td><b>Precio actual</b></td><td>El último precio conocido cuando se generó el informe. Es el mismo para los tres marcos.</td>
<td>Es el precio "de ahora". Se compara con los niveles en el recuadro de zona clave.</td></tr>
<tr><td><b>Cierre de referencia</b></td><td>El cierre de la última vela <b>completa</b> de cada marco: ayer, la semana pasada o el mes pasado. Indica cuál: «cierre de agosto», «semana del 21/09», «cierre del 27/09».</td>
<td>La etapa oficial se calcula solo con velas cerradas. Así no cambia con los vaivenes de una vela a medio formar ni genera avisos falsos. Por eso cada marco muestra un precio distinto.</td></tr>
<tr><td><b>Si cerrara hoy</b> <span class="muted">(en cursiva)</span></td><td>La etapa que saldría si la vela en curso cerrara al precio actual.</td>
<td>Es una lectura <b>provisional</b> y adelantada: puede cambiar antes de que cierre la vela. Si coincide con la oficial, la lectura es estable. Si es distinta, puede estar gestándose un cambio, pero no está confirmado.</td></tr>
</table></div>

<h2 id="tabla">5. Columnas de la tabla de cada informe</h2>
<div class="wrap"><table>
<tr><th>Columna</th><th>Qué significa</th></tr>
<tr><td>Marco</td><td>Mensual, semanal o diario (ver <a href="#marcos">apartado 3</a>).</td></tr>
<tr><td>Etapa</td><td>Etapa oficial, con su color, según la última vela cerrada.</td></tr>
<tr><td>Si cerrara hoy</td><td>Etapa provisional con la vela en curso (<a href="#precios">apartado 4</a>).</td></tr>
<tr><td>Qué significa</td><td>Una frase que explica la etapa con los datos de esa lectura: precio, media, dirección de la media, rango y estructura.</td></tr>
<tr><td>Mín./Máx. típico</td><td>Hasta dónde suele llegar el precio en una vela normal (<a href="#rangos">apartado 7</a>).</td></tr>
<tr><td>Mín./Máx. extremo</td><td>Hasta dónde llega el precio en 1 de cada 10 velas, las más movidas (<a href="#rangos">apartado 7</a>).</td></tr>
<tr><td>Nivel que confirma / invalida</td><td>Los dos precios que deciden si la etapa se mantiene o se rompe (<a href="#niveles">apartado 6</a>).</td></tr>
<tr><td>Confianza</td><td>Lo clara que es la lectura (<a href="#tecnicos">apartado 8</a>).</td></tr>
<tr><td>Transición</td><td>«X→Y» si se acerca un cambio de etapa; «—» si no.</td></tr>
<tr><td>Cierre de referencia</td><td>Precio y fecha de la vela cerrada usada (<a href="#precios">apartado 4</a>).</td></tr>
<tr><td>Media clave</td><td>La media móvil y su valor, por ejemplo <code>SMA30 70,477</code> (<a href="#tecnicos">apartado 8</a>).</td></tr>
<tr><td>Pendiente</td><td>Cuánto ha cambiado la media y si se considera plana, positiva o negativa (<a href="#tecnicos">apartado 8</a>).</td></tr>
<tr><td>Estructura</td><td>Si los máximos y mínimos suben o bajan, por ejemplo <code>HH/HL</code> (<a href="#tecnicos">apartado 8</a>).</td></tr>
</table></div>

<h2 id="niveles">6. Nivel que confirma y nivel que invalida</h2>
<p>Son los precios que conviene vigilar. <b>Solo cuentan al cierre de la vela de ese marco</b>: en la
fila semanal, dónde cierra la semana; en la mensual, dónde cierra el mes. Un cruce durante la vela
puede deshacerse antes del cierre.</p>
<div class="wrap"><table>
<tr><th>Etapa</th><th>Nivel que confirma</th><th>Nivel que invalida</th></tr>
<tr><td><span class="chip" style="background:#2563eb">1</span> Base</td><td><b>Techo del rango.</b> Si cierra por encima, la base se rompe al alza y empieza la etapa 2.</td><td><b>Suelo del rango.</b> Si lo pierde, la base falla y la caída puede continuar.</td></tr>
<tr><td><span class="chip" style="background:#16a34a">2</span> Alcista</td><td><b>Último máximo.</b> Si lo supera, la subida continúa.</td><td><b>Soporte más cercano</b> (último mínimo creciente o la media). Si lo pierde, la tendencia se debilita.</td></tr>
<tr><td><span class="chip" style="background:#eab308">3</span> Distribución</td><td><b>Suelo del rango.</b> Si lo pierde, el techo se confirma y empieza la etapa 4.</td><td><b>Techo del rango.</b> Si lo supera, no era un techo y vuelve la etapa 2.</td></tr>
<tr><td><span class="chip" style="background:#dc2626">4</span> Bajista</td><td><b>Último mínimo.</b> Si lo pierde, la caída continúa.</td><td><b>Resistencia más cercana</b> (último máximo o la media). Si la supera, la caída se frena.</td></tr>
</table></div>
<div class="warn">En las etapas 3 y 4, «confirmar» significa confirmar la <b>debilidad</b>: el nivel que confirma está por debajo del precio y el que invalida, por encima.</div>
<p><b>Ejemplo:</b> BTC semanal en etapa 1, con el nivel que confirma en 82.300. Un cierre semanal por encima rompería la base y pasaría a etapa 2.</p>

<h2 id="rangos">7. Rango típico y rango extremo</h2>
<p>Estiman <b>cuánto puede moverse</b> el precio durante la vela en curso (hoy, esta semana, este mes),
contado desde el cierre de referencia. <b>No indican hacia dónde</b>.</p>
<div class="wrap"><table>
<tr><th></th><th>Típico (fondo azul)</th><th>Extremo (fondo gris)</th></tr>
<tr><td>Qué es</td><td>Lo que se mueve una vela normal: la mediana.</td><td>Lo que se mueve una vela muy agitada: el percentil 90.</td></tr>
<tr><td>Cuántas veces se supera</td><td>La mitad de las velas llegan más allá.</td><td>Solo 1 de cada 10.</td></tr>
<tr><td>Para qué sirve</td><td>Expectativas del día a día: ¿es normal este movimiento?</td><td>Dimensionar el riesgo: ¿hasta dónde podría llegar en un periodo fuera de lo normal?</td></tr>
<tr><td>Ejemplo BTC mensual</td><td>72.955 – 87.196 (−7 % / +11 %)</td><td>62.593 – 97.554 (−20 % / +24 %)</td></tr>
</table></div>
<p><b>Cómo se calcula:</b> para cada vela de los últimos 250 días, 104 semanas o 48 meses, se mide
cuánto se alejaron su máximo y su mínimo del cierre anterior, en unidades de ATR. Ese
comportamiento se aplica a la volatilidad actual. Por eso se adapta: es más ancho cuando el
mercado está agitado. Se refiere al <b>máximo y mínimo dentro de la vela</b>, no al cierre. Es
normal que sea asimétrico, porque refleja cómo se ha movido el activo en el pasado. Se comprobó
con datos reales: el típico se respeta ~50 % de las veces y el extremo ~75-80 %.</p>

<h2 id="tecnicos">8. Datos técnicos</h2>
<h3>Media clave (SMA)</h3>
<p>Media móvil simple: el precio medio de cierre de las últimas N velas. Se recalcula con cada vela
nueva y suaviza el ruido para mostrar la tendencia de fondo. Es la <b>línea morada</b> de los gráficos.</p>
<div class="wrap"><table>
<tr><th>Marco</th><th>Media</th><th>Abarca</th></tr>
<tr><td>Diario</td><td>SMA50</td><td>50 días (en bolsa, 50 sesiones ≈ 10 semanas)</td></tr>
<tr><td>Semanal</td><td>SMA30</td><td>30 semanas ≈ 7 meses (la media original de Weinstein)</td></tr>
<tr><td>Mensual</td><td>SMA10</td><td>10 meses (si hay poco historial, SMA6 con aviso)</td></tr>
</table></div>
<p>Precio por encima de la media = domina el comprador; por debajo = domina el vendedor. La media
siempre va con retraso, así que confirma los cambios tarde.</p>

<h3>Pendiente</h3>
<p>Cuánto ha cambiado la media en las últimas velas (10 días, 5 semanas o 3 meses), en %. Se
compara con un <b>umbral</b> que se adapta a la volatilidad (la mitad del ATR%):</p>
<ul>
<li><b>plana</b>: el cambio es menor que el umbral. La media no tiene dirección clara (típico de las etapas 1 y 3).</li>
<li><b>positiva</b> o <b>negativa</b>: la media sube o baja de forma significativa (etapas 2 y 4).</li>
</ul>
<p>Ejemplo: <code>+1.1% plana</code> con umbral ±3,9 % significa que la media apenas se ha movido para lo
volátil que es el activo. La herramienta también tiene en cuenta si la media lleva muchas velas
seguidas en la misma dirección, aunque sea poco a poco.</p>

<h3>Estructura</h3>
<p>Compara los dos últimos <b>pivotes</b>: los picos (máximos) y los valles (mínimos) del precio.</p>
<div class="wrap"><table>
<tr><th>Código</th><th>Significado</th><th>Lectura</th></tr>
<tr><td><code>HH/HL</code></td><td>Máximo más alto / mínimo más alto</td><td>Estructura alcista (etapa 2)</td></tr>
<tr><td><code>LH/LL</code></td><td>Máximo más bajo / mínimo más bajo</td><td>Estructura bajista (etapa 4)</td></tr>
<tr><td><code>LH/HL</code></td><td>Máximos que bajan y mínimos que suben</td><td>Contracción: rango que se estrecha (etapas 1 o 3)</td></tr>
<tr><td><code>HH/LL</code></td><td>Máximos que suben y mínimos que bajan</td><td>Expansión: rango que se ensancha, indeciso (etapas 1 o 3)</td></tr>
<tr><td><code>?</code> / sin pivotes</td><td>Aún no hay suficientes picos o valles</td><td>Sin información de estructura</td></tr>
</table></div>
<p>H = high (máximo), L = low (mínimo); HH = higher high, LH = lower high, HL = higher low, LL = lower low.
Un cierre que rompe el último pivote cuenta de inmediato, sin esperar a que se forme uno nuevo.</p>

<h3>Confianza</h3>
<p>Cada etapa recibe una puntuación de 0 a 100 a partir de cinco señales: pendiente de la media
(30 %), posición del precio respecto a la media (25 %), estructura (25 %), tendencia previa (15 %)
y volumen (5 %). La <b>confianza</b> es la diferencia entre la etapa ganadora y la segunda:</p>
<ul>
<li><b>alta</b> (&gt; 30): lectura clara.</li>
<li><b>media</b> (15-30): lectura razonable, pero con otra etapa cerca.</li>
<li><b>baja</b> (&lt; 15): lectura dudosa, entre dos etapas.</li>
</ul>
<p>El número entre paréntesis, por ejemplo <code>alta (38)</code>, es esa diferencia.</p>

<h3>ATR</h3>
<p>Average True Range: cuánto se mueve de media una vela (de máximo a mínimo, incluidos los saltos
entre velas). Mide la volatilidad y sirve para que los umbrales se adapten a cada activo: lo que es
mucho para el oro es poco para una criptomoneda.</p>

<h2 id="metricas">9. Sección «Métricas que justifican cada etapa»</h2>
<p>Se despliega en cada informe. Muestra los números con los que se ha decidido la etapa:</p>
<div class="wrap"><table>
<tr><th>Métrica</th><th>Qué es</th><th>Interpretación</th></tr>
<tr><td>pendiente X vs umbral ±Y</td><td>Cambio de la media frente al umbral de «plana»</td><td>Ver <a href="#tecnicos">pendiente</a></td></tr>
<tr><td>% de cierres sobre la media</td><td>De las últimas velas (20 días, 10 semanas, 6 meses), cuántas cerraron por encima de la media</td><td>~100 %: fuerza alcista; ~0 %: debilidad; ~50 %: rango</td></tr>
<tr><td>cruces</td><td>Veces que el precio cruzó la media en esas velas</td><td>Muchos cruces = el precio va de lado (etapas 1 o 3)</td></tr>
<tr><td>tendencia previa</td><td>Cuánto se movió la media en la última tendencia significativa, y hasta cuándo duró</td><td>Distingue base (venía de caer) de techo (venía de subir)</td></tr>
<tr><td>rango A–B</td><td>Mínimo y máximo de las últimas velas</td><td>Base de los niveles de las etapas 1 y 3</td></tr>
<tr><td>volumen X× su media</td><td>Volumen de la última vela frente a su media de 20</td><td>Una ruptura con más de 1,5× el volumen medio es más creíble</td></tr>
<tr><td>ruptura alcista/bajista del rango</td><td>El cierre superó el máximo o perdió el mínimo de las velas anteriores</td><td>Posible inicio de movimiento</td></tr>
<tr><td>puntuaciones E1…E4</td><td>Puntuación de 0 a 100 de cada etapa</td><td>Gana la mayor; la confianza es la diferencia con la segunda</td></tr>
<tr><td>rango típico / extremo</td><td>Los rangos del <a href="#rangos">apartado 7</a> con su % respecto al cierre</td><td></td></tr>
</table></div>

<h2 id="zona">10. Recuadro «Lo más importante: el precio está en la zona clave»</h2>
<p>Aparece en el informe de un valor cuando el <b>precio actual</b> ya ha cruzado un nivel que confirma
o que invalida, o está muy cerca (a menos de un cuarto de ATR). Indica qué pasaría si la vela cerrara
así y cuándo cierra. Es la señal de que ese valor está en un momento decisivo.</p>
<div class="warn">Es solo un aviso: hasta que no cierra la vela de ese marco, el cruce no cuenta.</div>

<h2 id="alineacion">11. Alineación entre marcos</h2>
<div class="wrap"><table>
<tr><th>Estado</th><th>Qué significa</th></tr>
<tr><td><b style="color:#16a34a">ALINEADOS en etapa N</b></td><td>Los tres marcos coinciden. Es la situación más clara, por ejemplo tendencia alcista en todos los plazos.</td></tr>
<tr><td><b style="color:#ca8a04">PARCIALMENTE ALINEADOS</b></td><td>Dos marcos coinciden y uno no. Suele ser un retroceso o un rebote dentro de la tendencia.</td></tr>
<tr><td><b style="color:#dc2626">EN CONFLICTO</b></td><td>Los tres marcos dicen cosas distintas. Es típico de los cambios de ciclo, cuando el diario ya ha girado y los demás no.</td></tr>
</table></div>
<p>La frase de debajo interpreta el conflicto: primero compara el semanal (tendencia) con el diario
(momento) y después añade el contexto mensual.</p>

<h2 id="graficos">12. Gráficos</h2>
<ul>
<li><b>Velas</b>: verdes si el periodo cerró al alza, rojas si cerró a la baja. La mecha marca el máximo y el mínimo.</li>
<li><b>Línea morada</b>: la media clave.</li>
<li><b>Fondo de color</b>: la etapa en cada momento del histórico (1 azul, 2 verde, 3 amarillo, 4 rojo).</li>
<li><b>Línea verde discontinua</b>: nivel que confirma. <b>Línea roja discontinua</b>: nivel que invalida.</li>
<li><b>Líneas azules de puntos</b> (etiqueta a la izquierda): rango típico.</li>
<li><b>Líneas grises de puntos</b> (etiqueta a la derecha): rango extremo.</li>
<li>El eje de precios es <b>logarítmico</b>: la misma distancia vertical representa el mismo % de movimiento.</li>
</ul>
<p>Puedes hacer zoom arrastrando sobre el gráfico y volver a la vista inicial con doble clic.</p>

<h2 id="indice">13. Página resumen, cambios y avisos por correo</h2>
<ul>
<li><b>Actualizado</b>: fecha y hora (UTC) de la última ejecución. En España hay que sumar 2 horas en verano y 1 en invierno.</li>
<li><b>Cambios desde…</b>: lo que ha cambiado respecto a la ejecución anterior: cambios de etapa, transiciones nuevas, cierres que cruzan un nivel, y valores añadidos o retirados de la lista.</li>
<li>Cada valor muestra su <b>precio actual</b> y, para cada marco, la etapa, «si cerrara hoy», el cierre de referencia, la confianza, los niveles y los rangos.</li>
<li><b>Correo</b>: como mucho uno al día, solo si hay novedades (no por cambios en la lista). Llega de GitHub con el título «Etapas AAAA-MM-DD: N novedades».</li>
</ul>

<h2 id="acciones">14. Acciones, ETF y token de Ondo</h2>
<p>Las acciones y los ETF se añaden a la lista con <code>accion:TICKER</code> (datos de Yahoo Finance,
ajustados por dividendos). Si existe su versión tokenizada de Ondo (por ejemplo NVDA → NVDAon), el
informe muestra el precio del token y su diferencia con la acción. El token suele cotizar algo por
encima porque reinvierte los dividendos.</p>

<h2 id="contexto">15. Contexto de mercado</h2>
<p>Sección al final de cada informe con información <b>complementaria</b>. <b>No modifica la etapa
ni los niveles</b>, que siguen saliendo solo de las reglas técnicas.</p>
<div class="wrap"><table>
<tr><th>Dato</th><th>Qué es</th><th>Cómo interpretarlo</th></tr>
<tr><td>Titulares recientes</td><td>Hasta 5 noticias de los últimos 7 días que mencionan el valor, de CoinDesk, Cointelegraph, Decrypt y The Block (en inglés), con enlace.</td>
<td>Ayudan a entender qué está pasando. Una noticia no explica por sí sola un movimiento y a menudo llega después de él. Puede colarse alguna que solo lo menciona de pasada.</td></tr>
<tr><td>Índice de miedo y codicia</td><td>De 0 (miedo extremo) a 100 (codicia extrema), hoy y hace 7 días (alternative.me).</td>
<td>Es el ánimo general del mercado cripto, no el de cada moneda. Los extremos suelen coincidir con excesos, pero no marcan el momento de un giro.</td></tr>
<tr><td>Reserva Federal</td><td>Próxima reunión del FOMC, según el calendario oficial.</td>
<td>Sus decisiones sobre los tipos de interés mueven los mercados. Si la decisión cae dentro de una vela en curso, aparece un aviso 📅: el precio puede salirse del rango típico.</td></tr>
</table></div>
<p>En los <b>correos de aviso</b> se añaden los 2 o 3 titulares más recientes de los valores con
novedades. Si una fuente falla, se indica y el resto del informe se genera igual.</p>

<h2 id="como">16. Cómo leer un informe paso a paso</h2>
<ol>
<li><b>Semanal primero</b>: ¿en qué etapa está la tendencia principal?</li>
<li><b>Mensual después</b>: ¿el contexto de fondo acompaña o va en contra?</li>
<li><b>Diario</b>: ¿qué está haciendo ahora? ¿Confirma la tendencia o va a contracorriente?</li>
<li><b>Alineación</b>: resume si los tres marcos coinciden o están en conflicto.</li>
<li><b>Niveles</b>: ¿qué precio, al cierre de qué vela, cambiaría la lectura?</li>
<li><b>Zona clave</b>: ¿hay algún nivel a punto de decidirse?</li>
<li><b>Rangos</b>: ¿cuánto es normal que se mueva en el periodo?</li>
<li><b>Si cerrara hoy</b>: ¿se está gestando algún cambio que aún no es oficial?</li>
<li><b>Contexto</b>: ¿hay noticias o eventos, como una reunión de la Fed, que ayuden a entender el movimiento?</li>
</ol>
<div class="box">Ejemplo, BTC a finales de septiembre de 2026: mensual 4 (fondo bajista), semanal 1
(base tras la caída) y diario 2 (rebote), EN CONFLICTO. La señal que resolvería el conflicto al alza
es un cierre semanal por encima del nivel que confirma del semanal. Mientras no ocurra, es un rebote
dentro de un fondo bajista.</div>

<h2 id="limites">17. Limitaciones</h2>
<ul>
<li>Las medias <b>van con retraso</b>: los cambios de etapa se confirman tarde, sobre todo en el mensual.</li>
<li>Es un <b>análisis técnico automático</b> con reglas fijas. Las noticias se muestran como contexto, pero no intervienen en el cálculo. No tiene en cuenta fundamentales ni tu situación personal.</li>
<li>Los rangos estiman <b>cuánto</b> se mueve el precio, no <b>hacia dónde</b>, y en los periodos de mucha volatilidad el precio se sale de ellos más a menudo.</li>
<li>Las ejecuciones automáticas de GitHub pueden retrasarse horas. Mira la fecha de «Actualizado».</li>
</ul>
"""


def write_guide(out_dir: Path) -> Path:
    page = (f'<!doctype html>\n<html lang="es"><head><meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>Guía de lectura</title>\n<style>{_CSS}</style></head><body><main>'
            f'{_BODY}<p class="muted">{DISCLAIMER}</p></main></body></html>\n')
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "guia.html"
    path.write_text(page, encoding="utf-8")
    return path
