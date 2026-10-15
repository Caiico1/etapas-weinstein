"""Mercados de Aave V3 (depósito, préstamo y parámetros de riesgo) leídos de la blockchain. Uso:
    python aave.py [--red base] [--json]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from etapas.aave import main
except ImportError as e:
    sys.exit(f"Faltan librerías ({e}). Instálalas con: pip install ccxt pandas numpy")

if __name__ == "__main__":
    sys.exit(main())
