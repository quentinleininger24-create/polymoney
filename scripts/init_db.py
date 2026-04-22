"""Bootstrap the database: create every table from shared.models.

This is the simple path for single-user deployment. If you later need
schema migrations, run `alembic revision --autogenerate -m 'init'` once,
then `alembic upgrade head` going forward; the autogenerate pass will
diff the running DB against the models and produce the version file.
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine

from shared.config import settings
from shared.models import Base


def main() -> int:
    url = settings.database_url_sync
    print(f"bootstrapping DB at {url}")
    engine = create_engine(url, echo=False, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        result = conn.execute(
            Base.metadata.tables[next(iter(Base.metadata.tables))].select().limit(0)
        )
        result.close()
    print(f"done. tables: {len(Base.metadata.tables)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
