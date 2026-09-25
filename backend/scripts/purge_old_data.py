"""
Enforce config.DATA_RETENTION_DAYS (VA-B5 fix):
  cd backend
  python scripts/purge_old_data.py [--dry-run]

Permanently deletes every call older than the retention window, along with
its transcript, leads, meetings, escalations and sentiment report (see
database/crud.py's purge_calls_older_than for the deletion order — no FK
here has ON DELETE CASCADE).

Intended to run on a schedule (e.g. a daily k8s CronJob) — setting
DATA_RETENTION_DAYS alone does nothing without something actually running
this.
"""
import argparse
import asyncio
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select, func
from database.base import AsyncSessionLocal
from database.crud import purge_calls_older_than
from database.models import Call
import config


async def purge(dry_run: bool) -> None:
    cutoff = datetime.utcnow() - timedelta(days=config.DATA_RETENTION_DAYS)
    print(f"Retention window: {config.DATA_RETENTION_DAYS} days — cutoff: {cutoff.isoformat()}")

    async with AsyncSessionLocal() as db:
        if dry_run:
            count = (
                await db.execute(select(func.count(Call.id)).where(Call.created_at < cutoff))
            ).scalar() or 0
            print(f"[dry run] Would delete {count} call(s) and their related rows. No changes made.")
            return

        deleted = await purge_calls_older_than(db, cutoff)
        print(f"Deleted {deleted} row(s) across calls/transcripts/leads/meetings/escalations/sentiment_reports.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted without deleting anything")
    args = parser.parse_args()
    asyncio.run(purge(args.dry_run))
