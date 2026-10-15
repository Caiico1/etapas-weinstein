"""Mejores oportunidades de liquidez concentrada para un par. Uso:
    python pool.py ETH/USDC --capital 5000 [--red base] [--json]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from etapas.oportunidad import main
except ImportError as e:
    sys.exit(f"Faltan librerías ({e}). Instálalas con: pip install ccxt pandas numpy")

if __name__ == "__main__":
    sys.exit(main())
