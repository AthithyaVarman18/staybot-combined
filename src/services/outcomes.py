"""
Lead outcomes and score accuracy.

The team marks what really happened with a lead (rented, bought, listed,
lost, no response). The lead's score, tier and 9-part breakdown at that
moment are frozen with it. The accuracy report then shows whether the AI
Lead Score predicts results: hot leads should win more often than warm,
warm more than nurture, and each part should be higher for won leads.
"""

import statistics
from datetime import datetime, timezone

from src.services import db


WON = {"rented", "bought", "listed"}
CLOSED = WON | {"lost", "no_response"}
OUTCOMES = ["open"] + sorted(CLOSED)

TIER_ORDER = ["hot", "warm", "nurture", "unqualified"]
OLD_TIERS = {"very_hot": "hot", "cold": "nurture"}


def mark(lead: dict, outcome: str, note: str = None) -> dict:
    """Fields to save when the team marks an outcome."""

    if outcome == "open":
        return {
            "outcome": "open", "outcome_note": None, "outcome_at": None,
            "score_at_outcome": None, "tier_at_outcome": None, "components_at_outcome": None,
        }

    tier = OLD_TIERS.get(lead.get("lead_status"), lead.get("lead_status"))

    return {
        "outcome": outcome,
        "outcome_note": (note or "").strip() or None,
        "outcome_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "score_at_outcome": lead.get("intent_score"),
        "tier_at_outcome": tier,
        "components_at_outcome": (lead.get("lead_components") or {}).get("components"),
    }


def report(leads: list) -> dict:

    closed = [l for l in leads if l.get("outcome") in CLOSED]
    won = [l for l in closed if l["outcome"] in WON]
    lost = [l for l in closed if l["outcome"] not in WON]

    by_tier = []
    for tier in TIER_ORDER:
        rows = [l for l in closed if (l.get("tier_at_outcome") or "nurture") == tier]
        wins = sum(1 for l in rows if l["outcome"] in WON)
        by_tier.append({
            "tier": tier,
            "closed": len(rows),
            "won": wins,
            "win_rate": round(100 * wins / len(rows)) if rows else None,
        })

    # Does the score order match reality? Hot >= warm >= nurture >= unqualified.
    rates = [t["win_rate"] for t in by_tier if t["closed"] >= 3]
    if len(rates) < 2:
        verdict = "Mark at least 3 outcomes in two or more tiers to judge accuracy."
    elif all(a >= b for a, b in zip(rates, rates[1:])):
        verdict = "Good: higher tiers are winning more often."
    else:
        verdict = "Needs tuning: a lower tier is winning more often than a higher one."

    def avg(rows, key):
        values = [
            (l.get("components_at_outcome") or {}).get(key, {}).get("score")
            for l in rows
        ]
        values = [v for v in values if isinstance(v, (int, float))]
        return round(statistics.mean(values)) if values else None

    keys = []
    for l in closed:
        for key in (l.get("components_at_outcome") or {}):
            if key not in keys:
                keys.append(key)

    components = []
    for key in keys:
        won_avg, lost_avg = avg(won, key), avg(lost, key)
        label = next(
            ((l.get("components_at_outcome") or {}).get(key, {}).get("label") for l in closed
             if (l.get("components_at_outcome") or {}).get(key, {}).get("label")),
            key,
        )
        components.append({
            "key": key,
            "label": label,
            "won_avg": won_avg,
            "lost_avg": lost_avg,
            "gap": (won_avg - lost_avg) if won_avg is not None and lost_avg is not None else None,
        })

    components.sort(key=lambda c: (c["gap"] is None, -(c["gap"] or 0)))

    return {
        "total_leads": len(leads),
        "open": len(leads) - len(closed),
        "closed": len(closed),
        "won": len(won),
        "lost": len(lost),
        "win_rate": round(100 * len(won) / len(closed)) if closed else None,
        "avg_score_won": round(statistics.mean(l["score_at_outcome"] for l in won if l.get("score_at_outcome") is not None)) if any(l.get("score_at_outcome") is not None for l in won) else None,
        "avg_score_lost": round(statistics.mean(l["score_at_outcome"] for l in lost if l.get("score_at_outcome") is not None)) if any(l.get("score_at_outcome") is not None for l in lost) else None,
        "by_tier": by_tier,
        "verdict": verdict,
        "components": components,
    }
