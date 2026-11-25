"""Vigilante del stop de tus posiciones de liquidez abiertas: cada hora mira en Revert el tick del pool de
cada posición y avisa cuando se acerca a su stop (a menos de un 3 %) o lo cruza.

    python -m etapas.vigilante_stop               # revisa, avisa y abre/cierra la issue
    python -m etapas.vigilante_stop --simular     # solo dice qué haría (sin notificación ni GitHub)
    python -m etapas.vigilante_stop --probar      # manda una notificación de prueba

El stop de cada posición es el que sugiere el simulador (`posiciones_lp.simular`, a 30 días), o el que
elijas en ~/.cripto-renta/stops.json ({"n.º de posición": "5 % bajo el borde"}); si el simulador no sugiere
ninguno, vigila el borde de abajo y el aviso lo dice. El nivel se recalcula una vez al día o si cambia el
rango (out/vigilante_stop_niveles.json); el último estado de cada posición se guarda para avisar solo
cuando empeora (out/vigilante_stop_estado.json). Todo local, fuera de git.

Avisos:
  - notificación de Windows con el detalle (par, precio, stop, qué hacer);
  - issue de GitHub con la etiqueta «stop-posicion» (llega por correo, también al móvil) SIN pares, precios
    ni importes, porque el repositorio es público; se cierra sola cuando ninguna está cerca ni cruzada.
El vigilante no cierra nada: Revert es solo lectura. Lo lanza la tarea de Windows «Vigilante stop» cada hora
(vigilante_stop.vbs → .bat).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import posiciones_lp as P
from . import simulador as S

OUT = Path(__file__).resolve().parent.parent / "out"
NIVELES = OUT / "vigilante_stop_niveles.json"
ESTADO = OUT / "vigilante_stop_estado.json"
RECALCULO_H = 24
DIAS = 30
ESCENARIOS = 500
LABEL = "stop-posicion"
MARKER = f"<!-- {LABEL} -->"
REPO = os.environ.get("GITHUB_REPOSITORY", "Caiico1/etapas-weinstein")
GRAVEDAD = {"lejos": 0, "cerca": 1, "cruzado": 2}


def _leer(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _escribir(path: Path, d: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def niveles(posiciones: list[dict], cache: dict, simular=P.simular, elegidos: dict | None = None,
            ahora: float | None = None) -> dict:
    """El stop de cada posición (n.º → datos), reutilizando el de la caché si tiene menos de RECALCULO_H
    horas, el mismo rango y la misma elección."""
    ahora = time.time() if ahora is None else ahora
    elegidos = P.stops_elegidos() if elegidos is None else elegidos
    out = {}
    for p in posiciones:
        k = str(p["id"])
        c = cache.get(k)
        el = elegidos.get(k)
        if (c and c.get("tl") == p["tl"] and c.get("tu") == p["tu"] and c.get("elegido") == el
                and ahora - c.get("t", 0) < RECALCULO_H * 3600):
            out[k] = c
            continue
        r = simular(p, DIAS, ESCENARIOS)
        base = {"tl": p["tl"], "tu": p["tu"], "elegido": el, "t": ahora, "par": p["par"], "red": p["red"],
                "label": r.get("label", p["par"])}
        out[k] = base | ({"error": r["error"]} if "error" in r else {"stop": {x: v for x, v in r["stop"].items() if x != "estado"}})
    return out


def revisar(posiciones: list[dict], nv: dict, previo: dict) -> tuple[dict, list[dict]]:
    """Estado de cada posición con su tick de hoy y los cambios a peor (lejos → cerca → cruzado)."""
    estado, cambios = {}, []
    for p in posiciones:
        k = str(p["id"])
        n = nv.get(k) or {}
        if "stop" not in n or p.get("tick") is None:
            continue
        e = S.estado_stop(p["tick"], n["stop"]["nivel"])
        antes = previo.get(k, "lejos")
        estado[k] = e
        if GRAVEDAD[e] > GRAVEDAD[antes]:
            precio = precio_de_tick(p["tick"], n["stop"]["nivel"])
            cambios.append({"id": p["id"], "estado": e, "antes": antes, "par": n["label"], "red": n["red"],
                            "precio": precio, "stop": n["stop"], "valor": p.get("valor")})
    return estado, cambios


def precio_de_tick(tick: float, nivel: dict) -> float:
    """Precio mostrado (el del activo) a partir del tick del pool, con el del stop como referencia."""
    s = 1 if nivel["asset0"] else -1
    return nivel["precio"] * S.B.TICK_BASE ** (s * (tick - nivel["tick"]))


def texto(c: dict) -> tuple[str, str]:
    st = c["stop"]
    que = (f"stop «{st['regla']}» en {S._fmt_price(st['precio'])}" if st["sugerido"] or st["elegido"]
           else f"borde de abajo en {S._fmt_price(st['precio'])} (el simulador no sugería stop)")
    if c["estado"] == "cruzado":
        titulo = f"Stop cruzado: n.º {c['id']} {c['par']}"
        cuerpo = (f"{c['red']}: precio {S._fmt_price(c['precio'])}, por debajo de su {que}. "
                  + ("Cierra la posición y pasa a dólares en Revert o Uniswap si mantienes el plan."
                     if st["sugerido"] or st["elegido"] else "Decide si cerrar o esperar a que vuelva."))
    else:
        titulo = f"Cerca del stop: n.º {c['id']} {c['par']}"
        cuerpo = f"{c['red']}: precio {S._fmt_price(c['precio'])}, a menos de un 3 % de su {que}. Prepárate para cerrar."
    return titulo, cuerpo


_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName('text')
$n.Item(0).AppendChild($t.CreateTextNode($env:VS_TITULO)) > $null
$n.Item(1).AppendChild($t.CreateTextNode($env:VS_CUERPO)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def notificar_windows(titulo: str, cuerpo: str) -> None:
    env = os.environ | {"VS_TITULO": titulo, "VS_CUERPO": cuerpo}
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST], env=env, check=True,
                   capture_output=True, timeout=60)


class GitHub:
    def __init__(self, repo: str = REPO):
        self.repo = repo

    def _gh(self, *args: str) -> str:
        return subprocess.run(["gh", *args, "--repo", self.repo], check=True, capture_output=True,
                              text=True, encoding="utf-8").stdout

    def open_issue(self) -> int | None:
        out = json.loads(self._gh("issue", "list", "--label", LABEL, "--state", "open",
                                  "--json", "number,body", "--limit", "20"))
        return next((i["number"] for i in out if MARKER in (i.get("body") or "")), None)

    def create(self, title: str, body: str) -> None:
        self._gh("label", "create", LABEL, "--color", "e5534b", "--force",
                 "--description", "Una posición de liquidez está cerca de su stop o lo ha cruzado")
        self._gh("issue", "create", "--title", title, "--body", body, "--label", LABEL,
                 "--assignee", self.repo.split("/")[0])

    def comment(self, n: int, body: str) -> None:
        self._gh("issue", "comment", str(n), "--body", body)

    def close(self, n: int, comment: str) -> None:
        self._gh("issue", "close", str(n), "--comment", comment)


def resumen_publico(estado: dict) -> str:
    """Solo cuántas: el repositorio es público (sin pares, precios, importes ni números de posición)."""
    cz = sum(e == "cruzado" for e in estado.values())
    ce = sum(e == "cerca" for e in estado.values())
    partes = ([f"{cz} {'posición ha cruzado' if cz == 1 else 'posiciones han cruzado'} su stop"] if cz else []) + \
             ([f"{ce} {'está' if ce == 1 else 'están'} a menos de un 3 % de él"] if ce else [])
    return " y ".join(partes)


def run(gh: GitHub | None = None, notificar=notificar_windows, leer=P.leer, simular=P.simular,
        niveles_path: Path = NIVELES, estado_path: Path = ESTADO, guardar: bool = True) -> str:
    """`guardar` False (--simular): no anota el estado, para que la siguiente revisión real sí avise."""
    pos = leer(cache_s=0)
    nv = niveles(pos, _leer(niveles_path), simular)
    _escribir(niveles_path, nv)
    previo = _leer(estado_path)
    estado, cambios = revisar(pos, nv, previo)
    for c in cambios:
        titulo, cuerpo = texto(c)
        print(f"{titulo} · {cuerpo}")
        if notificar:
            try:
                notificar(titulo, cuerpo)
            except (subprocess.SubprocessError, OSError) as e:
                print(f"no se pudo mostrar la notificación: {e}", file=sys.stderr)
    if guardar:
        _escribir(estado_path, estado)
    pub = resumen_publico(estado)
    acc = ""
    if gh:
        n = gh.open_issue()
        if cambios and pub:
            cuerpo = (f"{pub[0].upper() + pub[1:]}.\n\nEl detalle (qué posición, precio y stop) está en la notificación "
                      "del PC y en el simulador, «Mis posiciones abiertas». Aquí no van pares, precios ni importes "
                      "porque el repositorio es público.\n\nSe cierra sola cuando ninguna esté cerca de su stop.\n\n" + MARKER)
            if n is None:
                gh.create("Vigilante del stop: revisa tus posiciones", cuerpo)
                acc = "abierta"
            else:
                gh.comment(n, cuerpo)
                acc = "comentada"
        elif n is not None and not pub:
            gh.close(n, "Ninguna posición está ya cerca de su stop (o se cerró).")
            acc = "cerrada"
    vig = sum(1 for n in nv.values() if "stop" in n)
    return (f"{time.strftime('%Y-%m-%d %H:%M')} · {len(pos)} posiciones, {vig} vigiladas · "
            f"{pub or 'todas lejos del stop'}" + (f" · issue {acc}" if acc else ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--simular", action="store_true", help="sin notificación ni GitHub")
    ap.add_argument("--probar", action="store_true", help="manda una notificación de prueba")
    a = ap.parse_args(argv)
    if a.probar:
        notificar_windows("Vigilante del stop", "Notificación de prueba: así te avisará si una posición se acerca a su stop.")
        return 0
    try:
        print(run(None if a.simular else GitHub(), None if a.simular else notificar_windows, guardar=not a.simular))
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
