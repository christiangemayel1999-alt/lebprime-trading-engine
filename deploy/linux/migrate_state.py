#!/usr/bin/env python3
"""Move LebPrime data and state from the Windows laptop to the Linux VPS.

Two steps, both with --dry-run:

  1. On Windows (bot and dashboard stopped, or at least the bot stopped):
       python deploy\\linux\\migrate_state.py export --out C:\\lebprime-export --dry-run
       python deploy\\linux\\migrate_state.py export --out C:\\lebprime-export
     -> a folder with consistent SQLite snapshots (online backup API), the other
        data files, config.json, and MANIFEST.json with a SHA-256 for every file.
     Copy it to the VPS, e.g.  scp -r C:\\lebprime-export admin@vps:/tmp/

  2. On the VPS, as root:
       python3 migrate_state.py import --bundle /tmp/lebprime-export --dry-run
       python3 migrate_state.py import --bundle /tmp/lebprime-export
     -> verifies every checksum, places files in /var/lib/lebprime, /var/log/lebprime
        and /opt/lebprime/app/config.json, rewrites Windows paths through the path map,
        and scrubs runtime state: no stale locks, PIDs, running flags or worker status,
        and execution starts PAUSED.

Standard library only (runs on the laptop's Python and the VPS system Python).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable

# Windows project folder -> Linux location. The app still sees these at its own base
# dir, because install creates symlinks /opt/lebprime/app/<name> -> target.
PATH_MAP = {
    "storage": "/var/lib/lebprime/storage",
    "reports": "/var/lib/lebprime/reports",
    "backtests": "/var/lib/lebprime/backtests",
    "LebPrime_Results": "/var/lib/lebprime/LebPrime_Results",
    "logs": "/var/log/lebprime",
}
APP_DIR = "/opt/lebprime/app"
SKIP_NAMES = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIXES = (".lock", ".db-wal", ".db-shm", ".tmp", ".pyc")
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
RUNTIME_FILES_TO_DROP = ("worker_status.json",)
CHUNK = 1 << 20


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def walk(root: Path, include_backups: bool) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES]
        for name in filenames:
            if name.endswith(SKIP_SUFFIXES):
                continue
            if not include_backups and ".bak-" in name:
                continue
            yield Path(dirpath) / name


# --------------------------------------------------------------------------- export

def export(project: Path, out: Path, dry_run: bool, include_backups: bool, include_logs: bool) -> int:
    dirs = [d for d in PATH_MAP if d != "logs" or include_logs]
    if out.exists() and any(out.iterdir()) and not dry_run:
        print(f"refusing to write into non-empty {out}", file=sys.stderr)
        return 2
    lock = project / "storage" / "bot_instance.lock"
    if lock.exists():
        print(f"note: {lock} exists. Make sure the Windows bot is stopped before exporting, or the snapshot may miss its last writes.")
    manifest: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(project),
        "files": {},
    }
    total = 0
    if not dry_run:
        out.mkdir(parents=True, exist_ok=True)
    sources = [(project / "config.json", "app/config.json")]
    for name in dirs:
        root = project / name
        if root.is_dir():
            for path in walk(root, include_backups):
                sources.append((path, f"data/{name}/{path.relative_to(root).as_posix()}"))
    for src, rel in sources:
        if not src.exists():
            continue
        size = src.stat().st_size
        total += size
        dst = out / rel
        kind = "sqlite" if src.suffix.lower() in SQLITE_SUFFIXES else "file"
        if dry_run:
            print(f"[dry-run] {kind:6} {size / 1e6:10.1f} MB  {src} -> {rel}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if kind == "sqlite":
            with sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30) as source, sqlite3.connect(dst) as target:
                source.backup(target)
            with sqlite3.connect(dst) as check:
                status = check.execute("PRAGMA integrity_check").fetchone()[0]
            if status != "ok":
                print(f"integrity_check failed for {src}: {status}", file=sys.stderr)
                return 1
        else:
            shutil.copy2(src, dst)
        manifest["files"][rel] = {"sha256": sha256(dst), "bytes": dst.stat().st_size, "kind": kind}
    print(f"{'would export' if dry_run else 'exported'} {len(sources)} files, {total / 1e9:.2f} GB")
    if not dry_run:
        (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        print(f"manifest: {out / 'MANIFEST.json'}")
    return 0


# --------------------------------------------------------------------------- import

def _windows_path_to_linux(value: str, source_root: str) -> str | None:
    """Map an absolute Windows path inside the old project folder to its Linux location."""
    if len(value) < 3 or value[1:3] not in (":\\", ":/"):
        return None
    path = PureWindowsPath(value)
    try:
        rel = path.relative_to(PureWindowsPath(source_root))
    except ValueError:
        return None
    parts = rel.parts
    if parts and parts[0] in PATH_MAP:
        return str(Path(PATH_MAP[parts[0]], *parts[1:]))
    return str(Path(APP_DIR, *parts))


def rewrite_paths(obj: Any, source_root: str, changes: list[str], where: str = "") -> Any:
    if isinstance(obj, dict):
        return {k: rewrite_paths(v, source_root, changes, f"{where}.{k}") for k, v in obj.items()}
    if isinstance(obj, list):
        return [rewrite_paths(v, source_root, changes, f"{where}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, str):
        mapped = _windows_path_to_linux(obj, source_root)
        if mapped is not None:
            changes.append(f"{where}: {obj} -> {mapped}")
            return mapped
        if len(obj) > 2 and obj[1:3] == ":\\":
            changes.append(f"{where}: WARNING unmapped Windows path left as is: {obj}")
    return obj


def scrub_control_state(state: dict[str, Any]) -> dict[str, Any]:
    state = dict(state)
    state.update(
        {
            "bot_running": False,
            "bot_pid": None,
            "request_shutdown": False,
            "request_reload_config": False,
            "pending_restart": False,
            "execution_paused": True,
            "last_command": "migration_import_pause",
            "last_command_by": "migrate_state.py",
            "last_command_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    runtime = dict(state.get("runtime") or {})
    runtime.update({"heartbeat_at": None, "heartbeat_status": None, "mt5_connected": False, "trade_allowed": False})
    state["runtime"] = runtime
    return state


def do_import(bundle: Path, dry_run: bool, owner: str, force: bool) -> int:
    manifest = json.loads((bundle / "MANIFEST.json").read_text(encoding="utf-8"))
    source_root = manifest["source"]
    bad = []
    for rel, meta in manifest["files"].items():
        path = bundle / rel
        if not path.exists() or path.stat().st_size != meta["bytes"] or sha256(path) != meta["sha256"]:
            bad.append(rel)
    if bad:
        print(f"{len(bad)} files failed checksum verification, e.g. {bad[:5]}. Copy the bundle again.", file=sys.stderr)
        return 1
    print(f"checksums OK for {len(manifest['files'])} files from {source_root}")

    plan: list[tuple[Path, Path]] = []
    for rel in manifest["files"]:
        if rel == "app/config.json":
            plan.append((bundle / rel, Path(APP_DIR) / "config.json"))
            continue
        _, name, rest = rel.split("/", 2)
        plan.append((bundle / rel, Path(PATH_MAP[name]) / rest))
    existing = [dst for _, dst in plan if dst.exists() and dst.name != "config.json"]
    if existing and not force:
        print(f"{len(existing)} target files already exist (e.g. {existing[0]}). Re-run with --force to overwrite.", file=sys.stderr)
        return 2

    for src, dst in plan:
        if dry_run:
            print(f"[dry-run] {src.relative_to(bundle)} -> {dst}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # Path map + state scrub on the placed copies.
    changes: list[str] = []
    config_path = Path(APP_DIR) / "config.json"
    config = json.loads((bundle / "app/config.json").read_text(encoding="utf-8"))
    config = rewrite_paths(config, source_root, changes, "config")
    if (config.get("mt5") or {}).get("terminal_path"):
        changes.append(f"config.mt5.terminal_path: {config['mt5']['terminal_path']} -> null (the gateway owns the terminal path)")
        config["mt5"]["terminal_path"] = None
    shadow = Path(PATH_MAP["storage"]) / "config.json"

    storage = Path(PATH_MAP["storage"])
    control_src = bundle / "data/storage/control_state.json"
    actions = [f"write {config_path} (and storage shadow copy) with mapped paths"]
    if control_src.exists():
        actions.append(f"scrub {storage / 'control_state.json'}: no PID, not running, execution PAUSED")
    actions += [f"delete stale runtime file {storage / name}" for name in RUNTIME_FILES_TO_DROP]
    actions.append(f"chown -R {owner} {', '.join(sorted(set(PATH_MAP.values())))}")
    for line in changes:
        print(("[dry-run] " if dry_run else "") + "path map: " + line)
    for line in actions:
        print(("[dry-run] " if dry_run else "") + line)
    if dry_run:
        return 0

    text = json.dumps(config, indent=4)
    config_path.write_text(text, encoding="utf-8")
    if shadow.exists():
        shadow_cfg = rewrite_paths(json.loads(shadow.read_text(encoding="utf-8")), source_root, [], "shadow")
        if (shadow_cfg.get("mt5") or {}).get("terminal_path"):
            shadow_cfg["mt5"]["terminal_path"] = None
        shadow.write_text(json.dumps(shadow_cfg, indent=4), encoding="utf-8")
    if control_src.exists():
        state = scrub_control_state(json.loads(control_src.read_text(encoding="utf-8")))
        (storage / "control_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    for name in RUNTIME_FILES_TO_DROP:
        (storage / name).unlink(missing_ok=True)
    for lock in storage.rglob("*.lock"):
        lock.unlink()
    user, _, group = owner.partition(":")
    for root in set(PATH_MAP.values()) | {str(config_path)}:
        for path in [Path(root), *Path(root).rglob("*")] if Path(root).is_dir() else [Path(root)]:
            shutil.chown(path, user=user, group=group or user)
    print("import complete. Execution is paused; start services in the order in MIGRATION_PLAN.md.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="run on Windows: snapshot data into a bundle folder")
    exp.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[2])
    exp.add_argument("--out", type=Path, required=True)
    exp.add_argument("--include-backups", action="store_true", help="also copy *.bak-* files (about 450 MB of old DB backups)")
    exp.add_argument("--include-logs", action="store_true")
    exp.add_argument("--dry-run", action="store_true")
    imp = sub.add_parser("import", help="run on the VPS as root: verify and place the bundle")
    imp.add_argument("--bundle", type=Path, required=True)
    imp.add_argument("--owner", default="lebprime:lebprime")
    imp.add_argument("--force", action="store_true")
    imp.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "export":
        return export(args.project.resolve(), args.out, args.dry_run, args.include_backups, args.include_logs)
    if os.name != "nt" and os.geteuid() != 0 and not args.dry_run:
        print("import must run as root (it sets ownership)", file=sys.stderr)
        return 2
    return do_import(args.bundle, args.dry_run, args.owner, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
