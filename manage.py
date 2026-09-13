"""CostCare management commands.

  python manage.py create-admin you@example.com "a-strong-password"
  python manage.py seed-demo
  python manage.py discover "private hospitals in Cape Town" --limit 10
  python manage.py sync-google
  python manage.py import-prices prices.xlsx            # preview only
  python manage.py import-prices prices.xlsx --apply    # apply changes
  python manage.py export-prices prices.xlsx
"""

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import or_, select

from app.auth import hash_password
from app.database import SessionLocal, init_db
from app.models import Provider, User
from app.seed import seed_catalogue, seed_demo
from app.services import excel_import, google_places


def main() -> int:
    parser = argparse.ArgumentParser(description="CostCare management commands")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-admin", help="Create or promote an admin user")
    p.add_argument("email")
    p.add_argument("password")
    p.add_argument("--name", default="Admin")

    sub.add_parser("seed-demo", help="Load the service catalogue and sample providers (only if no providers exist)")
    sub.add_parser("sync-google", help="Refresh Google reviews/ratings/locations for all linked providers")

    p = sub.add_parser("discover", help="Import real facilities from Google Places")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("import-prices", help="Import an Excel/CSV price list")
    p.add_argument("file")
    p.add_argument("--apply", action="store_true", help="Apply changes (otherwise preview only)")
    p.add_argument("--hide-missing", action="store_true", help="Hide services not in the file for providers it contains")

    p = sub.add_parser("export-prices", help="Export all prices to Excel")
    p.add_argument("out", nargs="?", default="costcare-prices.xlsx")

    args = parser.parse_args()
    init_db()

    with SessionLocal() as db:
        if args.cmd == "create-admin":
            user = db.scalar(select(User).where(User.email == args.email.lower()))
            if user is None:
                user = User(email=args.email.lower(), name=args.name, password_hash=hash_password(args.password))
                db.add(user)
            user.is_admin = True
            user.password_hash = hash_password(args.password)
            db.commit()
            print(f"Admin ready: {user.email}")

        elif args.cmd == "seed-demo":
            seed_catalogue(db)
            print("Sample providers loaded." if seed_demo(db) else "Providers already exist — catalogue refreshed only.")

        elif args.cmd == "sync-google":
            providers = db.scalars(select(Provider).where(
                or_(Provider.google_place_id != "", Provider.google_profile_url != ""))).all()
            ok = 0
            for provider in providers:
                try:
                    result = google_places.sync_provider(db, provider)
                    ok += 1
                    print(f"✓ {provider.name}: {result['rating']}★, {result['reviews_stored']} reviews")
                except Exception as exc:  # keep going for other providers
                    db.rollback()
                    print(f"✗ {provider.name}: {exc}")
            print(f"Synced {ok}/{len(providers)} providers.")

        elif args.cmd == "discover":
            created = google_places.discover_providers(db, args.query, args.limit)
            for item in created:
                print(f"+ {item['name']} — {item['address']} ({item['rating']}★, {item['reviews_stored']} reviews)")
            print(f"Imported {len(created)} providers. Add their prices in the admin dashboard or via Excel.")

        elif args.cmd == "import-prices":
            path = Path(args.file)
            job = excel_import.create_import_job(db, path.read_bytes(), path.name, None)
            print("Preview:", json.dumps(json.loads(job.summary_json), indent=2))
            for err in json.loads(job.errors_json):
                print(f"  row {err['row']}: {err['error']}")
            if args.apply:
                result = excel_import.apply_import_job(db, job, hide_missing=args.hide_missing)
                print("Applied:", json.dumps(result, indent=2))
                if result["google_sync_provider_ids"]:
                    print("Run `python manage.py sync-google` to capture Google reviews for new links.")
            else:
                print(f"Nothing changed. Re-run with --apply, or apply job #{job.id} in the admin dashboard.")

        elif args.cmd == "export-prices":
            Path(args.out).write_bytes(excel_import.export_prices(db))
            print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
