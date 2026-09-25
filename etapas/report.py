"""Salida: tabla de consola, resumen de alineación, JSON y HTML (Plotly)."""
import html
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .analysis import AssetResult, TimeframeResult
from .classifier import STAGE_NAMES
from .config import DISCLAIMER, ORDER
from .explain import KEY_ZONE_NOTE, KEY_ZONE_TITLE, key_zone, meaning

STAGE_COLORS = {1: "#2563eb", 2: "#16a34a", 3: "#eab308", 4: "#dc2626"}
HEADERS = ["Marco", "Etapa", "Confianza", "Transición", "Precio", "Media clave", "Pendiente",
           "Estructura", "Nivel que confirma", "Nivel que invalida", "Mín. estimado", "Máx. estimado"]
RANGE_NOTE = ("Mín./máx. estimado: rango en el que se espera que fluctúe el precio durante la vela en "
              "curso (hoy, esta semana, este mes), medido desde el último cierre con la volatilidad "
              "actual. En el histórico, ~8 de cada 10 velas quedaron dentro. No indica dirección.")


def fmt_price(x) -> str:
    if x is None or pd.isna(x):
        return "—"
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    if abs(x) >= 1:
        return f"{x:,.2f}"
    return f"{x:.4g}"


def fmt_pct(x, sign: bool = True) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{x:+.1%}" if sign else f"{x:.0%}"


def table_row(r: TimeframeResult) -> list[str]:
    if r.status != "ok":
        return [r.label, r.message if r.status == "insuficiente" else "error"] + [""] * (len(HEADERS) - 2)
    return [
        r.label,
        f"{r.stage} · {r.stage_name}",
        f"{r.confidence_label} ({r.confidence:.0f})",
        r.transition or "—",
        fmt_price(r.price),
        f"SMA{r.ma_len} {fmt_price(r.ma)}",
        f"{fmt_pct(r.slope)} {r.slope_label}",
        r.structure,
        fmt_price(r.confirm_level),
        fmt_price(r.invalid_level),
        fmt_price(r.est_low),
        fmt_price(r.est_high),
    ]


def render_table(rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(HEADERS)]
    line = lambda cells: "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"
    sep = "|-" + "-|-".join("-" * w for w in widths) + "-|"
    return "\n".join([line(HEADERS), sep, *(line(r) for r in rows)])


def detail_line(r: TimeframeResult) -> str:
    """Métricas que justifican la etapa."""
    if r.status != "ok":
        return f"  {r.label}: {r.message}"
    prior = (f"{fmt_pct(r.prior_change)} en la media, vigente hasta {r.prior_since}"
             if r.prior_since else "sin tendencia previa significativa")
    vol = f"{r.volume_ratio:.1f}×" if r.volume_ratio is not None else "—"
    brk = f", ruptura {r.breakout} del rango" if r.breakout else ""
    scores = " ".join(f"E{s}:{v:.0f}" for s, v in r.scores.items())
    return (f"  {r.label} ({r.candle_time}): pendiente {fmt_pct(r.slope)} vs umbral "
            f"±{r.slope_threshold:.1%} → {r.slope_label} · {fmt_pct(r.pct_above, False)} de cierres "
            f"sobre la media, {r.crosses} cruces · estructura {r.structure} · tendencia previa {prior} · "
            f"rango {fmt_price(r.range_bottom)}–{fmt_price(r.range_top)} · volumen {vol} su media{brk} · "
            f"puntuaciones {scores}{_range_text(r)}")


def _range_text(r: TimeframeResult) -> str:
    if r.est_low is None or r.est_high is None or not r.price:
        return ""
    return (f" · rango estimado {r.est_period}: {fmt_price(r.est_low)}–{fmt_price(r.est_high)} "
            f"({(r.est_low / r.price - 1):+.1%} / {(r.est_high / r.price - 1):+.1%})")


def _stage_text(r: TimeframeResult) -> str:
    return r.transition if r.transition else str(r.stage)


_CONTEXT = {
    1: "el contexto mensual es de base, todavía sin tendencia de fondo",
    2: "el contexto mensual es alcista, lo que respalda las subidas",
    3: "el contexto mensual es de distribución, con riesgo de techo de fondo",
    4: "el contexto mensual es bajista, lo que resta fiabilidad a las subidas",
}

_ALIGNED = {
    1: "El activo construye base en todos los plazos: aún no hay tendencia; la señal sería la "
       "ruptura del techo del rango.",
    2: "Tendencia alcista en todos los plazos: contexto, tendencia principal y momento coinciden.",
    3: "Posible techo en todos los plazos: la subida pierde impulso; vigilar la pérdida del soporte.",
    4: "Tendencia bajista en todos los plazos: contexto desfavorable para compras.",
}


def _main_vs_timing(w: int, d: int) -> str:
    if w == 2:
        return {2: "La tendencia principal (semanal) y el momento (diario) son alcistas.",
                1: "La tendencia principal es alcista y el diario consolida: la entrada llegaría "
                   "con la ruptura del rango diario.",
                3: "La tendencia principal es alcista, pero el diario pierde impulso: el momento "
                   "de entrada no acompaña.",
                4: "La tendencia principal es alcista, pero el diario está en corrección."}[d]
    if w == 4:
        return {2: "Rebote de corto plazo (diario) dentro de una tendencia principal bajista.",
                1: "La tendencia principal es bajista y el diario lateraliza: posible freno de la caída.",
                3: "La tendencia principal es bajista y el diario forma un techo: el rebote se agota.",
                4: "La tendencia principal y el momento son bajistas."}[d]
    if w == 1:
        return ("El semanal forma base y el diario ya sube: posible inicio de tendencia si el "
                "semanal rompe su rango." if d == 2 else
                "El semanal forma base: aún no hay tendencia principal definida.")
    return ("El semanal muestra distribución y el diario ya cae: riesgo de paso a etapa 4."
            if d == 4 else "El semanal muestra distribución: la subida pierde fuerza.")


def alignment_state(asset: AssetResult) -> tuple[str, str]:
    """(estado de alineación, frase que lo interpreta). Estado vacío si faltan marcos."""
    ok = {k: r for k, r in asset.timeframes.items() if r.status == "ok"}
    if len(ok) < 2:
        return "", "No hay marcos suficientes para evaluar la alineación."
    stages = [ok[k].stage for k in ORDER if k in ok]
    in_transition = any(ok[k].transition for k in ok)
    if len(set(stages)) == 1 and not in_transition:
        state = f"ALINEADOS en etapa {stages[0]}"
    elif len(set(stages)) == 1:
        state = f"ALINEADOS en etapa {stages[0]}, con transición en curso"
    elif len(set(stages)) == 2 and len(stages) == 3:
        state = "PARCIALMENTE ALINEADOS"
    else:
        state = "EN CONFLICTO"
    sentence = ""
    if len(set(stages)) == 1:
        sentence = _ALIGNED[stages[0]]
    elif "1w" in ok and "1d" in ok:
        sentence = _main_vs_timing(ok["1w"].stage, ok["1d"].stage)
        if "1M" in ok and ok["1M"].stage != ok["1w"].stage:
            sentence += f" Además, {_CONTEXT[ok['1M'].stage]}."
    return state, sentence


def alignment_summary(asset: AssetResult) -> str:
    tfs = asset.timeframes
    ok = [k for k in ORDER if tfs[k].status == "ok"]
    lines = ["Alineación (mensual = contexto, semanal = tendencia principal, diario = momento de entrada):"]
    state, sentence = alignment_state(asset)
    if not state:
        lines.append(f"  {sentence}")
        return "\n".join(lines)
    parts = [f"{tfs[k].label.lower()} {_stage_text(tfs[k])}" for k in ok]
    lines.append(f"  {state}: " + ", ".join(parts) + ".")
    if sentence:
        lines.append("  " + sentence)
    missing = [tfs[k].label for k in ORDER if k not in ok]
    if missing:
        lines.append(f"  (Sin datos válidos en: {', '.join(missing)})")
    return "\n".join(lines)


def token_text(asset: AssetResult) -> str:
    """Línea con el token de Ondo de una acción (vacía para criptomonedas)."""
    if asset.kind != "accion" or asset.error:
        return ""
    t = asset.token
    if not t:
        return "Token Ondo: no cotiza en MEXC ni BingX (o no se pudo consultar)."
    diff = ""
    if t.get("diferencia") is not None:
        diff = f" · {t['diferencia']:+.2%} frente a la acción ({fmt_price(asset.last_price)})"
    return f"Token Ondo {t['token']}: {fmt_price(t['precio'])} en {t['exchange'].upper()}{diff}"


def render_text(asset: AssetResult) -> str:
    out = [f"\n=== {asset.title} ==="]
    if token_text(asset):
        out.append(token_text(asset))
    if asset.error:
        out.append(f"ERROR: {asset.error}")
        return "\n".join(out)
    rows = []
    for k in ORDER:
        r = asset.timeframes[k]
        rows.append(table_row(r))
        if r.provisional:
            rows.append(table_row(r.provisional))
    out.append(render_table(rows))
    out.append(RANGE_NOTE)
    out.append("")
    out.append("Qué significa:")
    out.extend(f"  {asset.timeframes[k].label}: {meaning(asset.timeframes[k], asset.kind)}" for k in ORDER)
    zone = key_zone(asset)
    if zone:
        out.append("")
        out.append(f"⚠ {KEY_ZONE_TITLE.upper()}")
        out.extend(f"  · {z}" for z in zone)
        out.append(f"  ({KEY_ZONE_NOTE})")
    out.append("")
    out.append("Métricas:")
    out.extend(detail_line(asset.timeframes[k]) for k in ORDER)
    notes = [f"  [{asset.timeframes[k].label}] {n}" for k in ORDER for n in asset.timeframes[k].notes]
    errors = [f"  [{asset.timeframes[k].label}] {asset.timeframes[k].message}"
              for k in ORDER if asset.timeframes[k].status == "error"]
    if notes or errors:
        out.append("Avisos:")
        out.extend(notes + errors)
    sources = sorted({r.source for r in asset.timeframes.values() if r.source})
    if sources:
        out.append(f"Fuente: {', '.join(sources)} · solo velas cerradas")
    out.append("")
    out.append(alignment_summary(asset))
    return "\n".join(out)


def _asset_json(a: AssetResult) -> dict:
    d = a.to_dict() | {"alineacion": alignment_summary(a), "zona_clave": key_zone(a)}
    for k, tf in d["marcos"].items():
        tf["que_significa"] = meaning(a.timeframes[k], a.kind)
    return d


def render_json(assets: list[AssetResult]) -> str:
    payload = {
        "activos": [_asset_json(a) for a in assets],
        "aviso": DISCLAIMER,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- HTML

def _stage_runs(hist: pd.DataFrame):
    """Tramos consecutivos con la misma etapa: (inicio, fin, etapa)."""
    st = hist["stage"]
    runs, start = [], 0
    for i in range(1, len(st) + 1):
        if i == len(st) or st.iloc[i] != st.iloc[start]:
            if st.iloc[start]:
                end = hist.index[i] if i < len(st) else hist.index[-1]
                runs.append((hist.index[start], end, int(st.iloc[start])))
            start = i
    return runs


def _log_y(price: float) -> float:
    """Posición de una etiqueta sobre un eje logarítmico. Plotly espera log10(precio) en las
    anotaciones de ejes 'log' (las líneas horizontales, en cambio, van en precio)."""
    return float(np.log10(price))


def build_figure(asset: AssetResult):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    keys = [k for k in ORDER if asset.timeframes[k].history is not None]
    titles = []
    for k in keys:
        r = asset.timeframes[k]
        trans = f" · en transición {r.transition}" if r.transition else ""
        titles.append(f"{r.label}: etapa {r.stage} ({r.stage_name}) · confianza "
                      f"{r.confidence_label}{trans}")
    fig = make_subplots(rows=len(keys), cols=1, subplot_titles=titles, vertical_spacing=0.07)
    for i, k in enumerate(keys, start=1):
        r = asset.timeframes[k]
        c, h = r.candles, r.history
        fig.add_trace(go.Candlestick(x=c.index, open=c["open"], high=c["high"], low=c["low"],
                                     close=c["close"], name=f"{r.label}", showlegend=False),
                      row=i, col=1)
        fig.add_trace(go.Scatter(x=h.index, y=h["ma"], name=f"SMA{r.ma_len}",
                                 line=dict(color="#7c3aed", width=1.6), showlegend=False),
                      row=i, col=1)
        for x0, x1, s in _stage_runs(h):
            fig.add_vrect(x0=x0, x1=x1, fillcolor=STAGE_COLORS[s], opacity=0.15,
                          line_width=0, layer="below", row=i, col=1)
        levels = [(r.confirm_level, f"Confirma {fmt_price(r.confirm_level)}", "#16a34a", "dash", 2.2),
                  (r.invalid_level, f"Invalida {fmt_price(r.invalid_level)}", "#dc2626", "dash", 2.2),
                  (r.est_low, f"Mín. est. {r.est_period} {fmt_price(r.est_low)}", "#475569", "dot", 1.4),
                  (r.est_high, f"Máx. est. {r.est_period} {fmt_price(r.est_high)}", "#475569", "dot", 1.4)]
        axis = "" if i == 1 else str(i)
        for y, text, color, dash, width in levels:
            if y is None or y <= 0:
                continue
            fig.add_hline(y=y, line_dash=dash, line_color=color, line_width=width, row=i, col=1)
            fig.add_annotation(text=text, x=1, xref=f"x{axis} domain", xanchor="right",
                               y=_log_y(y), yref=f"y{axis}", yanchor="bottom", showarrow=False,
                               font=dict(size=11, color=color), bgcolor="rgba(255,255,255,0.8)")
        fig.update_xaxes(rangeslider_visible=False, row=i, col=1)
        fig.update_yaxes(type="log", row=i, col=1)
    # Leyenda de colores de etapa
    for s, color in STAGE_COLORS.items():
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name=f"Etapa {s} · {STAGE_NAMES[s]}",
                                 marker=dict(size=12, color=color, symbol="square")))
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="lines", name="Media clave",
                             line=dict(color="#7c3aed")))
    fig.update_layout(height=430 * len(keys), template="plotly_white",
                      legend=dict(orientation="h", y=1.02, x=0, yanchor="bottom"),
                      margin=dict(l=50, r=30, t=90, b=30))
    return fig


def write_html(asset: AssetResult, out_dir: Path, index_link: bool = False) -> Path | None:
    if asset.error or all(r.history is None for r in asset.timeframes.values()):
        return None
    fig = build_figure(asset)
    rows = [table_row(asset.timeframes[k]) for k in ORDER]
    meanings = [meaning(asset.timeframes[k], asset.kind) for k in ORDER]
    # "Qué significa" va justo después de "Etapa", para que se lea sin desplazar la tabla
    heads = HEADERS[:2] + ["Qué significa"] + HEADERS[2:]
    thead = "".join(f"<th>{html.escape(h)}</th>" for h in heads)
    cell = lambda c: f"<td>{html.escape(c)}</td>"
    tbody = "".join("<tr>" + "".join(cell(c) for c in r[:2]) + f'<td class="meaning">{html.escape(m)}</td>'
                    + "".join(cell(c) for c in r[2:]) + "</tr>" for r, m in zip(rows, meanings))
    zone = key_zone(asset)
    zone_html = ""
    if zone:
        items = "".join(f"<li>{html.escape(z)}</li>" for z in zone)
        zone_html = (f'<div class="keyzone"><b>⚠ {html.escape(KEY_ZONE_TITLE)}</b><ul>{items}</ul>'
                     f'<p>{html.escape(KEY_ZONE_NOTE)}</p></div>')
    details = "".join(f"<li>{html.escape(detail_line(asset.timeframes[k]).strip())}</li>" for k in ORDER)
    summary = html.escape(alignment_summary(asset)).replace("\n", "<br>")
    back = '<p><a href="index.html">← Todos los activos</a></p>' if index_link else ""
    token_html = f'<p class="note">{html.escape(token_text(asset))}</p>' if token_text(asset) else ""
    page = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Etapas {html.escape(asset.title)}</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 24px; color: #111; background: #fff; }}
 table {{ border-collapse: collapse; font-size: 14px; margin: 12px 0; }}
 th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; white-space: nowrap; }}
 td.meaning {{ white-space: normal; min-width: 320px; max-width: 460px; font-size: 13px; }}
 .keyzone {{ background: #fffbeb; border: 1px solid #f59e0b; border-left: 6px solid #f59e0b;
             border-radius: 6px; padding: 10px 14px; margin: 12px 0; }}
 .keyzone ul {{ margin: 6px 0; padding-left: 20px; }}
 .keyzone li {{ margin: 4px 0; font-size: 14px; }}
 .keyzone p {{ color: #78350f; font-size: 12px; margin: 4px 0 0; }}
 th {{ background: #f4f4f5; }}
 .wrap {{ overflow-x: auto; }}
 li {{ font-size: 13px; margin: 4px 0; }}
 .note {{ color: #555; font-size: 12px; margin: 0 0 12px; }}
 .summary {{ background: #f8fafc; border-left: 4px solid #7c3aed; padding: 10px 14px; }}
 .disclaimer {{ color: #666; font-size: 13px; margin-top: 24px; }}
</style></head><body>
{back}<h1>Etapas de Weinstein · {html.escape(asset.title)}</h1>
{token_html}
<div class="wrap"><table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table></div>
<p class="note">{html.escape(RANGE_NOTE)}</p>
{zone_html}
<p class="summary">{summary}</p>
<details><summary>Métricas que justifican cada etapa</summary><ul>{details}</ul></details>
{fig.to_html(full_html=False, include_plotlyjs="cdn")}
<p class="disclaimer">{DISCLAIMER}</p>
</body></html>"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"etapas_{asset.file_stem}.html"
    path.write_text(page, encoding="utf-8")
    return path


# ---------------------------------------------------------------- índice de la rutina

_ALIGN_COLORS = {"ALINEADOS": "#16a34a", "PARCIALMENTE": "#ca8a04", "EN CONFLICTO": "#dc2626"}


def _stage_cell(r: TimeframeResult) -> str:
    if r.status != "ok":
        return f'<td class="muted">{html.escape(r.message if r.status == "insuficiente" else "error")}</td>'
    trans = f' <span class="trans">{r.transition}</span>' if r.transition else ""
    return (f'<td><span class="chip" style="background:{STAGE_COLORS[r.stage]}">{r.stage}</span> '
            f'{html.escape(r.stage_name)}{trans}<br><span class="muted">confianza {r.confidence_label} · '
            f'confirma {fmt_price(r.confirm_level)} · invalida {fmt_price(r.invalid_level)}<br>'
            f'rango {html.escape(r.est_period)}: {fmt_price(r.est_low)} – {fmt_price(r.est_high)}</span></td>')


def write_index(assets: list[AssetResult], changes: list[dict], prev_date: str | None,
                warnings: list[str], out_dir: Path, now: pd.Timestamp) -> Path:
    """Página resumen de todos los activos de la lista, con los cambios desde la última ejecución."""
    rows = []
    for a in assets:
        name = html.escape(a.symbol) + (' <span class="kind">acción</span>' if a.kind == "accion" else "")
        link = f'<a href="etapas_{a.file_stem}.html">{name}</a>' if not a.error else name
        if a.error:
            rows.append(f'<tr><td class="sym">{link}</td><td colspan="4" class="err">'
                        f'{html.escape(a.error)}</td></tr>')
            continue
        state, sentence = alignment_state(a)
        color = next((c for k, c in _ALIGN_COLORS.items() if state.startswith(k)), "#666")
        cells = "".join(_stage_cell(a.timeframes[k]) for k in ORDER)
        token = ""
        if a.kind == "accion":
            t = a.token
            token = (f'<br><span class="muted">{html.escape(t["token"])}: {fmt_price(t["precio"])}'
                     f' ({t["diferencia"]:+.1%})</span>' if t and t.get("diferencia") is not None
                     else '<br><span class="muted">sin token Ondo</span>')
        rows.append(f'<tr><td class="sym">{link}<br><span class="muted">'
                    f'{fmt_price(a.timeframes["1d"].price)}</span>{token}</td>{cells}'
                    f'<td><b style="color:{color}">{html.escape(state)}</b><br>'
                    f'<span class="small">{html.escape(sentence)}</span></td></tr>')
    if changes:
        items = "".join(f"<li><b>{html.escape(c['simbolo'])}</b> {html.escape(c['marco'])}: "
                        f"{html.escape(c['texto'])}</li>" for c in changes)
        changes_html = f"<h2>Cambios desde {prev_date}</h2><ul>{items}</ul>"
    elif prev_date:
        changes_html = f"<h2>Cambios desde {prev_date}</h2><p class='muted'>Ninguno.</p>"
    else:
        changes_html = "<p class='muted'>Primera ejecución: los cambios aparecerán a partir de la próxima.</p>"
    zone_items = "".join(
        f"<li><b>{html.escape(a.title)}</b> · {html.escape(z)}</li>" for a in assets for z in key_zone(a))
    zone_html = (f'<div class="keyzone"><b>⚠ {html.escape(KEY_ZONE_TITLE)}</b><ul>{zone_items}</ul>'
                 f'<p>{html.escape(KEY_ZONE_NOTE)}</p></div>') if zone_items else ""
    warn_html = ("<h2>Avisos</h2><ul>" + "".join(f"<li>{html.escape(w)}</li>" for w in warnings)
                 + "</ul>") if warnings else ""
    legend = " ".join(f'<span class="chip" style="background:{c}">{s}</span> {STAGE_NAMES[s]}'
                      for s, c in STAGE_COLORS.items())
    page = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Etapas · resumen</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 24px; color: #111; background: #fff; }}
 table {{ border-collapse: collapse; font-size: 14px; margin: 12px 0; }}
 th, td {{ border: 1px solid #ddd; padding: 8px 10px; text-align: left; vertical-align: top; }}
 .keyzone {{ background: #fffbeb; border: 1px solid #f59e0b; border-left: 6px solid #f59e0b;
             border-radius: 6px; padding: 10px 14px; margin: 12px 0; }}
 .keyzone ul {{ margin: 6px 0; padding-left: 20px; }}
 .keyzone li {{ margin: 4px 0; font-size: 14px; }}
 .keyzone p {{ color: #78350f; font-size: 12px; margin: 4px 0 0; }}
 th {{ background: #f4f4f5; }}
 .wrap {{ overflow-x: auto; }}
 .chip {{ display: inline-block; min-width: 18px; text-align: center; color: #fff; border-radius: 4px;
          font-weight: 600; padding: 0 5px; }}
 .trans {{ background: #ede9fe; color: #5b21b6; border-radius: 4px; padding: 0 4px; font-size: 12px; }}
 .muted {{ color: #666; font-size: 12px; }}
 .small {{ font-size: 12px; }}
 .sym {{ font-weight: 700; font-size: 15px; }}
 .kind {{ font-size: 11px; font-weight: 500; background: #e0f2fe; color: #075985; border-radius: 4px;
          padding: 0 4px; }}
 .err {{ color: #b91c1c; }}
 .disclaimer {{ color: #666; font-size: 13px; margin-top: 24px; }}
</style></head><body>
<h1>Etapas de Weinstein · resumen</h1>
<p class="muted">Actualizado {now:%Y-%m-%d %H:%M} UTC · solo velas cerradas · {legend}</p>
{zone_html}
{changes_html}
<div class="wrap"><table>
<thead><tr><th>Activo</th><th>Mensual (contexto)</th><th>Semanal (tendencia)</th>
<th>Diario (entrada)</th><th>Alineación</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p class="muted">{html.escape(RANGE_NOTE)}</p>
{warn_html}
<p class="disclaimer">{DISCLAIMER}</p>
</body></html>"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    path.write_text(page, encoding="utf-8")
    return path
