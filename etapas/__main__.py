"""CLI.

python -m etapas BTC ETH [--html [DIR]] [--json] [--provisional]
python -m etapas                  # analiza la lista de seguimiento (watchlist.txt)
python -m etapas --rutina         # lista completa → informes, índice e historial en out/
"""
import argparse
import sys
from pathlib import Path

from .analysis import analyze_symbol
from .config import DISCLAIMER
from .report import render_json, render_text, write_html
from .rutina import load_watchlist, run as run_routine


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog="etapas",
        description="Etapa del ciclo de mercado (Weinstein) de criptoactivos en diario, semanal y mensual.")
    parser.add_argument("simbolos", nargs="*",
                        help="Criptoactivos, p. ej. BTC ETH SOL. Sin símbolos, se usa la lista")
    parser.add_argument("--lista", default="watchlist.txt", metavar="ARCHIVO",
                        help="Lista de seguimiento, un símbolo por línea (por defecto watchlist.txt)")
    parser.add_argument("--rutina", action="store_true",
                        help="Analiza la lista y genera informes, índice e historial de cambios")
    parser.add_argument("--html", nargs="?", const=".", metavar="DIR",
                        help="Genera etapas_<SÍMBOLO>.html con gráficos en DIR (por defecto, el actual)")
    parser.add_argument("--json", action="store_true", help="Salida estructurada en JSON")
    parser.add_argument("--provisional", action="store_true",
                        help="Añade una lectura provisional con la vela en curso")
    args = parser.parse_args(argv)

    if args.rutina:
        code = run_routine(Path(args.lista), Path(args.html or "out"))
        print(f"\n{DISCLAIMER}")
        return code

    symbols = args.simbolos
    if not symbols:
        lista = Path(args.lista)
        if not lista.exists():
            parser.error(f"indica símbolos o crea la lista {lista}")
        symbols, warnings = load_watchlist(lista)
        for w in warnings:
            print(f"Aviso: {w}", file=sys.stderr)
        if not symbols:
            parser.error(f"la lista {lista} está vacía")

    assets = [analyze_symbol(s, provisional=args.provisional) for s in symbols]

    if args.json:
        print(render_json(assets))
    else:
        for a in assets:
            print(render_text(a))
    if args.html is not None:
        for a in assets:
            path = write_html(a, Path(args.html))
            msg = f"HTML: {path}" if path else f"HTML: no generado para {a.symbol} (sin datos)"
            print(msg, file=sys.stderr if args.json else sys.stdout)
    if not args.json:
        print(f"\n{DISCLAIMER}")
    return 0 if any(not a.error for a in assets) else 1


if __name__ == "__main__":
    sys.exit(main())
