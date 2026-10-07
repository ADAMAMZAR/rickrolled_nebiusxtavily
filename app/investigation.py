"""ScamGraph investigations (Phase 4): pipeline, risk rules and graph. Loops stay in engine.py.

Investigations commit after every step so the page can show progress. A failed run is marked
`failed` and never gets a risk level from partial data.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlmodel import Session, col, select

from app.config import settings
from app.db import Claim, Entity, Evidence, Investigation, RiskSignal, utcnow
from app.engine import InvalidRequest, NotFound

log = logging.getLogger("continuum.investigation")

MAX_TEXT = 8000
MAX_URL = 2000
MAX_SCREENSHOT = 5 * 1024 * 1024
# Checked by file signature, not by the name or content type the browser sends.
IMAGE_TYPES = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg"}


@dataclass
class InvestigationDetail:
    investigation: Investigation
    entities: list[Entity]
    claims: list[Claim]
    evidence: list[Evidence]
    signals: list[RiskSignal]


def image_ext(data: bytes) -> str | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return next((ext for magic, ext in IMAGE_TYPES.items() if data.startswith(magic)), None)


def create_investigation(
    session: Session, text: str | None = None, url: str | None = None, screenshot: bytes | None = None
) -> Investigation:
    """Validate the input and save a running investigation. At least one input is needed."""
    text = (text or "").strip()
    url = (url or "").strip() or None
    if not (text or url or screenshot):
        raise InvalidRequest("Paste a message, a link or a screenshot.")
    if len(text) > MAX_TEXT:
        raise InvalidRequest(f"Keep the message under {MAX_TEXT} characters.")
    if url is not None and (len(url) > MAX_URL or not url.lower().startswith(("http://", "https://"))):
        raise InvalidRequest("The link must start with http:// or https://.")
    ext = None
    if screenshot is not None:
        if len(screenshot) > MAX_SCREENSHOT:
            raise InvalidRequest("The screenshot must be under 5 MB.")
        ext = image_ext(screenshot)
        if ext is None:
            raise InvalidRequest("The screenshot must be a PNG, JPEG or WebP image.")

    investigation = Investigation(input_text=text, input_url=url)
    if screenshot is not None:
        folder = Path(settings.uploads_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{investigation.id}.{ext}"
        path.write_bytes(screenshot)
        investigation.screenshot_path = str(path)
    add_step(investigation, "Received")
    session.add(investigation)
    try:
        session.commit()
    except Exception:
        if investigation.screenshot_path:
            Path(investigation.screenshot_path).unlink(missing_ok=True)
        raise
    session.refresh(investigation)
    log.info("investigation_created id=%s text=%s url=%s screenshot=%s", investigation.id, bool(text), bool(url), bool(ext))
    return investigation


def add_step(investigation: Investigation, text: str) -> None:
    """Append a progress line. Reassigned, not appended in place, so SQLAlchemy sees the JSON change."""
    investigation.steps = [*investigation.steps, {"at": utcnow().isoformat(), "text": text}]
    investigation.updated_at = utcnow()


def get_investigation(session: Session, investigation_id: UUID) -> InvestigationDetail:
    investigation = session.get(Investigation, investigation_id)
    if investigation is None:
        raise NotFound(f"Investigation {investigation_id} not found.")

    def rows(model: type) -> list:  # type: ignore[type-arg]
        return list(session.exec(select(model).where(model.investigation_id == investigation_id)).all())

    return InvestigationDetail(investigation, rows(Entity), rows(Claim), rows(Evidence), rows(RiskSignal))


def list_investigations(session: Session, limit: int = 50) -> list[Investigation]:
    query = select(Investigation).order_by(col(Investigation.created_at).desc()).limit(limit)
    return list(session.exec(query).all())
