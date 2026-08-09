from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

# `timeout=30` (SQLite busy-wait, seconds) makes a writer that finds the DB
# locked RETRY for up to 30s instead of raising OperationalError immediately
# -- SQLAlchemy's `connect_args` default is timeout=5, too short for this
# app's own concurrent startup writers. Real, reproduced bug (2026-07-17,
# adding a 3rd concurrent sport-refresh startup thread for MLB): NFL's and
# NBA's own refresh threads started throwing "database is locked" that
# hadn't been visibly hit before with only 2 concurrent threads -- latent
# contention that a 3rd thread finally surfaced, not new load MLB itself
# caused (each sport's own writes succeeded; it was cross-thread contention).
# WAL journal mode (set per-connection below, since SQLite's `PRAGMA` is
# connection-scoped, not database-wide-persistent across engine restarts in
# a way `connect_args` alone can set) lets readers and a writer proceed
# concurrently instead of exclusively blocking each other -- both fixes
# together are the standard, documented remedy for this exact SQLite
# multi-writer-thread pattern, not sport-specific.
engine = create_engine(settings.sqlite_url(), connect_args={"check_same_thread": False, "timeout": 120})


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=120000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


_MISSING_COLUMNS_BY_TABLE = {
    "soccer_matches": [("first_scorer", "VARCHAR")],
    "nfl_games": [
        ("location", "VARCHAR"), ("stadium", "VARCHAR"), ("surface", "VARCHAR"),
        ("away_score_1h", "INTEGER"), ("home_score_1h", "INTEGER"),
    ],
    "markets": [
        ("group_label", "VARCHAR"),
        ("sport", "VARCHAR"),
        ("nba_game_id", "VARCHAR"),
        ("wnba_game_id", "VARCHAR"),
        ("cfb_game_id", "VARCHAR"),
        ("mlb_game_id", "VARCHAR"),
        ("mma_fight_id", "VARCHAR"),
        ("tennis_match_id", "INTEGER"),
        ("soccer_match_id", "INTEGER"),
        ("correct_score_home", "INTEGER"),
        ("correct_score_away", "INTEGER"),
        ("valorant_match_id", "INTEGER"),
        ("cs2_match_id", "INTEGER"),
        ("lol_match_id", "INTEGER"),
        ("cod_match_id", "INTEGER"),
        ("race_event_id", "INTEGER"),
    ],
    # is_live is here rather than only on the model because cod_matches can
    # already exist without it -- the table was created one commit before the
    # column was added. Same additive path every other late column took.
    "cod_matches": [
        ("is_live", "BOOLEAN DEFAULT 0"),
    ],
    "news_adjustment_cache": [("home_scoring_penalty_pp", "FLOAT"), ("away_scoring_penalty_pp", "FLOAT")],
    "placed_bets": [
        ("sport", "VARCHAR"),
        ("nba_game_id", "VARCHAR"),
        ("wnba_game_id", "VARCHAR"),
        ("cfb_game_id", "VARCHAR"),
        ("mlb_game_id", "VARCHAR"),
        ("mma_fight_id", "VARCHAR"),
        ("tennis_match_id", "INTEGER"),
        ("soccer_match_id", "INTEGER"),
        ("valorant_match_id", "INTEGER"),
        ("cs2_match_id", "INTEGER"),
        ("lol_match_id", "INTEGER"),
        ("cod_match_id", "INTEGER"),
        ("paper", "BOOLEAN DEFAULT 0"),
        ("race_event_id", "INTEGER"),
        ("original_start_time", "VARCHAR"),
        ("league", "VARCHAR"),
    ],
    "catalog_entries": [
        ("sport", "VARCHAR"),
        ("disposition", "VARCHAR"),
        ("note", "VARCHAR"),
    ],
    "race_events": [
        ("result_json", "VARCHAR"),
    ],
    "mma_fights": [
        ("estimated_start_time", "VARCHAR"),
    ],
    "tennis_matches": [
        ("expected_expiration_time", "VARCHAR"),
        ("start_time_source", "VARCHAR"),
        ("best_of", "INTEGER"),
        ("estimated_start_time", "VARCHAR"),
    ],
}


def _add_missing_columns():
    """`create_all` only creates missing TABLES, not missing columns on ones
    that already exist -- this repo has no Alembic migration setup, so new
    nullable columns added to an existing model (e.g. NflGame.location/
    stadium, Market.group_label) need this to show up in an already-
    initialized dev DB without wiping cached data. Safe to run every
    startup: skips columns already present."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table, columns in _MISSING_COLUMNS_BY_TABLE.items():
        if table not in existing_tables:
            continue
        existing_cols = {c["name"] for c in inspector.get_columns(table)}
        for col_name, col_type in columns:
            if col_name not in existing_cols:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"))


_SPORT_BACKFILL_TABLES = ["markets", "placed_bets", "catalog_entries"]


def _backfill_sport_column():
    """The generic _add_missing_columns helper has no notion of a column
    DEFAULT (plain `ADD COLUMN {name} {type}`, matching every prior nullable
    column added this way) -- so existing NFL rows get `sport=NULL`, not the
    model's Python-side default of "nfl", after the migration above runs.
    Backfilling explicitly here rather than teaching the shared helper a
    one-off DEFAULT clause it's never needed before. Idempotent (only
    touches NULL rows), safe every startup."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table in _SPORT_BACKFILL_TABLES:
        if table not in existing_tables:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE {table} SET sport = 'nfl' WHERE sport IS NULL"))


def _add_missing_indexes():
    """Same reasoning as _add_missing_columns -- `create_all` only adds
    indexes for brand-new tables, not ones that already exist in an
    already-initialized dev DB. `IF NOT EXISTS` makes this safe to run every
    startup."""
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_market_snapshots_market_ts ON market_snapshots (market_id, ts)"))


def init_db():
    from app.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    _backfill_sport_column()
    _add_missing_indexes()


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
