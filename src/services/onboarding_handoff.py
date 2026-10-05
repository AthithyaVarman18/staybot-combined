"""
Escalation detection and internal handoff summaries for onboarding.

Rule-based on purpose: it only restates what the person wrote and facts
already stored on the case. It never invents facts, grants approvals or
changes the case. Confidence reflects how much structured information was
available, not how "sure" an AI feels.
"""

import re

CATEGORIES = {
    "legal": (r"\b(lawyer|attorney|legal|sue|court|evict\w*|discriminat\w*|fair housing|illegal)\b",
              "Legal review", "high", "Legal concern raised; do not give legal advice in chat."),
    "pricing": (r"\b(too (high|much|expensive)|lower (the )?rent|discount|negotiat\w*|price|cheaper|dispute\w* (the )?(rent|deposit|fee))\b",
                "Leasing manager", "medium", "Pricing or payment terms disputed."),
    "documents": (r"\b(can'?t upload|cannot upload|upload (failed|not working)|wrong document|document (problem|issue)|don'?t have (an? )?(id|document))\b",
                  "Onboarding coordinator", "medium", "Document problem reported."),
    "conflict": (r"\b(wrong|incorrect|not (me|mine|my)|mistake|not the (tenant|owner)|different (name|address|rent|date))\b",
                 "Onboarding coordinator", "medium", "Information on the case conflicts with what the person says."),
}
HELP = re.compile(r"^\s*(help|staff|agent|human|talk to (a )?(person|human|staff))\b", re.I)


def classify(message: str) -> str | None:
    if HELP.search(message or ""):
        return "help_request"
    lowered = (message or "").lower()
    for category, (pattern, *_rest) in CATEGORIES.items():
        if re.search(pattern, lowered):
            return category
    return None


def build(case: dict, party: str, category: str, message: str, missing: list[str], step_title: str) -> dict:
    advisor, urgency, reason = {
        "help_request": ("Onboarding coordinator", "medium", "The person asked to speak with staff."),
        "other": ("Onboarding coordinator", "low", "Staff follow-up requested."),
    }.get(category) or CATEGORIES[category][1:]
    known = sum(bool(x) for x in (case.get("tenant_id"), case.get("owner_id"), case.get("terms"), case.get("property_address")))
    confidence = "high" if known == 4 and message else "medium" if known >= 2 else "low"
    quoted = (message or "").strip()[:300]
    return {
        "role": party,
        "advisor_type": advisor,
        "urgency": urgency,
        "understood": f'The {party} wrote: "{quoted}"' if quoted else f"The {party} asked for help.",
        "context": f"Case {case.get('reference')} at step '{step_title}'. Missing: {', '.join(missing) if missing else 'nothing'}.",
        "next_action": f"{advisor} to reply to the {party} on WhatsApp and resolve the {category.replace('_', ' ')}.",
        "follow_up": "Record the outcome on the case; if any agreement detail changes, prepare a new agreement version for both parties to approve.",
        "escalation_reason": reason,
        "summary": f"{party.title()} on {case.get('reference')} needs staff: {reason}",
        "confidence": confidence,
        "generated_by": "rules",
    }
