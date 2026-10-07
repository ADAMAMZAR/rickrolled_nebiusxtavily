"""ScamGraph investigations (Phase 4): pipeline, risk rules and graph. Loops stay in engine.py.

Investigations commit after every step so the page can show progress. A failed run is marked
`failed` and never gets a risk level from partial data.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from app.config import settings
from app.connectors.tavily import TavilyClient, TavilyError
from app.db import (
    Claim, ClaimCategory, Entity, EntityType, Evidence, Investigation, InvestigationStatus, RiskSignal, utcnow,
)
from app.engine import InvalidRequest, NotFound
from app.extraction import Completer, ExtractionError, extract_scam, read_screenshot
from app.llm import LLMError

log = logging.getLogger("continuum.investigation")

MAX_TEXT = 8000
MAX_URL = 2000
MAX_SCREENSHOT = 5 * 1024 * 1024
MAX_PAGE = 4000  # characters of a linked page that go to the model
# Signal weights: source plan §11.1 (spec §7.4). Heuristics for explainable output, not probabilities.
WEIGHTS = {
    "regulatory_warning": 40,
    "confirmed_impersonation": 35,
    "false_regulatory_claim": 30,
    "official_domain_mismatch": 25,
    "contact_mismatch": 15,
    "payment_pressure": 15,
    "guaranteed_returns": 15,
    "official_identity_confirmed": -25,
    "verified_domain": -20,
    "verified_regulatory_status": -20,
}
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


# --- pipeline (spec §6) ---


def run_investigation(
    db: Engine, investigation_id: UUID, get_llm: Callable[[], Completer], get_web: Callable[[], TavilyClient]
) -> None:
    """Run every step for one investigation. Runs in the background; the page polls the steps.
    A model failure marks it failed with a clear error. Anything unexpected does too, then re-raises."""
    with Session(db) as session:
        investigation = session.get(Investigation, investigation_id)
        if investigation is None:
            raise NotFound(f"Investigation {investigation_id} not found.")
        try:
            llm = get_llm()
            text = _intake(session, investigation, llm, get_web)
            _extract(session, investigation, llm, text)
            investigation.status = InvestigationStatus.done
            _step(session, investigation, "Done")
        except (ExtractionError, LLMError, InvalidRequest) as e:
            _fail(session, investigation_id, str(e))
            log.warning("investigation_failed id=%s error=%s", investigation_id, type(e).__name__)
            return
        except Exception:
            _fail(session, investigation_id, "Something went wrong. Try again.")
            raise
    log.info("investigation_done id=%s", investigation_id)


def _step(session: Session, investigation: Investigation, text: str) -> None:
    add_step(investigation, text)
    session.add(investigation)
    session.commit()


def _fail(session: Session, investigation_id: UUID, error: str) -> None:
    """Throw away the failed step's unsaved rows, then mark the run failed. It never gets a risk level."""
    session.rollback()
    investigation = session.get(Investigation, investigation_id)
    if investigation is None:
        return
    investigation.status, investigation.error = InvestigationStatus.failed, error
    _step(session, investigation, f"Stopped: {error}")


def _intake(session: Session, investigation: Investigation, llm: Completer, get_web: Callable[[], TavilyClient]) -> str:
    """Turn the screenshot and the link into text next to the pasted message. Returns everything the model reads."""
    parts = [investigation.input_text] if investigation.input_text else []
    if investigation.screenshot_path:
        _step(session, investigation, "Reading the screenshot")
        path = Path(investigation.screenshot_path)
        seen = read_screenshot(llm, path.read_bytes(), path.suffix.lstrip("."), settings.nebius_vision_model)
        if seen:
            parts.append(f"[Text in the screenshot]\n{seen}")
    if investigation.input_url:
        _step(session, investigation, "Reading the link through Tavily")
        try:
            pages = get_web().extract([investigation.input_url])
        except TavilyError as e:
            pages = []
            log.warning("link_unreadable id=%s error=%s", investigation.id, e)
            _step(session, investigation, f"Couldn't read the link: {e}")
        page = next((p.raw_content.strip() for p in pages if p.raw_content.strip()), "")
        if page:
            parts.append(f"[Text on the linked page]\n{page[:MAX_PAGE]}")
        elif pages:
            _step(session, investigation, "The link had no readable text")
    text = "\n\n".join(parts).strip()
    if not text:
        raise InvalidRequest("There was no text to investigate.")
    investigation.input_text = text
    return text


def _extract(session: Session, investigation: Investigation, llm: Completer, text: str) -> None:
    """Entities, claims and pressure tactics. An entity the text doesn't contain, or a quote that isn't in it,
    is dropped: the model may only report what's there."""
    _step(session, investigation, "Reading the message with Nemotron")
    found = extract_scam(llm, text)
    entities: dict[tuple[EntityType, str], Entity] = {}

    def add(kind: EntityType, value: str) -> None:
        key = (kind, canonical(kind, value))
        if key[1] and key not in entities:
            entities[key] = Entity(investigation_id=investigation.id, type=kind, value=value.strip(), canonical=key[1])

    if investigation.input_url:  # the submitted link is a fact, not a model reading
        add(EntityType.url, investigation.input_url)
        add(EntityType.domain, investigation.input_url)
    for e in found.entities:
        if not appears(e.value, text, phone=e.type == "phone"):
            log.info("entity_dropped type=%s", e.type)
            continue
        add(EntityType(e.type), e.value)
        if e.type == "url":
            add(EntityType.domain, e.value)
    claims = [Claim(investigation_id=investigation.id, text=c.text, category=ClaimCategory(c.category)) for c in found.claims]
    signals: list[RiskSignal] = []
    for b in found.behaviours:
        if not appears(b.quote, text):
            log.info("behaviour_dropped kind=%s", b.kind)
            continue
        if b.kind not in {s.kind for s in signals}:  # one signal per kind
            signals.append(RiskSignal(
                investigation_id=investigation.id, kind=b.kind, weight=WEIGHTS[b.kind], input_quote=b.quote,
                reason="Pressure to pay now" if b.kind == "payment_pressure" else "Promises guaranteed returns",
            ))
    session.add_all([*entities.values(), *claims, *signals])
    _step(session, investigation, f"Found {_count(len(entities), 'name or contact', 'names and contacts')}, "
                                  f"{_count(len(claims), 'claim', 'claims')}, "
                                  f"{_count(len(signals), 'pressure tactic', 'pressure tactics')}")


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _squash(text: str) -> str:
    return " ".join(text.split()).casefold()


def appears(needle: str, text: str, phone: bool = False) -> bool:
    """Is `needle` in `text`, ignoring case and spacing? Phones compare digits only."""
    if phone:
        digits = re.sub(r"\D", "", needle)
        return len(digits) >= 6 and digits in re.sub(r"\D", "", text)
    return bool(_squash(needle)) and _squash(needle) in _squash(text)


def canonical(kind: EntityType, value: str) -> str:
    """The form used to compare entities: host without www., digits-only phone, lowercased email or name."""
    value = value.strip()
    if kind == EntityType.domain:
        host = urlparse(value if "//" in value else f"//{value}").hostname or ""
        return host.removeprefix("www.")
    if kind == EntityType.phone:
        return re.sub(r"\D", "", value)
    if kind == EntityType.url:
        return value
    return _squash(value)
