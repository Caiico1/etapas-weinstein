"""Presentación de la liquidez concentrada: texto de consola, sección HTML del informe, celda del
índice y tabla de posiciones abiertas."""
import html

from .liquidez import LiquidityResult, ProfileResult, PoolQuote

LEVEL_COLORS = {"favorable": "#16a34a", "precaución": "#d97706", "desfavorable": "#dc2626",
                "sin datos": "#6b7280"}
POS_COLORS = {"dentro": "#16a34a", "cerca del mínimo": "#d97706", "cerca del máximo": "#d97706",
              "fuera por abajo": "#dc2626", "fuera por arriba": "#dc2626", "sin precio": "#6b7280"}

LP_NOTE = ("Rango = hasta dónde llegó el precio en el 90 % de las velas pasadas de ese marco, por cada "
           "lado, con la volatilidad actual: el precio se mantuvo dentro durante toda la vela ~8 de cada 10 "
           "veces (fuera de muestra). «Si toca el mín./máx.»: resultado frente a haber mantenido lo "
           "depositado, sin contar comisiones. Comisiones para {cap} $: lo que cobró de verdad cada unidad "
           "de liquidez del pool, leído de la blockchain (el menor entre la media de 7 y de 30 días); si "
           "no hay ese dato (Solana), estimación con la mediana del volumen de 30 días. Solo se cobran mientras el precio está "
           "dentro del rango y cambian con el volumen. «Pérdida en el borde»: días "
           "de comisiones que compensan la pérdida si el precio acaba en el borde más desfavorable. No "
           "incluye el gas: en Ethereum (red principal) abrir, cerrar o mover un rango cuesta bastante más "
           "que en Base, Arbitrum o Solana, así que para el perfil diario conviene una de estas.")


POOLS_LIVE = ("Pools: búsqueda de hoy entre todos los de Uniswap v3, Uniswap v4 (sin hook) y Orca del "
              "activo frente a USDC y USDT, con al menos 1 M$ de TVL y 50 k$ de volumen diario. En el perfil "
              "diario no se destaca un pool de Ethereum si hay otro en una red barata. v3 y v4 son pools "
              "distintos: al crear la posición, comprueba la versión y la comisión.")
POOLS_FIXED = ("Pools: hoy la búsqueda en vivo no ha devuelto pools; se muestran los de referencia "
               "(Uniswap v3 / Orca).")


def fmt(x) -> str:
    if x is None:
        return "—"
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    if abs(x) >= 1:
        return f"{x:,.2f}"
    return f"{x:.4g}"


def _pct(x, sign=True) -> str:
    return "—" if x is None else (f"{x:+.1%}" if sign else f"{x:.0%}")


def _money(x) -> str:
    return "—" if x is None else f"{x:,.2f} $"


def _days(x) -> str:
    return "—" if x is None else (f"{x:.0f} días" if x >= 1.5 else f"{x:.1f} días")


# ---------------------------------------------------------------- consola

def liquidity_text(liq: LiquidityResult | None) -> str:
    if liq is None:
        return ""
    out = ["", "Liquidez concentrada (Uniswap v3 y v4 / Orca):", f"  {liq.best}"]
    if liq.note and liq.pools:
        out.append(f"  Nota: {liq.note}")
    for p in liq.profiles:
        if p.low is None:
            out.append(f"  {p.name}: {p.level} · sin rango")
            continue
        w = p.width
        out.append(f"  {p.name} ({p.horizon}): {p.level.upper()} · rango {fmt(p.low)} – {fmt(p.high)} "
                   f"({w[0]:+.1%} / {w[1]:+.1%}) · depósito {_pct(p.asset_share, False)} activo · si toca "
                   f"mín. {_pct(p.loss_low)}, máx. {_pct(p.loss_high)}")
        q = p.main_quote
        if q:
            out.append(f"      {q.pool.label}: {fmt(q.low)} – {fmt(q.high)} (ticks {q.tick_lower}/{q.tick_upper})"
                       f" · ~{_money(q.fee_day)}/día por {liq.capital:,.0f} $ (APR {_pct(q.fee_apr, False)})"
                       f" · pérdida en el borde = {_days(q.breakeven_days)} de comisiones")
        for reason in p.reasons:
            out.append(f"      · {reason}")
    return "\n".join(out)


# ---------------------------------------------------------------- informe HTML

def level_chip(level: str, small: bool = False) -> str:
    size = "font-size:11px;" if small else ""
    return (f'<span class="lvl" style="{size}background:{LEVEL_COLORS.get(level, "#6b7280")}">'
            f'{html.escape(level)}</span>')


def _quote_cells(q: PoolQuote | None, cap: float) -> str:
    if q is None:
        return '<td colspan="4" class="muted">sin estimación (sin datos del pool)</td>'
    return (f'<td><a href="{html.escape(q.pool.url)}" target="_blank" rel="noopener">'
            f'{html.escape(q.pool.label)}</a><br><b>{fmt(q.low)} – {fmt(q.high)}</b>'
            f'<br><span class="muted">ticks {q.tick_lower} / {q.tick_upper}</span></td>'
            f'<td>{_money(q.fee_day)}</td><td>{_pct(q.fee_apr, False)}</td><td>{_days(q.breakeven_days)}</td>')


def _pools_detail(p: ProfileResult, cap: float) -> str:
    rows = []
    for q in p.quotes:
        vol = f"{q.volume_24h / 1e6:,.1f} M$" if q.volume_24h else "—"
        tvl = f"{q.tvl / 1e6:,.1f} M$" if q.tvl else "—"
        err = f'<br><span class="muted">{html.escape(q.error)}</span>' if q.error else ""
        rows.append(f'<tr><td><a href="{html.escape(q.pool.url)}" target="_blank" rel="noopener">'
                    f'{html.escape(q.pool.label)}</a>{err}</td><td>{fmt(q.low)}</td><td>{fmt(q.high)}</td>'
                    f'<td>{q.tick_lower} / {q.tick_upper}</td><td>{fmt(q.pool_price)}</td>'
                    f'<td>{_pct(q.asset_share, False)}</td><td>{vol}</td><td>{tvl}</td>'
                    f'<td>{_money(q.fee_day)}</td><td>{_pct(q.fee_apr, False)}</td>'
                    f'<td>{_days(q.breakeven_days)}</td></tr>')
    return ('<table><thead><tr><th>Pool</th><th>Mín. (tick)</th><th>Máx. (tick)</th><th>Ticks</th>'
            '<th>Precio del pool</th><th>% en el activo</th><th>Volumen 24 h</th><th>TVL</th>'
            f'<th>Comisiones/día ({cap:,.0f} $)</th><th>APR est.</th><th>Borde = días</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def liquidity_html(liq: LiquidityResult | None) -> str:
    if liq is None:
        return ""
    cap = liq.capital
    rows, details = [], []
    for p in liq.profiles:
        if p.low is None:
            rows.append(f'<tr><td><b>{html.escape(p.name)}</b></td><td>{level_chip(p.level)}</td>'
                        f'<td colspan="9" class="muted">sin rango: {html.escape(" ".join(p.reasons))}</td></tr>')
            continue
        w = p.width
        rows.append(
            f'<tr><td><b>{html.escape(p.name)}</b><br><span class="muted">{html.escape(p.horizon)}</span></td>'
            f'<td>{level_chip(p.level)}</td>'
            f'<td class="typ">{fmt(p.low)}</td><td class="typ">{fmt(p.high)}</td>'
            f'<td>{w[0]:+.1%} / {w[1]:+.1%}</td>'
            f'<td>{_pct(p.asset_share, False)} activo<br>{_pct(1 - p.asset_share, False)} estable</td>'
            f'<td>{_pct(p.loss_low)} / {_pct(p.loss_high)}</td>'
            + (_quote_cells(p.main_quote, cap) if liq.pools else '<td colspan="4" class="muted">sin pool</td>')
            + "</tr>")
        levels = (f"<li>Dentro del rango está el {', el '.join(html.escape(x) for x in p.levels_inside)}: "
                  f"si se cruza, la etapa cambia aunque la posición siga dentro.</li>") if p.levels_inside else ""
        reasons = "".join(f"<li>{html.escape(r)}</li>" for r in p.reasons)
        pools = (f"<details><summary>Todos los pools ({len(p.quotes)})</summary>{_pools_detail(p, cap)}</details>"
                 if p.quotes else "")
        details.append(f"<h3>{html.escape(p.name)} · {level_chip(p.level, small=True)}</h3>"
                       f"<ul>{reasons}{levels}</ul>{pools}")
    note = f'<p class="note">{html.escape(liq.note)}</p>' if liq.note else ""
    if liq.pools:
        note += f'<p class="note">{html.escape(POOLS_LIVE if liq.live else POOLS_FIXED)}</p>'
    return f"""<div class="liq"><h2>Liquidez concentrada (Uniswap v3 y v4 / Orca)</h2>
<p class="summary">{html.escape(liq.best)}</p>{note}
<div class="wrap"><table><thead><tr><th>Perfil</th><th>Idoneidad</th><th>Precio mín.</th><th>Precio máx.</th>
<th>Anchura</th><th>Depósito</th><th>Si toca el mín. / máx.</th><th>Pool con más comisiones estimadas: rango ajustado a ticks</th>
<th>Comisiones/día ({cap:,.0f} $)</th><th>APR est.</th><th>Pérdida en el borde = días de comisiones</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="note">{html.escape(LP_NOTE.format(cap=f"{cap:,.0f}"))}</p>
{''.join(details)}</div>"""


LIQ_CSS = """
 .liq {{ border-top: 2px solid #e5e7eb; margin-top: 18px; }}
 .liq h2 {{ font-size: 20px; margin-bottom: 4px; }} .liq h3 {{ font-size: 15px; margin: 14px 0 4px; }}
 .lvl {{ display: inline-block; color: #fff; border-radius: 4px; padding: 0 6px; font-weight: 600; }}
""".replace("{{", "{").replace("}}", "}")


# ---------------------------------------------------------------- índice

def index_cell(liq: LiquidityResult | None) -> str:
    if liq is None:
        return '<td class="muted">—</td>'
    parts = []
    for p in liq.profiles:
        rng = f" {fmt(p.low)} – {fmt(p.high)}" if p.low is not None else ""
        parts.append(f'{level_chip(p.level, small=True)} <span class="small">{html.escape(p.name)}:{rng}</span>')
    extra = "" if liq.pools else '<br><span class="muted">sin pool de liquidez concentrada</span>'
    return f'<td>{"<br>".join(parts)}{extra}</td>'


def positions_html(items: list[dict]) -> str:
    if not items:
        return ""
    rows = "".join(
        f'<tr><td><b>{html.escape(i["simbolo"])}</b> {html.escape(i["nota"])}</td>'
        f'<td>{fmt(i["min"])} – {fmt(i["max"])}</td><td>{fmt(i["precio"])}</td>'
        f'<td><b style="color:{POS_COLORS.get(i["estado"], "#666")}">{html.escape(i["estado"])}</b><br>'
        f'<span class="small">{html.escape(i["texto"])}</span></td></tr>' for i in items)
    return ('<h2>Mis posiciones de liquidez (posiciones.txt)</h2><div class="wrap"><table><thead><tr>'
            '<th>Posición</th><th>Rango</th><th>Precio actual</th><th>Estado</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')


def watches_html(items: list[dict]) -> str:
    """Tabla de las oportunidades vigiladas (oportunidades.txt) en el índice."""
    if not items:
        return ""
    rows = []
    for i in items:
        where = ", ".join(r.capitalize() for r in i["redes"]) or "todas las redes"
        color = "#16a34a" if i["hay_propuesta"] else "#6b7280"
        text = html.escape(i["texto"])
        if i.get("enlace"):
            text += f' · <a href="{html.escape(i["enlace"])}" target="_blank" rel="noopener">abrir el pool</a>'
        rows.append(f'<tr><td><b>{html.escape(i["par"])}</b><br><span class="muted">{i["capital"]:,.0f} $ · '
                    f'{html.escape(where)}</span></td><td><b style="color:{color}">'
                    f'{"hay propuesta" if i["hay_propuesta"] else "sin propuesta"}</b><br>'
                    f'<span class="small">{text}</span></td></tr>')
    return ('<h2>Oportunidades de liquidez vigiladas (oportunidades.txt)</h2><div class="wrap"><table><thead><tr>'
            '<th>Par</th><th>Hoy</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
            '<p class="muted">Hay propuesta cuando la etapa no es desfavorable, el pool tiene liquidez suficiente y '
            'el resultado neto estimado es positivo incluso con el margen de seguridad. Son estimaciones.</p>')

