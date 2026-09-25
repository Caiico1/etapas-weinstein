"""Rutina: analiza la lista de seguimiento, genera informes e índice y detecta cambios de etapa."""
import json
import re
from pathlib import Path

import pandas as pd

from .analysis import AssetResult, analyze_symbol
from .config import ORDER, TIMEFRAMES
from .report import write_html, write_index

_SYMBOL = re.compile(r"^[A-Z0-9]{1,15}$")


def load_watchlist(path: Path) -> tuple[list[str], list[str]]:
    """Devuelve (símbolos, avisos). Ignora comentarios (#), líneas vacías y duplicados."""
    symbols, warnings = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        sym = line.split("#", 1)[0].strip().upper()
        if not sym:
            continue
        if not _SYMBOL.match(sym):
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
        out[a.symbol] = {
            k: {"etapa": r.stage, "transicion": r.transition, "vela": r.candle_time}
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
            log.append(f"{sym}: {asset.error}")
        else:
            write_html(asset, out_dir, index_link=True)

    hist_dir = out_dir / "historial"
    hist_dir.mkdir(parents=True, exist_ok=True)
    curr = snapshot([a for a in assets if not a.error])
    previous = load_previous(hist_dir, today)
    changes = diff(previous[1], curr) if previous else []
    prev_date = previous[0] if previous else None
    (hist_dir / f"{today}.json").write_text(
        json.dumps({"fecha": today, "activos": curr}, ensure_ascii=False, indent=2), encoding="utf-8")

    index = write_index(assets, changes, prev_date, log, out_dir, now)

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
