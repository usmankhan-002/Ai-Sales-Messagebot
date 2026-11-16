"""One-off migration: bot schema v1 -> v2 (organizations, clients, Trade Builder naming).

Runs against DATABASE_URL (the bot's own AISalesMessage_bot database only).
All-or-nothing in one transaction. Safe to re-run: exits if already applied.

    python -I migrations/001_align_with_trade_builder.py          # dry run
    python -I migrations/001_align_with_trade_builder.py --apply  # apply

v1 -> v2:
  * drop org_ranking_indicators and old-shape org_market_commentary (must be empty)
  * create organizations, clients, org_ai_settings, ai_rankings, new org_market_commentary
  * ai_drafts / ai_usage_log: org_id text -> uuid (old "demo" rows are filed under a
    new "Foundry FX" organization), add ai_drafts.client_id, ids -> bigint, foreign keys
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text  # noqa: E402

from app.db import get_engine  # noqa: E402
from app.models import Base  # noqa: E402

APPLY = "--apply" in sys.argv
DEMO_ORG_NAME = "Foundry FX"


def main() -> int:
    engine = get_engine()
    if engine is None:
        print("DATABASE_URL is not set.")
        return 1
    with engine.connect() as c:
        db_name = c.execute(text("select current_database()")).scalar()
    if db_name != "AISalesMessage_bot":
        print(f"Refusing to run: connected to {db_name!r}, expected the bot database.")
        return 1

    tables = set(inspect(engine).get_table_names())
    if "organizations" in tables:
        print("Already applied (organizations table exists). Nothing to do.")
        return 0

    with engine.begin() as c:  # one transaction
        counts = {t: c.execute(text(f"select count(*) from {t}")).scalar()
                  for t in ("org_market_commentary", "org_ranking_indicators", "ai_drafts", "ai_usage_log") if t in tables}
        print("Current rows:", counts)
        for t in ("org_market_commentary", "org_ranking_indicators"):
            if counts.get(t):
                raise SystemExit(f"Refusing: {t} is not empty; migrate it by hand.")
        org_values = set()
        for t in ("ai_drafts", "ai_usage_log"):
            if t in tables:
                org_values |= set(c.execute(text(f"select distinct org_id from {t} where org_id is not null")).scalars())
        unexpected = org_values - {"demo"}
        if unexpected:
            raise SystemExit(f"Refusing: unexpected org_id values {sorted(unexpected)}; migrate by hand.")

        print("Plan:")
        print("  drop empty org_market_commentary (old shape), org_ranking_indicators")
        print("  create organizations, clients, org_market_commentary, org_ai_settings, ai_rankings")
        print("  ai_drafts, ai_usage_log: org_id -> uuid, ids -> bigint, add client_id + foreign keys")
        if org_values:
            print(f"  file existing 'demo' rows under new organization {DEMO_ORG_NAME!r}")
        if not APPLY:
            print("Dry run only. Re-run with --apply.")
            raise _Rollback()

        for t in ("org_market_commentary", "org_ranking_indicators"):
            if t in tables:
                c.execute(text(f"drop table {t}"))

        # Creates only the tables that don't exist; leaves ai_drafts / ai_usage_log alone.
        Base.metadata.create_all(c)

        demo_org = None
        if org_values:
            demo_org = uuid.uuid4()
            c.execute(text("insert into organizations (id, name, status, created_at, updated_at) "
                           "values (:id, :name, 'active', now(), now())"), {"id": demo_org, "name": DEMO_ORG_NAME})
        mapped = f"'{demo_org}'::uuid" if demo_org else "NULL::uuid"  # generated here, not user input

        c.execute(text(f"""alter table ai_drafts
            alter column id type bigint,
            alter column org_id type uuid using (case when org_id = 'demo' then {mapped} else null end),
            add column client_id uuid"""))
        c.execute(text("""alter table ai_drafts
            add constraint ai_drafts_org_id_fkey foreign key (org_id) references organizations(id) on delete cascade,
            add constraint ai_drafts_client_id_fkey foreign key (client_id) references clients(id) on delete cascade"""))
        c.execute(text("create index if not exists ix_drafts_client on ai_drafts (client_id)"))
        c.execute(text("alter sequence if exists ai_drafts_id_seq as bigint"))

        c.execute(text(f"""alter table ai_usage_log
            alter column id type bigint,
            alter column org_id type uuid using (case when org_id = 'demo' then {mapped} else null end)"""))
        c.execute(text("""alter table ai_usage_log
            add constraint ai_usage_log_org_id_fkey foreign key (org_id) references organizations(id) on delete set null"""))
        c.execute(text("alter sequence if exists ai_usage_log_id_seq as bigint"))
        print("Applied.")
    return 0


class _Rollback(Exception):
    pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except _Rollback:
        sys.exit(0)
