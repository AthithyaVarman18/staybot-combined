from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "src" / "static" / "index.html"
REQUIREMENTS = ROOT / "src" / "static" / "requirements.html"


def test_requirement_submit_hands_verified_matches_to_existing_chat():
    form = REQUIREMENTS.read_text(encoding="utf-8")
    chat = INDEX.read_text(encoding="utf-8")

    assert "staybot_requirement_bootstrap" in form
    assert "data.matches || {matches:[], total:0}" in form
    assert "consumeRequirementBootstrap" in chat
    assert "addMessage('assistant', intro, { matches })" in chat


def test_requirement_bootstrap_is_consumed_once_after_chat_load():
    chat = INDEX.read_text(encoding="utf-8")
    assert "sessionStorage.removeItem('staybot_requirement_bootstrap')" in chat
    assert "await loadConversation({ latest: true });" in chat
    assert "if (accountRole && accountRole !== 'admin') consumeRequirementBootstrap();" in chat


def test_chat_cards_display_match_score_from_backend():
    chat = INDEX.read_text(encoding="utf-8")
    assert "m.match_score != null" in chat
    assert "⭐ ${m.match_score}% Match" in chat
