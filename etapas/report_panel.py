"""Panel visual: matriz de etapas del índice, barras de rango por marco y veredicto de pool de cada
informe. Solo presentación: todos los datos salen del análisis (etapas, rangos y liquidez)."""
import html

from .analysis import AssetResult, TimeframeResult
from . import power4
from .classifier import STAGE_NAMES
from .config import ORDER, TIMEFRAMES
from .liquidez import LiquidityResult
from .report_liquidez import LEVEL_COLORS, fmt

STAGE_COLORS = {1: "#2563eb", 2: "#16a34a", 3: "#eab308", 4: "#dc2626"}
STAGE_TINTS = {1: "#dbeafe", 2: "#dcfce7", 3: "#fef9c3", 4: "#fee2e2"}
STAGE_SHORT = {1: "Base", 2: "Alcista", 3: "Distribución", 4: "Bajista"}
_PROFILE_KEY = {"Diaria": "1d", "Semanal": "1w", "Mensual": "1M"}

PANEL_CSS = """
 .matrix { border-collapse: separate; border-spacing: 4px; font-size: 14px; margin: 8px 0 4px; }
 .matrix th { background: none; border: none; color: #555; font-size: 12px; text-transform: uppercase;
              letter-spacing: .04em; padding: 2px 8px; text-align: center; }
 .matrix td { border: none; border-radius: 8px; padding: 8px 10px; text-align: center; vertical-align: middle;
              min-width: 110px; }
 .matrix td.msym { text-align: left; background: #f4f4f5; min-width: 120px; }
 .matrix td.msym a { font-weight: 700; font-size: 15px; color: #111; text-decoration: none; }
 .matrix td.msym a:hover { text-decoration: underline; }
 .matrix td.mpool { background: #f8fafc; text-align: left; min-width: 150px; }
 .dot { display: inline-block; width: 12px; height: 12px; border-radius: 50%; vertical-align: -1px;
        margin-right: 5px; box-shadow: inset 0 -2px 3px rgba(0,0,0,.25); }
 .mlabel { font-weight: 600; }
 .msub { display: block; color: #555; font-size: 11px; margin-top: 2px; }
 .mnew { display: inline-block; background: #7c3aed; color: #fff; border-radius: 4px; font-size: 10px;
         font-weight: 700; padding: 0 4px; margin-left: 4px; vertical-align: 1px; }
 .mfilter { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0; }
 .mfilter button { border: 1px solid #d4d4d8; background: #fff; border-radius: 999px; padding: 3px 10px;
                   font-size: 13px; cursor: pointer; }
 .mfilter button.on { border-color: #111; font-weight: 700; }
 .matrix tr.hide { display: none; }
 .lvl-s { display: inline-block; color: #fff; border-radius: 4px; padding: 0 5px; font-size: 11px;
          font-weight: 700; margin: 1px 2px 1px 0; }
 .bars { display: grid; gap: 12px; margin: 12px 0; }
 .bar-card { border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 14px; background: #fafafa; }
 .bar-head { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 6px; }
 .bar-head h3 { margin: 0; font-size: 16px; }
 .bar-track { position: relative; height: 22px; margin: 26px 0 30px; border-radius: 11px;
              background: linear-gradient(90deg, #fee2e2, #f4f4f5 50%, #dcfce7); }
 .bar-typ { position: absolute; top: 4px; bottom: 4px; background: rgba(2,132,199,.25); border-radius: 7px; }
 .bar-mid { position: absolute; top: -4px; bottom: -4px; width: 2px; background: #a16207; }
 .bar-tick { position: absolute; top: -6px; bottom: -6px; width: 0; border-left: 2px dashed; }
 .bar-dot { position: absolute; top: 50%; width: 18px; height: 18px; margin: -9px 0 0 -9px; border-radius: 50%;
            border: 3px solid #fff; box-shadow: 0 0 0 1px #111, 0 2px 4px rgba(0,0,0,.3); }
 .bar-lab { position: absolute; font-size: 11px; white-space: nowrap; }
 .bar-lab.top { top: -20px; } .bar-lab.bot { top: 26px; }
 .bar-ends { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; text-align: center; }
 .bar-ends div { border: 1px solid #e5e7eb; border-radius: 8px; padding: 6px; background: #fff; }
 .bar-ends b { display: block; font-size: 15px; }
 .bar-ends span { font-size: 11px; color: #555; text-transform: uppercase; letter-spacing: .04em; }
 .pool-card { border: 1px solid #e5e7eb; border-left: 6px solid #7c3aed; border-radius: 10px; padding: 10px 14px;
              margin: 12px 0; background: #faf5ff; }
 .pool-card h2 { font-size: 18px; margin: 0 0 6px; }
 .pool-row { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: baseline; padding: 6px 0;
             border-top: 1px solid #ede9fe; }
 .pool-row:first-of-type { border-top: none; }
 .pool-row .pname { min-width: 120px; font-weight: 700; }
 .pool-card code { background: #fff; border: 1px solid #ddd6fe; border-radius: 4px; padding: 1px 5px; }
 @media (max-width: 640px) {
   .matrix td { min-width: 0; padding: 6px; } .mlabel { font-size: 12px; } .msub { display: none; }
   .bar-ends b { font-size: 13px; }
 }
"""

FILTER_JS = """<script>
(function () {
  var bar = document.querySelector('.mfilter'); if (!bar) return;
  bar.addEventListener('click', function (e) {
    var b = e.target.closest('button'); if (!b) return;
    var on = !b.classList.contains('on');
    bar.querySelectorAll('button').forEach(function (x) { x.classList.remove('on'); });
    if (on) b.classList.add('on');
    var f = on ? b.dataset.f : '';
    document.querySelectorAll('.matrix tbody tr').forEach(function (tr) {
      tr.classList.toggle('hide', !!f && (' ' + tr.dataset.f + ' ').indexOf(' ' + f + ' ') < 0);
    });
  });
})();
</script>"""


def stage_label(r: TimeframeResult) -> str:
    """'E2 Alcista' o 'E2→3 Alcista'."""
    tag = f"E{r.transition}" if r.transition else f"E{r.stage}"
    return f"{tag} {STAGE_SHORT[r.stage]}"


def _matrix_cell(r: TimeframeResult, changed: bool) -> str:
    if r.status != "ok":
        return '<td style="background:#f4f4f5" class="muted">sin datos</td>'
    prov = ""
    p = r.provisional
    if p is not None and p.status == "ok" and (p.stage, p.transition) != (r.stage, r.transition):
        prov = f'<span class="msub">si cerrara hoy: {html.escape(stage_label(p))}</span>'
    if r.power4 is not None:
        same = " (igual)" if r.power4 == r.stage else ""
        prov += f'<span class="msub">Power4: E{r.power4} {power4.NAMES[r.power4]}{same}</span>'
    new = '<span class="mnew" title="Cambió desde la ejecución anterior">nuevo</span>' if changed else ""
    title = (f"{r.label}: etapa {r.stage} ({STAGE_NAMES[r.stage]}), confianza {r.confidence_label}"
             + (f", en transición {r.transition}" if r.transition else ""))
    return (f'<td style="background:{STAGE_TINTS[r.stage]}" title="{html.escape(title)}">'
            f'<span class="dot" style="background:{STAGE_COLORS[r.stage]}"></span>'
            f'<span class="mlabel">{html.escape(stage_label(r))}</span>{new}'
            f'<span class="msub">confianza {html.escape(r.confidence_label)}</span>{prov}</td>')


def cycle_decisions(rep) -> dict:
    """{marco: (Opportunity, estado, motivo)} con la decisión en dos pasos de oportunidad.decision
    para la mejor combinación de cada ciclo. Vacío sin análisis."""
    if rep is None:
        return {}
    from . import oportunidad as op       # oportunidad importa medio paquete: solo cuando hace falta
    return {k: (o, *op.decision(o, rep.capital)) for k, o in op.decisions(rep).items()}


def _gap_txt(o, rep) -> str:
    from . import oportunidad as op
    gap = op.fee_gap(o, rep.capital)
    return f"comisiones ×{gap:.1f}".replace(".", ",") if gap and gap > 1 else ""


def _pool_mini(liq: LiquidityResult | None, rep=None, kind: str = "") -> str:
    if liq is not None:
        profs = [(p.name, _PROFILE_KEY.get(p.name, ""), p.level, p.low is not None) for p in liq.profiles]
    elif rep is not None:              # acción con POOL_PROXY: las etapas del token que sigue su precio
        profs = [(n, k, rep.stages.get(k, ("sin datos", []))[0], rep.ranges.get(k) is not None)
                 for n, k in _PROFILE_KEY.items()]
    else:
        why = "acción o ETF: sin pools on-chain" if kind == "accion" else ""
        return f'<td class="mpool muted">—{f"<span class=msub>{why}</span>" if why else ""}</td>'
    dec = cycle_decisions(rep)
    names = {k: n.lower() for n, k in _PROFILE_KEY.items()}
    chips = []
    for name, key, level, _ in profs:
        o, state, why = dec.get(key, (None, "", ""))
        go = state in ("abrir", "abrir con precaución")
        tip = f"{name}: paso 1 {level}" + ("" if rep is None else f"; paso 2: {state or 'sin pools'}"
                                           + (f" ({why})" if why else ""))
        chips.append(f'<span class="lvl-s" style="background:{LEVEL_COLORS.get(level, "#6b7280")}" '
                     f'title="{html.escape(tip)}">{name[0]}{"✓" if go else ""}</span>')
    apt = [n.lower() for n, _, level, ok in profs if level == "apta" and ok]
    caution = [n.lower() for n, _, level, ok in profs if level == "precaución" and ok]
    lines = ["etapa apta: " + (", ".join(apt) if apt else "ninguna")
             + (f"; precaución: {', '.join(caution)}" if caution else "")]
    if liq is None and rep is not None:
        lines.insert(0, f"vía {rep.base}/{rep.quote} (mismo precio)")
    if rep is not None:
        go = [f"{names[k]} ({o.net_apr:+.1%})" for k, (o, s, _) in dec.items() if s in ("abrir", "abrir con precaución")]
        if go:
            state = "ABRIR" if any(s == "abrir" for _, s, _ in dec.values()) else "ABRIR CON PRECAUCIÓN"
            lines.append(f"decisión: {state} · " + ", ".join(go))
        elif dec:
            k, (o, _, _) = max(dec.items(), key=lambda kv: kv[1][0].net_apr if kv[1][0].net_apr is not None else -9)
            gap = _gap_txt(o, rep)
            lines.append(f"decisión: vigilar · lo más cercano: {names[k]}" + (f", faltan {gap}" if gap else ""))
        else:
            lines.append("decisión: sin pools con liquidez suficiente")
    text = "".join(f'<span class="msub">{html.escape(t)}</span>' for t in lines)
    return f'<td class="mpool">{"".join(chips)}{text}</td>'


def matrix_html(assets: list[AssetResult], changes: list[dict]) -> str:
    """Cuadrícula valor × marco con el color de la etapa, como portada del índice."""
    label_key = {TIMEFRAMES[k].label: k for k in ORDER}
    changed = {(c["simbolo"], label_key.get(c["marco"])) for c in changes}
    rows = []
    for a in assets:
        if a.error:
            rows.append(f'<tr data-f=""><td class="msym">{html.escape(a.title)}</td>'
                        f'<td colspan="{len(ORDER) + 1}" class="err">{html.escape(a.error)}</td></tr>')
            continue
        tfs = a.timeframes
        tags = " ".join(f"e{tfs[k].stage}" for k in ORDER if tfs[k].status == "ok")
        if any(tfs[k].transition for k in ORDER if tfs[k].status == "ok"):
            tags += " trans"
        if any(s == a.key for s, _ in changed):
            tags += " nuevo"
        kind = {"par": " par", "accion": " acción"}.get(a.kind, "")
        cells = "".join(_matrix_cell(tfs[k], (a.key, k) in changed) for k in ORDER)
        rows.append(f'<tr data-f="{tags}"><td class="msym"><a href="etapas_{a.file_stem}.html">'
                    f'{html.escape(a.symbol)}</a><span class="msub">{fmt(a.last_price)}{kind}</span></td>'
                    f'{cells}{_pool_mini(a.liquidity, a.pool_report, a.kind)}</tr>')
    heads = "".join(f"<th>{TIMEFRAMES[k].label}</th>" for k in ORDER)
    filters = "".join(
        f'<button data-f="e{s}"><span class="dot" style="background:{STAGE_COLORS[s]}"></span>'
        f'Etapa {s}</button>' for s in STAGE_COLORS)
    filters += '<button data-f="trans">En transición</button><button data-f="nuevo">Con cambios</button>'
    return (f'<h2>Etapas por marco</h2><div class="mfilter">{filters}</div>'
            f'<div class="wrap"><table class="matrix"><thead><tr><th>Valor</th>{heads}'
            f'<th>Pool (D · S · M)</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            '<p class="muted">Etapa con velas cerradas; «si cerrara hoy» solo aparece cuando la vela en curso '
            'la cambiaría. «Power4»: la etapa con las reglas de MetaPro (SMA20/40), para comparar. «nuevo»: cambió desde la ejecución anterior. Pool, en dos pasos: el color es el '
            'paso 1, si la etapa permite un rango diario (D), semanal (S) y mensual (M) (verde apta, naranja '
            'precaución); ✓ y «decisión» son el paso 2, si /pool lo propondría hoy (neto positivo con margen '
            'de seguridad y por encima de depositar los mismos tokens en Aave). Abre un pool solo con ✓; '
            '«vigilar» dice cuántas veces las comisiones de hoy harían falta. Pulsa un botón para filtrar.</p>'
            + FILTER_JS)


# ---------------------------------------------------------------- barras de rango

def _pos(x: float, lo: float, hi: float) -> float:
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100))


def _lab_style(x: float) -> str:
    """Posición de una etiqueta sobre la barra, sin salirse por los bordes."""
    shift = "0" if x < 15 else "-100%" if x > 85 else "-50%"
    return f"left:{x:.1f}%;transform:translateX({shift})"


def range_bar(r: TimeframeResult, price: float | None) -> str:
    """Barra soporte–resistencia de un marco: rango de las últimas velas, rango típico de la vela
    en curso (sombreado), niveles que confirman e invalidan y el precio actual."""
    if r.status != "ok":
        return (f'<div class="bar-card"><div class="bar-head"><h3>{html.escape(r.label)}</h3></div>'
                f'<p class="muted">{html.escape(r.message or "sin datos")}</p></div>')
    sup, res = r.range_bottom, r.range_top
    if sup is None or res is None or res <= sup:
        sup, res = r.est_low, r.est_high
    if sup is None or res is None or price is None:
        return ""
    mid = (sup + res) / 2
    pts = [sup, res, price] + [x for x in (r.typ_low, r.typ_high) if x is not None]
    lo, hi = min(pts), max(pts)
    pad = (hi - lo) * 0.04 or hi * 0.01
    lo, hi = lo - pad, hi + pad
    parts = []
    if r.typ_low is not None and r.typ_high is not None:
        a, b = _pos(r.typ_low, lo, hi), _pos(r.typ_high, lo, hi)
        parts.append(f'<div class="bar-typ" style="left:{a:.1f}%;width:{b - a:.1f}%" '
                     f'title="Rango típico {html.escape(r.est_period)}: {fmt(r.typ_low)} – {fmt(r.typ_high)}"></div>')
    parts.append(f'<div class="bar-mid" style="left:{_pos(mid, lo, hi):.1f}%"></div>')
    for lvl, color, name in ((r.confirm_level, "#16a34a", "confirma"), (r.invalid_level, "#dc2626", "invalida")):
        if lvl is not None and lo < lvl < hi:
            x = _pos(lvl, lo, hi)
            parts.append(f'<div class="bar-tick" style="left:{x:.1f}%;border-color:{color}" '
                         f'title="Nivel que {name}: {fmt(lvl)}"></div>'
                         f'<div class="bar-lab bot" style="{_lab_style(x)};color:{color}">{name} {fmt(lvl)}</div>')
    x = _pos(price, lo, hi)
    parts.append(f'<div class="bar-dot" style="left:{x:.1f}%;background:{STAGE_COLORS[r.stage]}" '
                 f'title="Precio actual {fmt(price)}"></div>'
                 f'<div class="bar-lab top" style="{_lab_style(x)}"><b>{fmt(price)}</b></div>')
    where = (price - sup) / (res - sup)
    place = ("por encima de la resistencia" if where > 1 else "por debajo del soporte" if where < 0
             else f"al {where:.0%} del rango")
    return (f'<div class="bar-card"><div class="bar-head"><h3>{html.escape(r.label)} · '
            f'<span style="color:{STAGE_COLORS[r.stage]}">{html.escape(stage_label(r))}</span></h3>'
            f'<span class="muted">precio {place} · cierre de referencia {html.escape(r.candle_time)}</span></div>'
            f'<div class="bar-track">{"".join(parts)}</div>'
            f'<div class="bar-ends"><div><span>Soporte</span><b style="color:#dc2626">{fmt(sup)}</b></div>'
            f'<div><span>Punto medio</span><b style="color:#a16207">{fmt(mid)}</b></div>'
            f'<div><span>Resistencia</span><b style="color:#16a34a">{fmt(res)}</b></div></div></div>')


def range_bars_html(asset: AssetResult) -> str:
    bars = "".join(range_bar(asset.timeframes[k], asset.last_price) for k in ORDER)
    if not bars:
        return ""
    return ('<h2>Rangos por marco</h2>'
            f'<div class="bars">{bars}</div>'
            '<p class="note">Soporte y resistencia: mínimo y máximo de las últimas velas cerradas del marco '
            '(las que usa la etapa). Sombreado azul: rango típico de la vela en curso. Líneas discontinuas: '
            'nivel que confirma (verde) e invalida (rojo) la etapa. El punto es el precio actual, del color '
            'de la etapa.</p>')


# ---------------------------------------------------------------- veredicto de pool

def pool_command(asset: AssetResult) -> str:
    rep = asset.pool_report
    pair = f"{rep.base}/{rep.quote}" if rep is not None and rep.quote else f"{asset.base}/{asset.quote}"
    return f"python -m etapas.oportunidad {pair}"


MAX_PRODUCTIVE = 3        # pools productivos que se enlazan por ciclo


def _net_txt(o) -> str:
    return (f"neto {o.net_cycle:+,.2f} $ por ciclo ({o.net_apr:+.1%} anual; con margen "
            f"{o.net_safe_apr:+.1%})" if o.net_cycle is not None else "sin neto")


def _stop_txt(o) -> str:
    """Stop-loss del método (oportunidad.stop_levels): por abajo y, entre dos volátiles, por arriba."""
    if not o.stop_low:
        return ""
    return (f" · stop-loss <b>{fmt(o.stop_low)}</b>"
            + (f" (o si sube de <b>{fmt(o.stop_high)}</b>)" if o.stop_high else ""))


def pool_verdict_html(asset: AssetResult, github_link: str = "") -> str:
    """Tarjeta «¿Pool ahora?». Con el análisis de /pool (asset.pool_report): por ciclo, la etapa como
    información y solo los pools productivos (los que /pool propondría: neto positivo con el margen
    de seguridad y por encima de Aave), con su enlace; si no hay, el mejor neto sin enlace y cuánto
    faltaría. Sin ese análisis, la etapa y el rango sin enlazar ningún pool."""
    rep = asset.pool_report
    if rep is not None:
        return _verdict_from_report(asset, rep, github_link)
    liq = asset.liquidity
    if liq is None:
        return ""
    dec = cycle_decisions(asset.pool_report)
    rows = []
    for p in liq.profiles:
        color = LEVEL_COLORS.get(p.level, "#6b7280")
        chip = f'<span class="lvl" style="background:{color}">paso 1: etapa {html.escape(p.level)}</span>'
        if asset.pool_report is not None:
            o, state, why = dec.get(_PROFILE_KEY.get(p.name, ""), (None, "", "no hay pools con liquidez suficiente"))
            if state in ("abrir", "abrir con precaución"):
                chip += (f'<span class="lvl" style="background:#16a34a">✓ paso 2: {state} · {o.net_apr:+.1%} anual · '
                         f'{html.escape(o.pool.label)}</span>')
            else:
                net = f" ({o.net_apr:+.1%} anual)" if o is not None and o.net_apr is not None else ""
                chip += f'<span class="muted">paso 2: {html.escape(state or "vigilar")} · {html.escape(why)}{net}</span>'
        if p.low is None:
            rows.append(f'<div class="pool-row"><span class="pname">{html.escape(p.name)}</span>{chip}'
                        f'<span class="muted">sin rango</span></div>')
            continue
        w = p.width
        rng = f"rango <b>{fmt(p.low)} – {fmt(p.high)}</b> ({w[0]:+.1%} / {w[1]:+.1%})"
        r = asset.timeframes.get(_PROFILE_KEY.get(p.name, ""))
        stage = (f'<span class="muted">{html.escape(stage_label(r))}</span>' if r is not None and r.status == "ok"
                 else "")
        rows.append(f'<div class="pool-row"><span class="pname">{html.escape(p.name)}'
                    f' <span class="muted">{html.escape(p.horizon)}</span></span>{chip}{stage}'
                    f'<span>{rng}</span><span class="muted">rentabilidad sin calcular: no se enlaza ningún pool</span></div>')
    best = liq.best.replace(liq.note, "").strip() if liq.note else liq.best
    return (f'<div class="pool-card"><h2>¿Pool ahora? {html.escape(asset.base)}/{html.escape(asset.quote)}</h2>'
            f'<p>{html.escape(best)}</p>{"".join(rows)}<p class="note">{_more(asset, github_link)}</p></div>')


def _more(asset: AssetResult, github_link: str) -> str:
    more = f'Análisis completo del pool (comisiones reales, gas, stop-loss y neto): <code>{html.escape(pool_command(asset))}</code>'
    if github_link:
        more += f' · <a href="{html.escape(github_link)}">o desde GitHub</a>'
    return more


def _verdict_from_report(asset: AssetResult, rep, github_link: str) -> str:
    from . import oportunidad as op
    liq = asset.liquidity
    horizons = {p.timeframe: p.horizon for p in liq.profiles} if liq is not None else {}
    rows, n_ok = [], 0
    for key, name, _ in op.CYCLES:
        level = rep.stages.get(key, ("sin datos", []))[0]
        r = rep.asset.timeframes.get(key) if rep.asset is not None else None
        chip = (f'<span class="lvl" style="background:{LEVEL_COLORS.get(level, "#6b7280")}">etapa {html.escape(level)}</span>'
                + (f'<span class="muted">{html.escape(stage_label(r))}</span>' if r is not None and r.status == "ok" else ""))
        rng = rep.ranges.get(key)
        rtxt = f"<span>rango <b>{fmt(rng[0])} – {fmt(rng[1])}</b></span>" if rng else ""
        mine = sorted((o for o in rep.opportunities if o.cycle == key), key=lambda o: -(o.net_apr if o.net_apr is not None else -9))
        good = [o for o in mine if not o.excluded][:MAX_PRODUCTIVE]
        n_ok += len(good)
        if good:
            pools = "".join(
                f'<div><span class="lvl" style="background:#16a34a">✓ {html.escape(op.decision(o, rep.capital)[0])}</span> '
                f'<a href="{html.escape(o.pool.app_url or o.pool.url)}" target="_blank" rel="noopener">{html.escape(o.pool.label)}</a>'
                f' · rango {fmt(o.quote.low)} – {fmt(o.quote.high)}{_stop_txt(o)} · {_net_txt(o)}</div>' for o in good)
        elif mine and mine[0].net_cycle is not None:
            o = mine[0]
            gap = op.fee_gap(o, rep.capital)
            pools = (f'<div class="muted">Ningún pool productivo hoy. El mejor, {html.escape(o.pool.label)}: '
                     f'{_net_txt(o)}' + (f"; para abrir harían falta unas comisiones {gap:.1f} veces las de hoy"
                                         .replace(".", ",", 1) if gap and gap > 1 else "")
                     + f' ({html.escape(o.excluded)}).</div>')
        else:
            pools = f'<div class="muted">Ningún pool productivo hoy: {html.escape(mine[0].excluded if mine else "sin pools con liquidez suficiente")}.</div>'
        rows.append(f'<div class="pool-row"><span class="pname">{html.escape(name)}'
                    f' <span class="muted">{html.escape(horizons.get(key, ""))}</span></span>{chip}{rtxt}'
                    f'<div style="flex-basis:100%">{pools}</div></div>')
    pair = f"{rep.base}/{rep.quote}"
    proxy = (f' <span class="muted">(pools de {html.escape(pair)}, que sigue el mismo precio)</span>'
             if pair not in (asset.symbol, f"{asset.base}/{asset.quote}") else "")
    head = (f"{n_ok} pool{'es' if n_ok != 1 else ''} productivo{'s' if n_ok != 1 else ''} con {rep.capital:,.0f} $: "
            "neto positivo incluso con la pérdida un 25 % peor y por encima de Aave. La etapa es informativa: "
            "decides tú." if n_ok else
            f"Ningún pool productivo hoy con {rep.capital:,.0f} $ (neto positivo con margen de seguridad y por "
            "encima de Aave): no se enlaza ninguno.")
    return (f'<div class="pool-card"><h2>¿Pool ahora? {html.escape(pair)}{proxy}</h2>'
            f'<p>{html.escape(head)}</p>{"".join(rows)}<p class="note">{_more(asset, github_link)}</p></div>')
