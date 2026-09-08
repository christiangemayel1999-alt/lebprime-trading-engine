from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.database import DatabaseService
from utils import load_json, utc_now


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Print a compact trade lifecycle/dashboard debug snapshot.")
    parser.add_argument("--base-dir", default=".", help="Project root directory")
    parser.add_argument("--day", default=None, help="UTC day to summarize, YYYY-MM-DD")
    parser.add_argument("--recent-closed-limit", type=int, default=10, help="Number of recent closed trades to print")
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    base_dir = Path(args.base_dir).resolve()
    config = load_json(base_dir / "config.json", {})
    configured_db = Path(config.get("storage", {}).get("database_path", "storage/bot.db"))
    db_path = configured_db if configured_db.is_absolute() else (base_dir / configured_db)
    database = DatabaseService(db_path)
    snapshot = database.get_trade_lifecycle_debug_snapshot(
        day=args.day or utc_now().date().isoformat(),
        recent_closed_limit=int(args.recent_closed_limit),
    )
    print(json.dumps(snapshot, indent=int(args.indent), ensure_ascii=True, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
