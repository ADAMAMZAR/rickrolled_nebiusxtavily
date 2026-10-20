"""The dashboard's Paste box against real Nemotron: a pasted chat is read from the user's side (PASTE_NOTE).
Without the note, "Aina: can you send me the slides?" became "Get slides from Adam" in 2 of 3 runs.
Run: pytest -m live -k paste_live"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session

from app import engine as eng
from app.config import settings
from app.db import LoopKind
from app.extraction import PASTE_NOTE
from app.llm import LLMClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not settings.nebius_api_key.get_secret_value(), reason="NEBIUS_API_KEY not set"),
]

TUESDAY = datetime(2026, 10, 20, 21, 30, tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
THURSDAY = date(2026, 10, 22)


@pytest.mark.parametrize("text", [
    "[20/10, 9:14 pm] Aina: Adam, can you send me the slides by Thursday? Also the RM30 for the dinner, thanks!",
    "[20/10, 9:00 am] Aina: can you send the slides by Thursday?\n[20/10, 9:01 am] Adam: ok will do",
])
def test_a_pasted_request_is_the_users_to_do(session: Session, text: str) -> None:
    made = eng.process_message(session, LLMClient(settings), text, now=TUESDAY, note=PASTE_NOTE).created_loops
    seen = [(l.title, l.kind.value, l.waiting_on, l.due) for l in made]
    assert not any(l.kind == LoopKind.waiting for l in made), seen  # nobody owes the user slides
    assert any("slides" in l.title.lower() and l.due == THURSDAY for l in made), seen
