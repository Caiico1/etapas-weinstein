"""Rutina: analiza la lista de seguimiento, genera informes e índice y detecta cambios de etapa."""
import json
import os
import re
from pathlib import Path

import pandas as pd

from . import agentes
from .analysis import AssetResult, analyze_symbol, parse_symbol
from .config import DISCLAIMER, ORDER, TIMEFRAMES
from .contexto import MarketContext, headlines_for, load_context
from .guia import write_guide
from .liquidez import analyze_liquidity
from .posiciones import check as check_position, load_positions
from .report import fmt_price, write_html, write_index

_CRYPTO = re.compile(r"^[A-Z0-9]{1,15}$")
_STOCK = re.compile(r"^[A-Z0-9][A-Z0-9.^=-]{0,19}$")


def load_watchlist(path: Path) -> tuple[list[str], list[str]]:
    """Devuelve (símbolos, avisos). Ignora comentarios (#), líneas vacías y duplicados.

    Criptomonedas: BTC. Acciones: accion:NVDA (ticker de Yahoo Finance, p. ej. SAN.MC).
    """
    symbols, warnings = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = line.split("#", 1)[0].strip()
        if not raw:
            continue
        kind, ticker = parse_symbol(raw)
        sym = f"accion:{ticker}" if kind == "accion" else ticker
        if not (_STOCK if kind == "accion" else _CRYPTO).match(ticker):
            warnings.append(f"{path.name} línea {n}: '{sym}' no parece un símbolo válido; se ignora")
        elif sym in symbols:
            warnings.append(f"{path.name} línea {n}: {sym} repetido; se ignora")
        else:
            symbols.append(sym)
    return symbols, warnings


def snapshot(assets: list[AssetResult]) -> dict:
    """Estado mínimo de cada activo para comparar entre ejecuciones."""
    out = {}
    for a in assets:
        out[a.key] = {
            k: {"etapa": r.stage, "transicion": r.transition, "vela": r.candle_time,
                "cierre": r.price, "confirma": r.confirm_level, "invalida": r.invalid_level}
            for k, r in a.timeframes.items() if r.status == "ok"
        }
    return out


def load_previous(hist_dir: Path, today: str) -> tuple[str, dict] | None:
    """Última instantánea anterior a hoy, si existe."""
    files = sorted(p for p in hist_dir.glob("*.json") if p.stem < today)
    if not files:
        return None
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    return files[-1].stem, data["activos"]


def load_previous_positions(hist_dir: Path, today: str) -> dict:
    """Estado de las posiciones en la última instantánea anterior a hoy que las incluya."""
    for f in sorted((p for p in hist_dir.glob("*.json") if p.stem < today), reverse=True):
        data = json.loads(f.read_text(encoding="utf-8"))
        if "posiciones" in data:
            return data["posiciones"]
    return {}


def diff(prev: dict, curr: dict) -> list[dict]:
    """Cambios de etapa y transiciones nuevas entre dos instantáneas."""
    changes = []
    for sym, tfs in curr.items():
        if sym not in prev:
            changes.append({"simbolo": sym, "marco": "", "tipo": "lista", "texto": "añadido a la lista"})
            continue
        for k in ORDER:
            new, old = tfs.get(k), prev[sym].get(k)
            if not new or not old:
                continue
            label = TIMEFRAMES[k].label
            if new["etapa"] != old["etapa"]:
                changes.append({"simbolo": sym, "marco": label,
                                "texto": f"etapa {old['etapa']} → {new['etapa']}"})
            elif new["transicion"] and new["transicion"] != old["transicion"]:
                changes.append({"simbolo": sym, "marco": label,
                                "texto": f"entra en transición {new['transicion']}"})
    for sym in prev:
        if sym not in curr:
            changes.append({"simbolo": sym, "marco": "", "tipo": "lista", "texto": "retirado de la lista"})
    return changes


def level_alerts(prev: dict, curr: dict) -> list[dict]:
    """Cierres que cruzan el nivel que confirma o que invalida de la ejecución anterior.

    Solo cuenta cuando hay una vela nueva cerrada en ese marco (así el semanal y el mensual no
    avisan cada día). La dirección depende de la etapa: en 1 y 2 confirmar es cerrar por encima
    e invalidar por debajo; en 3 y 4, al revés.
    """
    alerts = []
    for sym, tfs in curr.items():
        for k in ORDER:
            new, old = tfs.get(k), prev.get(sym, {}).get(k)
            if not new or not old or new["vela"] == old["vela"] or old.get("cierre") is None:
                continue
            close, up = new["cierre"], old["etapa"] in (1, 2)
            label = TIMEFRAMES[k].label
            for field, name in (("confirma", "que confirma"), ("invalida", "que invalida")):
                level = old.get(field)
                if level is None:
                    continue
                above = field == "confirma" if up else field == "invalida"
                if (close > level) if above else (close < level):
                    side = "por encima" if close > level else "por debajo"
                    alerts.append({"simbolo": sym, "marco": label, "tipo": "nivel",
                                   "texto": f"cierra {side} del nivel {name} de la etapa "
                                            f"{old['etapa']} ({fmt_price(level)} → cierre {fmt_price(close)})"})
    return alerts


def write_alerts(items: list[dict], prev_date: str | None, out_dir: Path, today: str,
                 headlines: dict | None = None) -> Path | None:
    """Escribe out/alertas.md y out/alertas_titulo.txt si hay novedades (los usa GitHub Actions
    para abrir una issue, que GitHub envía por correo). Si no hay novedades, no escribe nada."""
    for name in ("alertas.md", "alertas_titulo.txt"):
        (out_dir / name).unlink(missing_ok=True)
    if not items:
        return None
    web = os.environ.get("ETAPAS_WEB_URL", "")
    n = f"{len(items)} novedad" + ("es" if len(items) != 1 else "")
    lines = [f"**{n}** en la rutina del {today}"
             + (f" (comparado con {prev_date})" if prev_date else "") + ":", ""]
    lines += [f"- **{c['simbolo']}**{' ' + c['marco'] if c['marco'] else ''}: {c['texto']}" for c in items]
    news = [(sym, hs) for sym, hs in (headlines or {}).items()
            if hs and any(c["simbolo"] == sym for c in items)]
    if news:
        lines += ["", "**Titulares recientes** (contexto, no señales):"]
        for sym, hs in news:
            lines += [f"- **{sym}**: " + " · ".join(f"[{h.title}]({h.link}) ({h.source}, {h.when:%d/%m})"
                                                    for h in hs[:3])]
    if web:
        lines += ["", f"Informes completos: {web}"]
    lines += ["", f"_{DISCLAIMER}_"]
    (out_dir / "alertas.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "alertas_titulo.txt").write_text(
        f"Etapas {today}: {n}", encoding="utf-8")
    return out_dir / "alertas.md"


def add_liquidity(asset: AssetResult, log: list[str]) -> None:
    """Calcula la liquidez concentrada de un criptoactivo. Un fallo nunca detiene la rutina."""
    if asset.error or asset.kind != "cripto":
        return
    try:
        asset.liquidity = analyze_liquidity(asset)
    except Exception as e:  # la liquidez es un añadido: la etapa y el informe siguen igual
        log.append(f"{asset.key}: no se pudo calcular la liquidez ({type(e).__name__}: {str(e)[:120]})")
    if asset.liquidity is not None:
        for prof in asset.liquidity.profiles:
            for q in prof.quotes:
                if q.error and q.fee_day is None and "on-chain" in q.error:
                    log.append(f"{asset.key} {q.pool.label}: {q.error}")
                    break


def position_status(path: Path, assets: list[AssetResult], log: list[str]) -> list[dict]:
    """Estado de cada posición de posiciones.txt con el precio actual de su activo."""
    positions, warnings = load_positions(path)
    log.extend(warnings)
    prices = {a.symbol: a.last_price for a in assets if not a.error and a.kind == "cripto"}
    out = []
    for pos in positions:
        if pos.symbol not in prices:
            log.append(f"posiciones.txt: {pos.symbol} no está en la lista de seguimiento; añádelo para vigilarlo")
        out.append(check_position(pos, prices.get(pos.symbol)))
    return out


def position_alerts(prev: dict, items: list[dict]) -> list[dict]:
    """Avisa cuando una posición cambia de estado (entra en zona de aviso, sale o vuelve)."""
    alerts = []
    for i in items:
        key = f"{i['simbolo']} {i['min']:g}-{i['max']:g}"
        old = prev.get(key)
        if i["estado"] != old and (i["aviso"] or old is not None):
            alerts.append({"simbolo": i["simbolo"], "marco": "posición", "tipo": "posicion",
                           "texto": f"rango {i['min']:g}–{i['max']:g}: {i['texto']}"})
    return alerts


def run(watchlist: Path, out_dir: Path, now: pd.Timestamp | None = None,
        positions_file: Path | None = None, ia: bool = False) -> int:
    now = now or pd.Timestamp.now(tz="UTC")
    today = now.strftime("%Y-%m-%d")
    log = []
    if not watchlist.exists():
        print(f"No existe la lista {watchlist}. Crea el archivo con un símbolo por línea.")
        return 1
    symbols, warnings = load_watchlist(watchlist)
    log.extend(warnings)
    if not symbols:
        print(f"La lista {watchlist} está vacía.")
        return 1

    context = load_context(now)
    assets = []
    for sym in symbols:
        print(f"Analizando {sym}...", flush=True)
        asset = analyze_symbol(sym)
        assets.append(asset)
        if asset.error:
            log.append(f"{asset.key}: {asset.error}")
        else:
            add_liquidity(asset, log)
            write_html(asset, out_dir, index_link=True, context=context)

    hist_dir = out_dir / "historial"
    hist_dir.mkdir(parents=True, exist_ok=True)
    curr = snapshot([a for a in assets if not a.error])
    previous = load_previous(hist_dir, today)
    changes = diff(previous[1], curr) if previous else []
    changes += level_alerts(previous[1], curr) if previous else []
    if os.environ.get("ETAPAS_AVISO_PRUEBA"):
        changes.append({"simbolo": "PRUEBA", "marco": "", "tipo": "prueba",
                        "texto": "aviso de prueba: si te llega este correo, los avisos funcionan"})
    prev_date = previous[0] if previous else None

    positions = position_status(positions_file or watchlist.parent / "posiciones.txt", assets, log)
    prev_pos = load_previous_positions(hist_dir, today)
    changes += position_alerts(prev_pos, positions)
    pos_state = {f"{i['simbolo']} {i['min']:g}-{i['max']:g}": i["estado"] for i in positions}
    (hist_dir / f"{today}.json").write_text(
        json.dumps({"fecha": today, "activos": curr, "posiciones": pos_state}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # Agentes: dictamen por activo, listo para una IA (gratis: pegar en claude.ai; con --ia: API)
    for a in assets:
        if a.error:
            continue
        d = agentes.dictamen(a, a.liquidity, context, positions)
        agentes.write_prompt(d, out_dir)
        if ia:
            path, msg = agentes.ask_claude(d, out_dir)
            if path is None:
                log.append(f"{a.key}: IA no disponible: {msg}")

    write_guide(out_dir)
    index = write_index(assets, [c for c in changes if c.get("tipo") != "prueba"], prev_date, log,
                        out_dir, now, context, positions)
    # Añadir o quitar valores de la lista se ve en la web, pero no genera correo
    headlines = {a.key: headlines_for(context, a.symbol, a.kind) for a in assets if not a.error}
    write_alerts([c for c in changes if c.get("tipo") != "lista"], prev_date, out_dir, today, headlines)

    ok = sum(1 for a in assets if not a.error)
    summary = (f"{now:%Y-%m-%d %H:%M} UTC · {ok}/{len(assets)} activos analizados · "
               f"{len(changes)} cambios" + (f" desde {prev_date}" if prev_date else " (primera ejecución)"))
    with open(out_dir / "rutina.log", "a", encoding="utf-8") as f:
        f.write(summary + "\n")
        for c in changes:
            f.write(f"    {c['simbolo']} {c['marco']}: {c['texto']}\n")
        for line in log:
            f.write(f"    AVISO {line}\n")
    print(summary)
    for c in changes:
        print(f"  · {c['simbolo']} {c['marco']}: {c['texto']}")
    for line in log:
        print(f"  ! {line}")
    print(f"Índice: {index}")
    return 0 if ok else 1
