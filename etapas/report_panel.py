"""Panel visual: matriz de etapas del índice, barras de rango por marco y veredicto de pool de cada
informe. Solo presentación: todos los datos salen del análisis (etapas, rangos y liquidez)."""
import html

from .analysis import AssetResult, TimeframeResult
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
    new = '<span class="mnew" title="Cambió desde la ejecución anterior">nuevo</span>' if changed else ""
    title = (f"{r.label}: etapa {r.stage} ({STAGE_NAMES[r.stage]}), confianza {r.confidence_label}"
             + (f", en transición {r.transition}" if r.transition else ""))
    return (f'<td style="background:{STAGE_TINTS[r.stage]}" title="{html.escape(title)}">'
            f'<span class="dot" style="background:{STAGE_COLORS[r.stage]}"></span>'
            f'<span class="mlabel">{html.escape(stage_label(r))}</span>{new}'
            f'<span class="msub">confianza {html.escape(r.confidence_label)}</span>{prov}</td>')


def _pool_mini(liq: LiquidityResult | None) -> str:
    if liq is None:
        return '<td class="mpool muted">—</td>'
    chips = "".join(
        f'<span class="lvl-s" style="background:{LEVEL_COLORS.get(p.level, "#6b7280")}" '
        f'title="{html.escape(p.name)}: {html.escape(p.level)}">{p.name[0]}</span>' for p in liq.profiles)
    fav = [p.name.lower() for p in liq.profiles if p.level == "favorable" and p.low is not None]
    text = ("favorable: " + ", ".join(fav)) if fav else "ninguno favorable"
    return f'<td class="mpool">{chips}<span class="msub">{html.escape(text)}</span></td>'


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
                    f'{cells}{_pool_mini(a.liquidity)}</tr>')
    heads = "".join(f"<th>{TIMEFRAMES[k].label}</th>" for k in ORDER)
    filters = "".join(
        f'<button data-f="e{s}"><span class="dot" style="background:{STAGE_COLORS[s]}"></span>'
        f'Etapa {s}</button>' for s in STAGE_COLORS)
    filters += '<button data-f="trans">En transición</button><button data-f="nuevo">Con cambios</button>'
    return (f'<h2>Etapas por marco</h2><div class="mfilter">{filters}</div>'
            f'<div class="wrap"><table class="matrix"><thead><tr><th>Valor</th>{heads}'
            f'<th>Pool (D · S · M)</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            '<p class="muted">Etapa con velas cerradas; «si cerrara hoy» solo aparece cuando la vela en curso '
            'la cambiaría. «nuevo»: cambió desde la ejecución anterior. Pool: idoneidad de un rango '
            'diario (D), semanal (S) y mensual (M). Pulsa un botón para filtrar.</p>' + FILTER_JS)


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
    return f"python -m etapas.oportunidad {asset.base}/{asset.quote}"


def pool_verdict_html(asset: AssetResult, github_link: str = "") -> str:
    """Tarjeta «¿Pool ahora?»: idoneidad, rango y mejor pool por perfil, y cómo pedir el análisis
    completo del par (comisiones reales, gas, stop-loss y neto)."""
    liq = asset.liquidity
    if liq is None:
        return ""
    rows = []
    for p in liq.profiles:
        color = LEVEL_COLORS.get(p.level, "#6b7280")
        chip = f'<span class="lvl" style="background:{color}">{html.escape(p.level)}</span>'
        if p.low is None:
            rows.append(f'<div class="pool-row"><span class="pname">{html.escape(p.name)}</span>{chip}'
                        f'<span class="muted">sin rango</span></div>')
            continue
        w = p.width
        rng = f"rango <b>{fmt(p.low)} – {fmt(p.high)}</b> ({w[0]:+.1%} / {w[1]:+.1%})"
        q = p.main_quote
        pool = (f'<a href="{html.escape(q.pool.url)}" target="_blank" rel="noopener">{html.escape(q.pool.label)}</a>'
                f' · ~{q.fee_apr:.0%} APR est.' if q and q.fee_apr is not None else "")
        r = asset.timeframes.get(_PROFILE_KEY.get(p.name, ""))
        stage = (f'<span class="muted">{html.escape(stage_label(r))}</span>' if r is not None and r.status == "ok"
                 else "")
        rows.append(f'<div class="pool-row"><span class="pname">{html.escape(p.name)}'
                    f' <span class="muted">{html.escape(p.horizon)}</span></span>{chip}{stage}'
                    f'<span>{rng}</span>{pool}</div>')
    best = liq.best.replace(liq.note, "").strip() if liq.note else liq.best
    more = f'Análisis completo del pool (comisiones reales, gas, stop-loss y neto): <code>{html.escape(pool_command(asset))}</code>'
    if github_link:
        more += f' · <a href="{html.escape(github_link)}">o desde GitHub</a>'
    return (f'<div class="pool-card"><h2>¿Pool ahora? {html.escape(asset.base)}/{html.escape(asset.quote)}</h2>'
            f'<p>{html.escape(best)}</p>{"".join(rows)}<p class="note">{more}</p></div>')
