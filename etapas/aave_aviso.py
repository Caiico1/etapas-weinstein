"""Aviso por correo cuando el factor de salud de una posición en Aave V3 baja de un umbral.

    python -m etapas.aave_aviso                 # direcciones de ~/.cripto-renta/config.json (o AAVE_CARTERAS)
    python -m etapas.aave_aviso --simular       # solo dice qué haría, sin tocar GitHub

Lee las posiciones con aave_posicion y lleva el estado en issues de GitHub (etiqueta «aave-hf»), que
GitHub envía por correo y al móvil, como los avisos de la rutina:
  - HF < 1,5 → abre una issue de aviso; HF < 1,3 → urgente (el umbral de defensa del backtest).
  - Si empeora de aviso a urgente, comenta en la misma issue (nuevo correo).
  - Cuando el HF vuelve a ≥ 1,6, comenta y la cierra. Entre 1,5 y 1,6 mantiene el estado, para no
    abrir y cerrar con cada vaivén.
Con la issue abierta no se repite el aviso, así que se puede ejecutar a menudo sin llenar el correo
(configuración actual: tarea de Windows a las 09:30 y al iniciar sesión, y GitHub Actions una vez al día).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass

from . import aave_posicion as AP
from .keccak import keccak256

AVISO, URGENTE, VUELTA = 1.5, 1.3, 1.6
OBJETIVO = 1.8                      # HF del perfil: cuánto devolver para volver a él
LABEL, LABEL_URGENTE = "aave-hf", "aave-hf-urgente"
REPO = os.environ.get("GITHUB_REPOSITORY", "Caiico1/etapas-weinstein")
ORDER = {"ok": 0, "aviso": 1, "urgente": 2}


def level(hf: float, prev: str) -> str:
    """Estado nuevo según el HF y el anterior (con histéresis entre AVISO y VUELTA)."""
    if hf < URGENTE:
        return "urgente"
    if hf < AVISO:
        return "urgente" if prev == "urgente" else "aviso"
    if hf < VUELTA and prev != "ok":
        return prev
    return "ok"


def action(prev: str, new: str) -> str | None:
    """abrir | escalar | cerrar | None (nada que avisar)."""
    if prev == new:
        return None
    if prev == "ok":
        return "abrir"
    if new == "ok":
        return "cerrar"
    return "escalar" if ORDER[new] > ORDER[prev] else None


def key(p: AP.Position) -> str:
    """Identificador de la posición sin la dirección: el repositorio es público (issues y logs de
    Actions incluidos), así que la dirección va resumida en un hash."""
    h = keccak256(p.address.lower().encode()).hex()[:10]
    return f"{p.chain}/{p.market}/{h}"


def marker(k: str) -> str:
    return f"<!-- {LABEL}:{k} -->"


def repay_to(p: AP.Position, target: float = OBJETIVO) -> float:
    """Deuda en $ a devolver para llevar el HF a `target` (con los precios de ahora)."""
    hf = p.health_recalc
    return max(0.0, p.debt_usd * (1 - hf / target)) if hf != float("inf") else 0.0


def title(p: AP.Position, new: str) -> str:
    tag = "URGENTE" if new == "urgente" else "Aviso"
    return f"{tag}: Aave {p.chain}/{p.market} factor de salud {AP._hf_txt(p.health_factor)}"


def body(p: AP.Position, new: str) -> str:
    """Sin dirección ni importes en $ (la issue es pública): HF, distancia y qué parte de la deuda
    devolver. Los importes los da `python -m etapas.aave_posicion` en el PC."""
    share = repay_to(p) / p.debt_usd if p.debt_usd else 0.0
    lines = [f"**Factor de salud {AP._hf_txt(p.health_factor)}** en Aave V3 {p.chain} (mercado {p.market}).", "",
             f"- Liquidación si todo lo no estable cae otro **{AP._pct(p.fall_to_liq)}**"
             + (f" o sube un **{AP._pct(p.rise_to_liq)}**" if p.rise_to_liq is not None else ""),
             f"- Para volver a HF {AP._hf_txt(OBJETIVO)}: devolver un **{AP._pct(share)} de la deuda** "
             "(o aportar más garantía).", ""]
    if new == "urgente":
        lines += [f"Está por debajo de {AP._hf_txt(URGENTE)}, el umbral en el que el backtest defiende la "
                  "posición. Con la reserva en estables, devuelve deuda ahora.", ""]
    lines += ["Compruébalo con `python -m etapas.aave_posicion` o en https://app.aave.com. "
              f"Esta issue se cierra sola cuando el HF vuelva a {AP._hf_txt(VUELTA)} o más.", "", marker(key(p))]
    return "\n".join(lines)


# --- GitHub (gh) ---------------------------------------------------------------------------------

@dataclass
class Issue:
    number: int
    key: str
    urgent: bool

    @property
    def level(self) -> str:
        return "urgente" if self.urgent else "aviso"


class GitHub:
    def __init__(self, repo: str = REPO):
        self.repo = repo

    def _gh(self, *args: str) -> str:
        return subprocess.run(["gh", *args, "--repo", self.repo], check=True, capture_output=True,
                              text=True, encoding="utf-8").stdout

    def open_issues(self) -> dict[str, Issue]:
        out = json.loads(self._gh("issue", "list", "--label", LABEL, "--state", "open",
                                  "--json", "number,body,labels", "--limit", "100"))
        issues = {}
        for i in out:
            for line in (i.get("body") or "").splitlines():
                if line.startswith(f"<!-- {LABEL}:"):
                    k = line.removeprefix(f"<!-- {LABEL}:").removesuffix(" -->")
                    issues[k] = Issue(i["number"], k, any(l["name"] == LABEL_URGENTE for l in i["labels"]))
        return issues

    def ensure_labels(self) -> None:
        self._gh("label", "create", LABEL, "--color", "d97706", "--force",
                 "--description", "Aviso de factor de salud en Aave")
        self._gh("label", "create", LABEL_URGENTE, "--color", "dc2626", "--force",
                 "--description", "Factor de salud en Aave por debajo del umbral de defensa")

    def create(self, title: str, body: str, urgent: bool) -> None:
        labels = [LABEL] + ([LABEL_URGENTE] if urgent else [])
        owner = self.repo.split("/")[0]
        self._gh("issue", "create", "--title", title, "--body", body, "--label", ",".join(labels),
                 "--assignee", owner)

    def escalate(self, n: int, title: str, body: str) -> None:
        self._gh("issue", "edit", str(n), "--title", title, "--add-label", LABEL_URGENTE)
        self._gh("issue", "comment", str(n), "--body", body)

    def close(self, n: int, comment: str) -> None:
        self._gh("issue", "close", str(n), "--comment", comment)


# --- ejecución -----------------------------------------------------------------------------------

def plan(positions: list[AP.Position], issues: dict[str, Issue]) -> list[tuple[str, AP.Position, Issue | None, str]]:
    """(acción, posición, issue abierta, estado nuevo) de lo que hay que hacer. Las issues de posiciones
    que ya no tienen deuda (o han desaparecido) se cierran aparte, en run()."""
    todo = []
    for p in positions:
        iss = issues.get(key(p))
        prev = iss.level if iss else "ok"
        new = level(p.health_factor, prev)
        act = action(prev, new)
        if act:
            todo.append((act, p, iss, new))
    return todo


def read_positions(addrs: list[str]) -> tuple[list[AP.Position], list[str]]:
    positions, failed = [], []
    for addr in addrs:
        for chain, markets in AP.MARKETS.items():
            for market in markets:
                try:
                    p = AP.position(addr, chain, market)
                except (OSError, ValueError, KeyError, TypeError) as e:
                    failed.append(key(AP.Position(addr, chain, market, 0, "", 0, 0, 0, 0, 0, None, None, None)))
                    print(f"No se pudo leer {chain}/{market}: {type(e).__name__}", file=sys.stderr)
                    continue
                if p:
                    positions.append(p)
    return positions, failed


def run(addrs: list[str], gh: GitHub | None, simulate: bool = False) -> list[str]:
    positions, failed = read_positions(addrs)
    issues = gh.open_issues() if gh else {}
    done = []
    for act, p, iss, new in plan(positions, issues):
        line = f"{act}: {key(p)} HF {AP._hf_txt(p.health_factor)} → {new}"
        done.append(line)
        if simulate or gh is None:
            continue
        if act == "abrir":
            gh.ensure_labels()
            gh.create(title(p, new), body(p, new), new == "urgente")
        elif act == "escalar":
            gh.escalate(iss.number, title(p, new), body(p, new))
        elif act == "cerrar":
            gh.close(iss.number, f"El factor de salud ha vuelto a {AP._hf_txt(p.health_factor)}. Se cierra el aviso.")
    # Issues abiertas de posiciones que ya no aparecen (deuda devuelta o cerrada): se cierran, salvo
    # que esa lectura haya fallado
    seen = {key(p) for p in positions}
    for k, iss in issues.items():
        if k not in seen and k not in failed:
            done.append(f"cerrar: {k} (ya no hay posición)")
            if not simulate:
                gh.close(iss.number, "La posición ya no tiene saldo en Aave. Se cierra el aviso.")
    return done


def addresses(cli: list[str]) -> list[str]:
    env = [a.strip() for a in os.environ.get("AAVE_CARTERAS", "").split(",") if a.strip()]
    return cli or env or AP.addresses_from_config()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="python -m etapas.aave_aviso", description=__doc__.split("\n\n")[0])
    ap.add_argument("direcciones", nargs="*", help="direcciones 0x... (por defecto, AAVE_CARTERAS o la config)")
    ap.add_argument("--simular", action="store_true", help="dice qué haría sin crear ni cerrar issues")
    ap.add_argument("--prueba", action="store_true",
                    help="abre un aviso de prueba (HF 1,40 ficticio en Base); la siguiente ejecución normal lo cierra")
    a = ap.parse_args(argv)
    addrs = addresses(a.direcciones)
    if not addrs:
        print("Sin direcciones: pásalas, ponlas en AAVE_CARTERAS o en «carteras» de ~/.cripto-renta/config.json.",
              file=sys.stderr)
        return 2
    try:
        if a.prueba:
            p = AP.Position(addrs[0], "base", "base", 0, "", 10_000.0, 5_000.0, 1.40, 1.40, 0.5, 0.29, None, None, [])
            gh = GitHub()
            gh.ensure_labels()
            gh.create("PRUEBA · " + title(p, "aviso"), "**Aviso de prueba con datos ficticios.** Si te llega este "
                      "correo, los avisos funcionan; la siguiente comprobación normal lo cerrará.\n\n" + body(p, "aviso"),
                      urgent=False)
            print("Aviso de prueba abierto.")
            return 0
        done = run(addrs, GitHub(), simulate=a.simular)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        # Python de Microsoft Store no deja a gh leer el token del Administrador de credenciales:
        # aave_aviso.bat se lo pasa en GH_TOKEN (en GitHub Actions ya viene puesto)
        err = getattr(e, "stderr", "") or str(e)
        print(f"No se pudo usar gh: {err.strip()[:200]}\nDefine GH_TOKEN (aave_aviso.bat lo hace con «gh auth token»).",
              file=sys.stderr)
        return 1
    print("\n".join(done) if done else "Sin cambios: ninguna posición cruza los umbrales.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
