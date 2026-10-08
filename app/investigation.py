"""ScamGraph investigations (Phase 4): pipeline, risk rules and graph. Loops stay in engine.py.

Investigations commit after every step so the page can show progress. A failed run is marked
`failed` and never gets a risk level from partial data.
"""

import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from app.config import settings
from app.connectors.tavily import TavilyClient, TavilyError, WebResult
from app.db import (
    Claim, ClaimCategory, Direction, Entity, EntityType, Evidence, Investigation, InvestigationStatus, RiskSignal,
    Tier, utcnow,
)
from app.engine import InvalidRequest, NotFound
from app.extraction import (
    Completer, ExtractionError, PageFindings, classify_page, extract_scam, plan_searches, read_screenshot,
)
from app.llm import LLMError

log = logging.getLogger("continuum.investigation")

MAX_TEXT = 8000
MAX_URL = 2000
MAX_SCREENSHOT = 5 * 1024 * 1024
MAX_PAGE = 4000  # characters of a linked page that go to the model
# Research budget per round (spec §6, §0.6)
MAX_SEARCHES = 8
MAX_PAGES = 5
MAX_FIXED_ORGS = 2  # organisations that get the fixed regulator searches
RESEARCH_SECONDS = 60
MAX_CHECKED = 6000  # characters of each page that go to the model
# Organisations that are regulators themselves: no alert-list search for them.
# ponytail: a name list; extend it if other regulators show up as "orgs".
REGULATOR_NAMES = ("securities commission", "suruhanjaya sekuriti", "bank negara", "companies commission",
                   "suruhanjaya syarikat", "polis diraja", "royal malaysia police")
SUFFIXES = re.compile(r"\b(sdn\.?|bhd\.?|berhad|group|inc\.?|ltd\.?|limited|plc|llc)(?=\W|$)", re.IGNORECASE)
# Source tiers by site (spec §7.1). B is the org's official domain, found during research; D is everything else.
TIER_SITES = {
    Tier.a: ["gov.my", "sc.com.my", "ssm.com.my", "gov"],
    Tier.c: ["thestar.com.my", "malaymail.com", "nst.com.my", "freemalaysiatoday.com", "theedgemalaysia.com",
             "bernama.com", "reuters.com", "bbc.com", "bbc.co.uk"],
    Tier.e: ["facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com", "reddit.com", "youtube.com",
             "quora.com", "lowyat.net", "t.me", "linkedin.com"],
}
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
            _research(session, investigation, llm, get_web)
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


@dataclass
class Search:
    label: str  # shown as a step
    query: str
    include_domains: list[str]


def _research(session: Session, investigation: Investigation, llm: Completer, get_web: Callable[[], TavilyClient]) -> None:
    """Plan searches, run them, read the best pages and check each against the claims (spec §6 steps 3-5).
    Searches, page reads and page checks share one RESEARCH_SECONDS deadline."""
    detail = get_investigation(session, investigation.id)
    if not (detail.entities or detail.claims):
        _step(session, investigation, "Nothing to look up on the web")
        return
    try:
        web = get_web()
    except TavilyError as e:
        _step(session, investigation, f"Skipped web research: {e}")
        return
    ids = {f"e{i}": e for i, e in enumerate(detail.entities, 1)} | {f"c{i}": c for i, c in enumerate(detail.claims, 1)}
    subjects = [{"id": k, "type": v.type.value, "value": v.value} for k, v in ids.items() if isinstance(v, Entity)]
    claims = [{"id": k, "text": v.text} for k, v in ids.items() if isinstance(v, Claim)]

    searches = _fixed_searches(detail.entities)
    _step(session, investigation, "Planning the research with Nemotron")
    try:
        planned = plan_searches(llm, subjects, claims, MAX_SEARCHES - len(searches))
    except (ExtractionError, LLMError) as e:
        log.warning("plan_failed id=%s error=%s", investigation.id, type(e).__name__)
        planned = []
        _step(session, investigation, "Couldn't plan extra searches; using the regulator searches only")
    searches += [Search(f"Searching the web: {p.query}", p.query, p.include_domains) for p in planned]
    deadline = time.monotonic() + RESEARCH_SECONDS

    for search in searches:
        add_step(investigation, search.label)
    session.commit()
    results = _run_searches(web, searches, deadline)
    failed = sum(1 for r in results if r is None)
    own = {e.canonical for e in detail.entities if e.type == EntityType.domain}  # the message's own sites prove nothing

    def best_first(found: list[WebResult]) -> list[WebResult]:
        usable = [r for r in found if host_of(r.url) and not any(same_site(host_of(r.url), d) for d in own)]
        return sorted(usable, key=lambda r: (tier_of(host_of(r.url)), -r.score))

    ranked = {r.url: r for r in best_first([r for rs in results for r in rs or []])}
    # Each search's best page first (alert lists, then the official website, then the planned searches),
    # so the regulators' many pages can't crowd out the company's own site. Then the rest by rank.
    picked: dict[str, WebResult] = {}
    for found in results:
        top = next((r for r in best_first(found or []) if r.url not in picked), None)
        if top and len(picked) < MAX_PAGES:
            picked[top.url] = top
    for url, found in ranked.items():
        if len(picked) < MAX_PAGES:
            picked.setdefault(url, found)
    pages = list(picked.values())
    _step(session, investigation, f"{_count(len(ranked), 'source', 'sources')} found"
                                  + (f" ({_count(failed, 'search', 'searches')} failed)" if failed else "")
                                  + (f", reading the top {len(pages)}" if pages else ""))
    if not pages:
        return
    try:
        read = web.extract([p.url for p in pages], cached=True)
    except TavilyError as e:
        _step(session, investigation, f"Couldn't read the pages: {e}")
        return
    texts = {p.url: p.raw_content for p in read if p.raw_content.strip()}
    pages = [p for p in pages if p.url in texts]
    if not pages:
        _step(session, investigation, "None of the pages had readable text")
        return

    _step(session, investigation, f"Checking {_count(len(pages), 'page', 'pages')} against the claims with Nemotron")
    checked, late = _classify(llm, pages, texts, subjects, claims, deadline)
    evidence = []
    for page, findings in checked:
        evidence += _evidence(investigation.id, page, texts[page.url], findings, ids)
    _set_official_tier(evidence)
    session.add_all(evidence)
    regulators = sum(1 for e in evidence if e.tier == Tier.a)
    _step(session, investigation, f"{_count(len(evidence), 'piece of evidence', 'pieces of evidence')} from "
                                  f"{_count(len({e.url for e in evidence}), 'page', 'pages')} "
                                  f"({regulators} from regulators)"
                                  + (f"; ran out of time for {_count(late, 'page', 'pages')}" if late else ""))


def _fixed_searches(entities: list[Entity]) -> list[Search]:
    """Regulator alert lists and the official website for each organisation, always run, so the key finding
    doesn't depend on what the model chooses to search."""
    searches = []
    orgs = [e for e in entities if e.type == EntityType.org and not is_regulator(e)]
    for org in orgs[:MAX_FIXED_ORGS]:
        searches += [
            Search(f"Checking SC's Investor Alert List for {org.value}", org.value, ["sc.com.my"]),
            Search(f"Checking Bank Negara's alert list for {org.value}", org.value, ["bnm.gov.my"]),
            Search(f"Finding {org.value}'s official website", f"{org.value} official website", []),
        ]
    return searches


def _run_searches(web: TavilyClient, searches: list[Search], deadline: float) -> list[list[WebResult] | None]:
    """All searches in parallel. Results in search order; None for a failed or late search."""
    pool = ThreadPoolExecutor(max_workers=len(searches) or 1)
    futures = [
        pool.submit(web.search, s.query, max_results=5, include_domains=s.include_domains or None, cached=True)
        for s in searches
    ]
    done, _ = wait(futures, timeout=max(0, deadline - time.monotonic()))
    pool.shutdown(wait=False, cancel_futures=True)
    results: list[list[WebResult] | None] = []
    for future in futures:
        try:
            results.append(future.result().results if future in done else None)
        except TavilyError as e:
            log.warning("search_failed error=%s", e)
            results.append(None)
    return results


def _classify(
    llm: Completer, pages: list[WebResult], texts: dict[str, str], subjects: list[dict[str, str]],
    claims: list[dict[str, str]], deadline: float,
) -> tuple[list[tuple[WebResult, PageFindings]], int]:
    """One Nemotron call per page, in parallel. Returns the pages checked in time, and how many were late.
    A page whose check fails is skipped."""
    needles = [s["value"] for s in subjects]
    pool = ThreadPoolExecutor(max_workers=len(pages))
    futures = {
        pool.submit(classify_page, llm, p.url, p.title, relevant_text(texts[p.url], needles), subjects, claims): p
        for p in pages
    }
    done, late = wait(futures, timeout=max(0, deadline - time.monotonic()))
    pool.shutdown(wait=False, cancel_futures=True)  # late checks finish in the background and are ignored
    checked = []
    for future in done:
        try:
            checked.append((futures[future], future.result()))
        except (ExtractionError, LLMError) as e:
            log.warning("page_check_failed error=%s", type(e).__name__)
    checked.sort(key=lambda pf: pages.index(pf[0]))
    return checked, len(late)


def _evidence(
    investigation_id: UUID, page: WebResult, text: str, findings: PageFindings, ids: dict[str, Entity | Claim]
) -> list[Evidence]:
    """Turn one page's findings into evidence rows. A quote not on the page, an unknown id, or an official
    contact not in its quote is dropped (spec §2: the model may only cite what's there)."""
    host = host_of(page.url)
    tier = tier_of(host)
    base = {"investigation_id": investigation_id, "url": page.url, "title": page.title, "host": host, "tier": tier}
    rows = []
    entities = [v for v in ids.values() if isinstance(v, Entity)]
    for item in findings.evidence:
        target = ids.get(item.about)
        # The quote must be on the page and name something from the message: a page about someone else
        # (or a generic disclaimer) says nothing about this sender.
        if target is None or not appears(item.quote, text) or not names_any(item.quote, entities):
            log.info("evidence_dropped known=%s", target is not None)
            continue
        link = {"claim_id": target.id} if isinstance(target, Claim) else {"entity_id": target.id}
        rows.append(Evidence(**base, **link, direction=Direction(item.direction), quote=item.quote))
    for item in findings.official:
        org = ids.get(item.org)
        kind = EntityType(item.type)
        value = canonical(kind, item.value)
        # Official contacts come only from ordinary sites (tier D, where the org's own site is) and are never a
        # regulator, news or social domain: those pages list their own contacts (e.g. Bank Negara's hotline on SSM).
        ok = (
            isinstance(org, Entity) and org.type == EntityType.org and tier == Tier.d and value
            and appears(item.quote, text) and appears(value, item.quote, phone=kind == EntityType.phone)
            and (kind != EntityType.domain or tier_of(value) == Tier.d)
        )
        if not ok:
            log.info("official_dropped type=%s", item.type)
            continue
        rows.append(Evidence(
            **base, entity_id=org.id, direction=Direction.supports, quote=item.quote,  # type: ignore[union-attr]
            official_type=kind, official_value=value,
        ))
    return rows


def _set_official_tier(evidence: list[Evidence]) -> None:
    """Pages on an organisation's stated official domain are tier B (spec §7.1). Regulators stay A."""
    official = {e.official_value for e in evidence if e.official_type == EntityType.domain and e.official_value}
    for e in evidence:
        if e.tier != Tier.a and any(same_site(e.host, d) for d in official):
            e.tier = Tier.b


def relevant_text(text: str, needles: list[str]) -> str:
    """The start of a page plus the passages around each name, up to MAX_CHECKED characters. Alert lists are
    long tables, and the row that matters is usually far down."""
    keep = [(0, min(len(text), 1500))]
    # Match on letters and digits only, so "T. Rowe Price Group Sdn. Bhd." finds "T Rowe Price Group Sdn.Bhd",
    # and also without company suffixes, so it finds "T Rowe Price Group" too.
    flat, where = "", []
    for i, ch in enumerate(text.casefold()):
        if ch.isalnum():
            flat += ch
            where.append(i)
    forms = {_alnum(n) for n in needles} | {_alnum(SUFFIXES.sub("", n)) for n in needles}
    for form in {f for f in forms if len(f) >= 5}:
        start = flat.find(form)
        while start != -1 and len(keep) < 40:
            keep.append((max(0, where[start] - 400), min(len(text), where[start + len(form) - 1] + 400)))
            start = flat.find(form, start + 1)
    merged: list[list[int]] = []
    for start, end in sorted(keep):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return "\n…\n".join(text[s:e] for s, e in merged)[:MAX_CHECKED]


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def same_site(host: str, domain: str) -> bool:
    """`host` is `domain` or one of its subdomains. A look-alike (abc-domain.com) isn't."""
    return host == domain or host.endswith(f".{domain}")


def tier_of(host: str) -> Tier:
    """Source tier by hostname (spec §7.1). B (the official domain) is set after the pages are checked."""
    for tier, sites in TIER_SITES.items():
        if any(same_site(host, site) for site in sites):
            return tier
    return Tier.d


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _squash(text: str) -> str:
    return " ".join(text.split()).casefold()


def appears(needle: str, text: str, phone: bool = False) -> bool:
    """Is `needle` in `text`, comparing letters and digits only? Pages write the same name differently
    ("Sdn. Bhd." vs "Sdn.Bhd", table pipes). Phones compare their last 9 digits (at least 6), so
    "+60 12-000 5678" matches "012-000 5678"."""
    if phone:
        # ponytail: last-9-digits match covers +60 vs 0 prefixes; use a phone library if other formats appear.
        digits = re.sub(r"\D", "", needle)[-9:]
        return len(digits) >= 6 and digits in re.sub(r"\D", "", text)
    return bool(_alnum(needle)) and _alnum(needle) in _alnum(text)


def is_regulator(entity: Entity) -> bool:
    return any(name in entity.canonical for name in REGULATOR_NAMES)


def names_any(quote: str, entities: list[Entity]) -> bool:
    """Does the quote name one of the message's organisations, people, domains, phones or emails?
    Organisations also match without suffixes ("T Rowe Price Group" for "T. Rowe Price Group Sdn. Bhd.").
    A regulator named in the message doesn't count: every page on its site names it."""
    for e in entities:
        if e.type == EntityType.org and is_regulator(e):
            continue
        if e.type == EntityType.phone:
            if appears(e.value, quote, phone=True):
                return True
            continue
        forms = {_alnum(e.value), _alnum(SUFFIXES.sub("", e.value))} if e.type in (EntityType.org, EntityType.person) \
            else {_alnum(e.canonical)}
        if any(len(f) >= 4 and f in _alnum(quote) for f in forms):
            return True
    return False


def _alnum(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


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
