"""
AI test exam.

Sends every example chat in evals/cases.json to the running server's
/analyze endpoint and checks the answers with fixed rules. Nothing is
saved to Supabase (no session_id is sent).

Usage (server must be running):
    .venv/bin/python evals/run_evals.py
    .venv/bin/python evals/run_evals.py --url http://127.0.0.1:8003
    .venv/bin/python evals/run_evals.py --only viewings     # one category
    .venv/bin/python evals/run_evals.py --case pets-adyar   # one case
    .venv/bin/python evals/run_evals.py --repeat 3          # run each case 3x to catch flaky answers

Exit code is 1 if the pass rate is below --min-pass (default 90%).
"""

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


HERE = Path(__file__).resolve().parent
CASES_PATH = HERE / "cases.json"
RESULTS_DIR = HERE / "results"
TIMEZONE = ZoneInfo("Asia/Kolkata")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def today():
    return datetime.now(TIMEZONE).date()


def resolve_date_token(value):
    """'{tomorrow}' / '{saturday}' -> ISO date relative to today."""

    if not isinstance(value, str) or not value.startswith("{"):
        return value

    token = value.strip("{}").lower()
    base = today()

    if token == "today":
        return base.isoformat()
    if token == "tomorrow":
        return (base + timedelta(days=1)).isoformat()
    if token in WEEKDAYS:
        return (base + timedelta(days=(WEEKDAYS.index(token) - base.weekday()) % 7)).isoformat()

    raise ValueError(f"Unknown date token {value}")


def number_in(value):
    """Pull a number out of '40k', '₹40,000', 40000."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    match = re.search(r"(\d+(?:,\d+)*(?:\.\d+)?)\s*(k|lakh|lac|l|crore|cr)?\b", str(value).lower())
    if not match:
        return None

    units = {"k": 1e3, "lakh": 1e5, "lac": 1e5, "l": 1e5, "crore": 1e7, "cr": 1e7}
    return float(match.group(1).replace(",", "")) * units.get(match.group(2) or "", 1)


def field_matches(actual, expected):
    """Loose comparison for extracted fields: numbers by value, text by
    case-insensitive 'contains', booleans exactly."""

    if isinstance(expected, bool) or expected is None:
        return actual is expected or (expected is None and actual in ["", [], None])

    if isinstance(expected, (int, float)):
        return number_in(actual) == float(expected)

    return str(expected).lower() in str(actual or "").lower()


def contains_any(text, words):
    text = text.lower()
    return any(word.lower() in text for word in words)


# ---------------------------------------------------------------------
# Checks: each returns None when fine, or a short failure message
# ---------------------------------------------------------------------

def run_checks(expect: dict, result: dict) -> list[str]:

    failures = []
    reply = str(result.get("response") or "")
    q = result.get("qualification") or {}
    score = q.get("intent_score")
    matches = [m.get("id") for m in (result.get("matches") or [])]
    viewing = result.get("viewing") or {}
    breakdown = [b.get("reason", "") for b in (q.get("score_breakdown") or [])]

    def fail(message):
        failures.append(message)

    for key, want in expect.items():

        if key == "role" and result.get("role") != want:
            fail(f"role was {result.get('role')!r}, expected {want!r}")

        elif key == "intent_in" and result.get("intent") not in want:
            fail(f"intent was {result.get('intent')!r}, expected one of {want}")

        elif key == "lead_status_in" and q.get("lead_status") not in want:
            fail(f"lead status was {q.get('lead_status')!r}, expected one of {want}")

        elif key == "score_min" and (score is None or score < want):
            fail(f"score was {score}, expected at least {want}")

        elif key == "score_max" and (score is None or score > want):
            fail(f"score was {score}, expected at most {want}")

        elif key == "breakdown_has":
            for reason in want:
                if not any(reason.lower() in b.lower() for b in breakdown):
                    fail(f"score reasons {breakdown} missing {reason!r}")

        elif key == "breakdown_has_any":
            if not any(reason.lower() in b.lower() for reason in want for b in breakdown):
                fail(f"score reasons {breakdown} should include one of {want}")

        elif key == "breakdown_not_has":
            for reason in want:
                if any(reason.lower() in b.lower() for b in breakdown):
                    fail(f"score reasons unexpectedly include {reason!r}")

        elif key == "reply_contains_any" and not contains_any(reply, want):
            fail(f"reply should mention one of {want}")

        elif key == "reply_contains_all":
            for word in want:
                if word.lower() not in reply.lower():
                    fail(f"reply should mention {word!r}")

        elif key == "reply_not_contains":
            for word in want:
                if word.lower() in reply.lower():
                    fail(f"reply must NOT say {word!r}")

        elif key == "requirements":
            req = result.get("requirements") or {}
            for field, value in want.items():
                if not field_matches(req.get(field), value):
                    fail(f"requirements.{field} was {req.get(field)!r}, expected {value!r}")

        elif key == "property_details":
            details = result.get("property_details") or {}
            for field, value in want.items():
                if not field_matches(details.get(field), value):
                    fail(f"property_details.{field} was {details.get(field)!r}, expected {value!r}")

        elif key == "matches_include":
            for listing_id in want:
                if listing_id not in matches:
                    fail(f"search results {matches} missing {listing_id!r}")

        elif key == "matches_exclude":
            for listing_id in want:
                if listing_id in matches:
                    fail(f"search results must NOT include {listing_id!r}")

        elif key == "first_match" and (not matches or matches[0] != want):
            fail(f"first search result was {matches[:1]}, expected {want!r}")

        elif key == "no_matches" and want and matches:
            fail(f"expected no search results, got {matches}")

        elif key == "has_matches" and want and not matches:
            fail("expected search results, got none")

        elif key == "no_search" and want and "matches" in result:
            fail("expected no search at all")

        elif key == "viewing":
            for field, value in want.items():
                expected = resolve_date_token(value) if field == "date" else value
                if viewing.get(field) != expected:
                    fail(f"viewing.{field} was {viewing.get(field)!r}, expected {expected!r}")

        elif key == "viewing_missing_includes":
            for item in want:
                if item not in (viewing.get("missing") or []):
                    fail(f"viewing.missing {viewing.get('missing')} should include {item!r}")

        elif key == "viewing_problem" and want and not viewing.get("problem"):
            fail(f"expected the viewing slot to be rejected, got {viewing}")

        elif key == "quick_reply" and bool(result.get("quick_reply")) != bool(want):
            fail(f"quick_reply was {result.get('quick_reply')!r}, expected {'an instant reply' if want else 'an AI reply'}")

        elif key == "max_ms" and ((result.get("_perf") or {}).get("total_ms") or 0) > want:
            fail(f"reply took {(result.get('_perf') or {}).get('total_ms')} ms, limit {want} ms")

        elif key == "no_viewing" and want and viewing.get("date") and viewing.get("time"):
            fail(f"expected no viewing request, got {viewing}")

        elif key not in {
            "role", "intent_in", "lead_status_in", "score_min", "score_max", "breakdown_has", "breakdown_has_any",
            "breakdown_not_has", "reply_contains_any", "reply_contains_all", "reply_not_contains",
            "requirements", "property_details", "matches_include", "matches_exclude", "first_match",
            "no_matches", "has_matches", "no_search", "viewing", "viewing_missing_includes",
            "viewing_problem", "no_viewing", "quick_reply", "max_ms",
        }:
            fail(f"unknown check {key!r} in cases.json")

    return failures


# ---------------------------------------------------------------------
# Running a case
# ---------------------------------------------------------------------

PACE = {"seconds": 4.5, "last": 0.0}
PACE_LOCK = threading.Lock()
QUOTA_RETRIES = 3
QUOTA_WAIT_SECONDS = 45


def paced_post(url: str, payload: dict):
    """POST /analyze, spaced out to stay under the AI's per-minute limit,
    waiting and retrying when the provider says the quota is used up."""

    for attempt in range(QUOTA_RETRIES + 1):

        with PACE_LOCK:
            wait = PACE["last"] + PACE["seconds"] - time.time()
            if wait > 0:
                time.sleep(wait)
            PACE["last"] = time.time()

        response = requests.post(url, json=payload, timeout=300)

        quota_hit = response.status_code == 500 and re.search(
            r"RATE_LIMIT|quota|daily limit|429", response.text, re.IGNORECASE
        )

        if not quota_hit or attempt == QUOTA_RETRIES:
            return response, bool(quota_hit)

        print(f"  (AI quota hit, waiting {QUOTA_WAIT_SECONDS}s and retrying...)")
        time.sleep(QUOTA_WAIT_SECONDS)


def run_case(case: dict, base_url: str, contexts: dict, titles: dict) -> dict:

    listing = case.get("property")

    if listing and listing not in contexts:
        return {"id": case["id"], "category": case.get("category"), "passed": False,
                "failures": [f"property {listing!r} not found in /properties"], "turns": []}

    history = []
    turns = []
    failures = []
    skipped = None

    for number, turn in enumerate(case["turns"], start=1):

        started = time.time()

        try:
            response, quota_hit = paced_post(f"{base_url}/analyze", {
                "message": turn["user"],
                "conversation_history": history,
                "property_context": contexts.get(listing, {}) if listing else {},
                "listing_id": listing,
                "listing_title": titles.get(listing),
            })
            result = response.json()
        except Exception as e:
            skipped = f"turn {number}: request failed: {e}"
            break

        seconds = round(time.time() - started, 1)

        if quota_hit:
            skipped = f"turn {number}: AI quota used up, couldn't run"
            break

        if response.status_code != 200:
            failures.append(f"turn {number}: HTTP {response.status_code}: {str(result)[:200]}")
            break

        turn_failures = [f"turn {number}: {f}" for f in run_checks(turn.get("expect", {}), result)]
        failures.extend(turn_failures)

        turns.append({
            "user": turn["user"],
            "reply": result.get("response"),
            "seconds": seconds,
            "model": result.get("_model_used"),
            "failures": turn_failures,
        })

        history += [
            {"role": "user", "content": turn["user"]},
            {"role": "assistant", "content": result.get("response") or ""},
        ]

    return {
        "id": case["id"],
        "category": case.get("category", "other"),
        "passed": not failures and not skipped,
        "skipped": skipped,
        "failures": failures,
        "turns": turns,
    }


def main():

    parser = argparse.ArgumentParser(description="Run the AI test exam.")
    parser.add_argument("--url", default="http://127.0.0.1:8003")
    parser.add_argument("--only", help="run one category")
    parser.add_argument("--case", help="run one case id")
    parser.add_argument("--repeat", type=int, default=1, help="run each case N times")
    parser.add_argument("--workers", type=int, default=2, help="cases run at the same time")
    parser.add_argument("--pace", type=float, default=4.5, help="seconds between AI calls (free Gemini: 15/minute)")
    parser.add_argument("--min-pass", type=float, default=90.0, help="pass rate %% needed to exit 0")
    args = parser.parse_args()
    PACE["seconds"] = args.pace

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))

    if args.only:
        cases = [c for c in cases if c.get("category") == args.only]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
    if not cases:
        sys.exit("No cases selected.")

    try:
        listings = requests.get(f"{args.url}/properties", timeout=20).json()["properties"]
    except Exception as e:
        sys.exit(f"Server not reachable at {args.url} ({e}). Start it with ./run.sh first.")

    contexts = {p["id"]: p["context"] for p in listings}
    titles = {p["id"]: p["title"] for p in listings}

    jobs = [case for case in cases for _ in range(args.repeat)]
    turns_total = sum(len(c["turns"]) for c in jobs)
    print(f"Running {len(jobs)} chats ({turns_total} AI calls) against {args.url} ...\n")

    started = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda c: run_case(c, args.url, contexts, titles), jobs))

    # Report card (cases that couldn't run are not counted as pass or fail)
    ran = [r for r in results if not r["skipped"]]
    skipped = [r for r in results if r["skipped"]]

    by_category = {}
    for r in ran:
        stats = by_category.setdefault(r["category"], [0, 0])
        stats[0] += r["passed"]
        stats[1] += 1

    width = max([len(c) for c in by_category] or [5])
    for category, (passed, total) in sorted(by_category.items()):
        bar = "█" * round(10 * passed / total) + "░" * (10 - round(10 * passed / total))
        print(f"  {category:<{width}}  {bar}  {passed}/{total}")

    passed = sum(r["passed"] for r in ran)
    rate = 100 * passed / len(ran) if ran else 0.0
    print(f"\n  TOTAL  {passed}/{len(ran)} passed ({rate:.0f}%) in {time.time() - started:.0f}s")
    if skipped:
        print(f"  {len(skipped)} couldn't run (AI quota/network) - not counted: {', '.join(r['id'] for r in skipped)}")
    print()

    failed = [r for r in ran if not r["passed"]]
    for r in failed:
        print(f"✗ {r['id']}  [{r['category']}]")
        for f in r["failures"]:
            print(f"    - {f}")
        if r["turns"]:
            print(f"    last reply: {str(r['turns'][-1]['reply'])[:220]!r}")
        print()

    RESULTS_DIR.mkdir(exist_ok=True)
    report = RESULTS_DIR / f"{datetime.now(TIMEZONE):%Y-%m-%d_%H-%M}.json"
    report.write_text(json.dumps({
        "ran_at": datetime.now(TIMEZONE).isoformat(),
        "url": args.url,
        "passed": passed,
        "total": len(ran),
        "skipped": len(skipped),
        "pass_rate": round(rate, 1),
        "by_category": by_category,
        "results": results,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Full report: {report}")

    sys.exit(0 if ran and rate >= args.min_pass else 1)


if __name__ == "__main__":
    main()
