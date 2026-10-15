"""Qué duplas merecen una posición de liquidez concentrada hoy. Uso:
    python cribado.py --capital 5000 [--red base] [--solo-filtro] [--json]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from etapas.cribado import main
except ImportError as e:
    sys.exit(f"Faltan librerías ({e}). Instálalas con: pip install ccxt pandas numpy")

if __name__ == "__main__":
    sys.exit(main())
