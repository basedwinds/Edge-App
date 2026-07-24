"""Export/import the data that matters and CAN'T be re-scraped: your placed bets
(the tracker + paper-CLV history) and your settings. The 973 MB market-snapshot
history is NOT included -- it rebuilds forward on its own once the pollers run,
so there's no point carrying it. Use this to move your real state to another
computer (e.g. a laptop while travelling) without copying the whole DB.

    python scripts/portable_data.py export            # -> portable_state.json
    python scripts/portable_data.py import portable_state.json

Run from the backend/ dir with the venv active. Safe to re-run import: it
upserts by primary key, so it won't duplicate rows.
"""
import argparse
import datetime
import json
import os
import sys

# Allow running as `python scripts/portable_data.py` from the backend/ dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import SessionLocal, init_db
from app.db.models import PlacedBet, Setting

_DEFAULT = "portable_state.json"


def _colnames(model) -> list[str]:
    return [c.name for c in model.__table__.columns]


def _ser(v):
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    return v


def do_export(path: str) -> None:
    session = SessionLocal()
    try:
        bets = [{c: _ser(getattr(b, c)) for c in _colnames(PlacedBet)} for b in session.query(PlacedBet).all()]
        settings = [{c: _ser(getattr(s, c)) for c in _colnames(Setting)} for s in session.query(Setting).all()]
    finally:
        session.close()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"exported_at": datetime.datetime.utcnow().isoformat(), "placed_bets": bets, "settings": settings}, f, indent=2)
    print(f"Exported {len(bets)} placed bets + {len(settings)} settings -> {path}")


def _deser(model, row: dict) -> dict:
    dt_cols = {c.name for c in model.__table__.columns if str(c.type).startswith(("DATETIME", "DATE"))}
    out = {}
    for k, v in row.items():
        if k in dt_cols and isinstance(v, str):
            try:
                v = datetime.datetime.fromisoformat(v)
            except ValueError:
                v = None
        out[k] = v
    return out


def do_import(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    init_db()
    session = SessionLocal()
    try:
        for row in data.get("placed_bets", []):
            session.merge(PlacedBet(**_deser(PlacedBet, row)))  # merge = upsert by PK
        for row in data.get("settings", []):
            session.merge(Setting(**_deser(Setting, row)))
        session.commit()
        print(f"Imported {len(data.get('placed_bets', []))} placed bets + {len(data.get('settings', []))} settings")
    finally:
        session.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("path", nargs="?", default=_DEFAULT)
    im = sub.add_parser("import")
    im.add_argument("path", nargs="?", default=_DEFAULT)
    args = ap.parse_args()
    if args.cmd == "export":
        do_export(args.path)
    elif args.cmd == "import":
        do_import(args.path)
    else:
        ap.print_help()
        sys.exit(1)
