#!/usr/bin/env python3
"""Build authentic product media directly from original photoshoot and client assets.

Delegates to tools/build_authentic_product_media.py.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.build_authentic_product_media import main

if __name__ == "__main__":
    main()
