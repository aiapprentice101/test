#!/usr/bin/env python3
"""Run a wide fare search from the terminal.

Your example, verbatim:

    python scripts/find_deals.py \
        --from SIN --to-region US --airline SQ --cabin BUSINESS \
        --depart-between 2026-12-01 2026-12-31 \
        --return-between 2027-01-01 2027-01-31 \
        --trip-days 30

Add --dry-run to see the request budget without touching the network.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.deals.engine import run_deal_search  # noqa: E402
from app.deals.planner import PlanError, plan_search  # noqa: E402
from app.deals.schemas import DealSearchRequest  # noqa: E402
from app.providers.fli_provider import FliProvider, configure_rate_limit  # noqa: E402
from app.service import SearchError  # noqa: E402


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="origins", nargs="+", required=True, metavar="IATA")
    p.add_argument("--to", dest="destinations", nargs="*", default=[], metavar="IATA")
    p.add_argument("--to-region", dest="region", default=None, help="e.g. US, EUROPE, JAPAN")
    p.add_argument("--depart-between", nargs=2, type=_date, required=True, metavar=("FROM", "TO"))
    p.add_argument("--return-between", nargs=2, type=_date, default=None, metavar=("FROM", "TO"))
    p.add_argument("--trip-days", type=int, default=None)
    p.add_argument("--min-trip-days", type=int, default=None)
    p.add_argument("--max-trip-days", type=int, default=None)
    p.add_argument("--cabin", default="ECONOMY")
    p.add_argument("--airline", dest="airlines", nargs="*", default=[], metavar="IATA")
    p.add_argument("--stops", default="ANY")
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--currency", default="USD")
    p.add_argument("--top", type=int, default=10, help="how many cheapest dates to price in full")
    p.add_argument("--max-requests", type=int, default=60)
    p.add_argument("--bundle", action="store_true", help="one scan for all destinations (cheaper, no attribution)")
    p.add_argument("--rate", type=float, default=1, help="upstream requests per second")
    p.add_argument("--dry-run", action="store_true", help="show the plan and exit")
    return p


def main() -> int:
    args = build_parser().parse_args()

    request = DealSearchRequest(
        origins=args.origins,
        destinations=args.destinations,
        destination_region=args.region,
        depart_from=args.depart_between[0],
        depart_to=args.depart_between[1],
        return_from=args.return_between[0] if args.return_between else None,
        return_to=args.return_between[1] if args.return_between else None,
        min_trip_days=args.min_trip_days or args.trip_days,
        max_trip_days=args.max_trip_days or args.trip_days,
        cabin_class=args.cabin,
        airlines=args.airlines,
        max_stops=args.stops,
        adults=args.adults,
        currency=args.currency,
        refine_top_n=args.top,
        max_requests=args.max_requests,
        bundle_destinations=args.bundle,
    )

    try:
        plan = plan_search(request)
    except PlanError as exc:
        print(f"Cannot run this search: {exc}", file=sys.stderr)
        return 2

    print(f"Plan: {plan.scan_requests} calendar scans + {plan.refine_top_n} full lookups "
          f"= ~{plan.estimated_requests} requests")
    print(f"Destinations: {', '.join(plan.destinations)}")
    print(f"Trip lengths: {', '.join(str(d) for d in plan.durations)} days")
    if plan.scans:
        print(f"Departure window: {plan.scans[0].depart_from} .. {plan.scans[0].depart_to}")
    for note in plan.notes:
        print(f"  note: {note}")
    if args.dry_run:
        return 0

    configure_rate_limit(max(1, int(args.rate)))
    print(f"\nSearching at ~{args.rate} req/sec (about "
          f"{plan.estimated_requests / max(args.rate, 0.1):.0f}s)...\n")

    def progress(stage: str, done: int, total: int) -> None:
        print(f"\r  {stage}: {done}/{total}", end="", flush=True)

    try:
        result = run_deal_search(request, FliProvider(), progress)
    except SearchError as exc:
        print(f"\nSearch failed: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"Hint: {exc.hint}", file=sys.stderr)
        return 1

    print("\n")
    print("=" * 78)
    print(result.summary)
    print("=" * 78)

    if result.deals:
        print(f"\n{'#':<3} {'ROUTE':<12} {'DEPART':<12} {'RETURN':<12} {'PRICE':>12}  FLIGHTS")
        print("-" * 78)
        for deal in result.deals:
            route = f"{deal.origin}-{deal.destination}"
            flights = ""
            if deal.itinerary:
                flights = " / ".join(
                    " ".join(f"{leg.airline_code}{leg.flight_number}" for leg in s.legs)
                    for s in deal.itinerary.slices
                )
            elif deal.refine_note:
                flights = f"({deal.refine_note})"
            price = f"{deal.currency} {deal.grid_price:,.0f}"
            print(f"{deal.rank:<3} {route:<12} {deal.departure_date!s:<12} "
                  f"{deal.return_date or '-'!s:<12} {price:>12}  {flights}")

    if result.cheapest_by_destination:
        print("\nCheapest by destination:")
        for dest, price in sorted(result.cheapest_by_destination.items(), key=lambda kv: kv[1]):
            print(f"  {dest}: {result.currency} {price:,.0f}")

    stats = result.stats
    print(f"\n{stats.requests_made} requests in {stats.elapsed_seconds}s | "
          f"{stats.grid_cells} date/route combinations priced | "
          f"{stats.scans_failed} scans failed")
    for warning in result.warnings:
        print(f"  warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
