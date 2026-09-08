"""Run the DB-backed execution worker."""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.worker import WorkerService
from services.worker_control import read_worker_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Trading Bot execution worker")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Worker polling interval in seconds")
    parser.add_argument("--heartbeat-stale-seconds", type=int, default=300, help="Mark RUNNING jobs interrupted when heartbeat exceeds this age")
    parser.add_argument("--once", action="store_true", help="Claim and execute one queued job, then exit")
    parser.add_argument("--max-jobs", type=int, default=None, help="Claim and execute at most this many queued jobs, then exit")
    return parser.parse_args()


@contextlib.contextmanager
def worker_lock(enabled: bool = True):
    """Prevent duplicate long-running workers from the dashboard launcher."""
    if not enabled:
        yield True
        return
    lock_dir = PROJECT_ROOT / "storage"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "worker_instance.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if sys.platform.startswith("win"):
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                status = read_worker_status(PROJECT_ROOT)
                print(f"Worker lock is held; exiting. heartbeat_state={status.get('state')} last_seen={status.get('last_seen_at')}")
                yield False
                return
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                status = read_worker_status(PROJECT_ROOT)
                print(f"Worker lock is held; exiting. heartbeat_state={status.get('state')} last_seen={status.get('last_seen_at')}")
                yield False
                return
        handle.seek(0)
        handle.truncate()
        handle.write(str(PROJECT_ROOT))
        handle.flush()
        yield True
    finally:
        try:
            if sys.platform.startswith("win"):
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def main() -> None:
    args = parse_args()
    worker = WorkerService(
        PROJECT_ROOT,
        poll_interval_seconds=args.poll_interval,
        heartbeat_stale_seconds=args.heartbeat_stale_seconds,
    )
    if args.once or args.max_jobs is not None:
        max_jobs = int(args.max_jobs or 1)
        ran = 0
        try:
            for _ in range(max(0, max_jobs)):
                if worker.run_once() is None:
                    break
                ran += 1
        finally:
            worker.mark_stopped()
        print(f"worker executed {ran} job(s)")
        return
    with worker_lock() as acquired:
        if acquired:
            worker.run_forever()


if __name__ == "__main__":
    main()
