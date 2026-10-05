"""
Fair Housing check on the AI's reply, before it reaches the customer.

US federal Fair Housing Act + North Carolina State Fair Housing Act: never
treat people differently, or steer them toward / away from a home or area,
because of race, color, religion, sex (incl. sexual orientation and gender
identity), national origin, familial status (children) or disability.

The AI is told the rules (# FAIR HOUSING in src/prompts/system_prompt.py).
This is the safety net in code: a reply that still says who a home or area
"suits", describes an area by who lives there, or judges its safety or
schools is caught here. chat.py then asks the AI once to rewrite it, and
sends SAFE_REPLY if the rewrite still breaks a rule.

Deliberately narrow: plain facts about the HOME (bedrooms, fenced yard,
stairs, near a park) pass, and sentences that refuse or hedge ("I can't
say whether an area is safe") are never flagged.
"""

import re

PEOPLE = (r"(?:families|family|kids|children|child|singles|single people|couples|young couples|"
          r"young professionals|professionals|retirees|seniors|elderly|students|bachelors|"
          r"empty[- ]nesters|newlyweds|christians|muslims|jews|hindus|immigrants|foreigners)")
GROUPS = (r"(?:white|black|african[- ]american|hispanic|latino|latina|latinx|asian|indian|mexican|"
          r"chinese|arab|christian|jewish|muslim|hindu|catholic|immigrant|foreign|minority|"
          r"gay|lgbt\w*)")

RULES = [
    ("who_it_suits",
     re.compile(rf"\b(?:good|great|perfect|ideal|best|suited|suitable|wonderful|nice|safe)\s+(?:for|to)\s+(?:a\s+|an\s+)?"
                rf"(?:growing\s+|young\s+|small\s+|big\s+|large\s+)?{PEOPLE}\b", re.I),
     "it says who the home or area is good for (a familial status / protected class preference)"),
    ("who_it_suits",
     re.compile(r"\b(?:family|kid|child)[- ]friendly\b", re.I),
     "\"family-friendly\" describes who should live there (familial status)"),
    ("no_children",
     re.compile(r"\b(?:no\s+(?:kids|children)|adults?\s+only|not\s+(?:suitable|ideal|good)\s+for\s+(?:kids|children))\b", re.I),
     "it limits children (familial status)"),
    ("who_lives_there",
     re.compile(rf"\b(?:mostly|mainly|predominantly|largely|majority|heavily|lots of|many)\s+{GROUPS}\b", re.I),
     "it describes the area by who lives there (race / religion / national origin)"),
    ("who_lives_there",
     re.compile(rf"\b{GROUPS}\s+(?:neighbou?rhood|area|community|part of town|population|families|residents)\b", re.I),
     "it describes the area by who lives there (race / religion / national origin)"),
    ("area_safety",
     re.compile(r"\b(?:safe|safer|safest|unsafe|dangerous|rough|sketchy|bad|good|high[- ]crime|low[- ]crime|crime[- ]free)\s+"
                r"(?:neighbou?rhood|area|part of town|side of town|street|community)\b", re.I),
     "it judges how safe or 'good' an area is (often used for steering)"),
    ("area_safety",
     re.compile(r"\b(?:neighbou?rhood|area|community|street)\s+is\s+(?:very\s+|really\s+|quite\s+|pretty\s+)?"
                r"(?:safe|unsafe|dangerous|rough|sketchy|bad|crime[- ]free)\b", re.I),
     "it judges how safe an area is (often used for steering)"),
    ("schools",
     re.compile(r"\b(?:good|great|excellent|top|top[- ]rated|best|bad|poor|failing|better|worse)\s+(?:public\s+)?schools?\b", re.I),
     "it rates the schools (often used for steering)"),
    ("language",
     re.compile(r"\b(?:english[- ]speak\w*\s+only|must\s+speak\s+english|no\s+(?:foreigners|immigrants))\b", re.I),
     "it limits people by language / national origin"),
]

# A sentence that refuses or hedges is fine: "I can't say whether an area is safe."
HEDGE = re.compile(r"\b(?:can't|cannot|can not|don't|do not|won't|will not|not able|unable|aren't able|"
                   r"isn't something|not something|rather not|avoid|instead)\b", re.I)

SAFE_REPLY = (
    "I can share facts about the home itself - price, size, features and location. "
    "For schools, the school district's website is the best source, and for crime, the local "
    "police department's crime map. Would you like details on any of the homes?"
)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def check(reply: str) -> list[dict]:
    """Fair Housing problems in the reply: [{"type", "claim", "truth"}], same
    shape as fact_check.check_reply() issues."""
    issues = []
    for sentence in _sentences(reply):
        if HEDGE.search(sentence):
            continue
        for kind, pattern, why in RULES:
            if pattern.search(sentence):
                issues.append({"type": f"fair_housing_{kind}", "claim": sentence, "truth": why})
                break
    return issues


def correction_note(issues: list[dict]) -> str:
    lines = "\n".join(f"- You wrote: \"{i['claim']}\" - not allowed: {i['truth']}." for i in issues[:5])
    return (
        "FAIR HOUSING CHECK FAILED on your previous draft:\n"
        f"{lines}\n"
        "Rewrite the reply describing only the HOME and plain facts (price, size, features, "
        "distance to places). Never say who a home or area suits, who lives there, or whether an "
        "area or its schools are good or safe - point to the school district website or the "
        "police crime map instead. Follow # FAIR HOUSING."
    )


def is_fair_housing(issue: dict) -> bool:
    return str(issue.get("type") or "").startswith("fair_housing_")
