"""Comisiones frente a variación de una posición abierta. Uso:
    python seguimiento.py base 12345 [--v4] --capital 4500 --precio 2650 [--json]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from etapas.seguimiento import main
except ImportError as e:
    sys.exit(f"Faltan librerías ({e}). Instálalas con: pip install ccxt pandas numpy")

if __name__ == "__main__":
    sys.exit(main())
