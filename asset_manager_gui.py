"""Deprecated Gradio entrypoint.

The asset browser UI is now a React app served by Hono:

    cd web && npm install && npm run build && npm start

Open http://127.0.0.1:7860
"""

from __future__ import annotations

import sys


def main() -> None:
    print(
        "asset_manager_gui.py (Gradio) は廃止されました。\n"
        "React + Hono 版を起動してください:\n\n"
        "  cd web\n"
        "  npm install\n"
        "  npm run build\n"
        "  npm start\n\n"
        "開発時 (API :7860 + Vite :5173):\n"
        "  npm run dev\n",
        file=sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    main()
