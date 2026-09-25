"""Rutina: analiza la lista de seguimiento, genera informes e índice y detecta cambios de etapa."""
import json
import os
import re
from pathlib import Path

import pandas as pd

from .analysis import AssetResult, analyze_symbol, parse_symbol
from .config import DISCLAIMER, ORDER, TIMEFRAMES
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


def diff(prev: dict, curr: dict) -> list[dict]:
    """Cambios de etapa y transiciones nuevas entre dos instantáneas."""
    changes = []
    for sym, tfs in curr.items():
        if sym not in prev:
            changes.append({"simbolo": sym, "marco": "", "texto": "añadido a la lista"})
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
            changes.append({"simbolo": sym, "marco": "", "texto": "retirado de la lista"})
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


def write_alerts(items: list[dict], prev_date: str | None, out_dir: Path, today: str) -> Path | None:
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
    if web:
        lines += ["", f"Informes completos: {web}"]
    lines += ["", f"_{DISCLAIMER}_"]
    (out_dir / "alertas.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "alertas_titulo.txt").write_text(
        f"Etapas {today}: {n}", encoding="utf-8")
    return out_dir / "alertas.md"


def run(watchlist: Path, out_dir: Path, now: pd.Timestamp | None = None) -> int:
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

    assets = []
    for sym in symbols:
        print(f"Analizando {sym}...", flush=True)
        asset = analyze_symbol(sym)
        assets.append(asset)
        if asset.error:
            log.append(f"{asset.key}: {asset.error}")
        else:
            write_html(asset, out_dir, index_link=True)

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
    (hist_dir / f"{today}.json").write_text(
        json.dumps({"fecha": today, "activos": curr}, ensure_ascii=False, indent=2), encoding="utf-8")

    index = write_index(assets, [c for c in changes if c.get("tipo") != "prueba"], prev_date, log,
                        out_dir, now)
    write_alerts(changes, prev_date, out_dir, today)

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
