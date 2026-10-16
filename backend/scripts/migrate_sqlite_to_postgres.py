"""One-off: copy data/continuum.db into the Postgres DATABASE_URL set in .env.

Usage: python scripts/migrate_sqlite_to_postgres.py [sqlite_path]
Defaults to ./data/continuum.db. Target is app.config.settings.database_url.
"""

import sys

from sqlmodel import SQLModel

from app.config import settings
from app.db import create_db_engine


def main() -> None:
    sqlite_path = sys.argv[1] if len(sys.argv) > 1 else "./data/continuum.db"
    target_url = settings.database_url
    if target_url.startswith("sqlite"):
        raise SystemExit("DATABASE_URL in .env is still sqlite — set it to your Neon connection string first.")

    source = create_db_engine(f"sqlite:///{sqlite_path}")
    target = create_db_engine(target_url)

    with source.connect() as src_conn, target.connect() as dst_conn:
        for table in SQLModel.metadata.sorted_tables:
            rows = [dict(row._mapping) for row in src_conn.execute(table.select())]
            if not rows:
                continue
            dst_conn.execute(table.delete())
            dst_conn.execute(table.insert(), rows)
            print(f"{table.name}: {len(rows)} rows")
        dst_conn.commit()

    print("Done. Point DATABASE_URL at Postgres and restart uvicorn.")


if __name__ == "__main__":
    main()
