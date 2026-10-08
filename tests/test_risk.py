"""Spec §7 rules: verdicts, signals, score, level, confidence, identity. Plain code, no model."""

from uuid import UUID, uuid4

import pytest

from app import investigation as inv
from app.db import (
    Claim, ClaimCategory, Confidence, Direction, Entity, EntityType, Evidence, Identity, RiskLevel, RiskSignal, Tier,
    Verdict,
)

INV = uuid4()


def entity(kind: EntityType, value: str) -> Entity:
    return Entity(investigation_id=INV, type=kind, value=value, canonical=inv.canonical(kind, value))


def claim(text: str, category: ClaimCategory, verdict: Verdict = Verdict.insufficient) -> Claim:
    return Claim(investigation_id=INV, text=text, category=category, verdict=verdict)


def ev(tier: Tier, direction: Direction, quote: str, *, about: Entity | Claim | None = None, host: str = "example.com",
       official: tuple[EntityType, str] | None = None) -> Evidence:
    link = {} if about is None else {"claim_id": about.id} if isinstance(about, Claim) else {"entity_id": about.id}
    extra = {"official_type": official[0], "official_value": official[1]} if official else {}
    return Evidence(investigation_id=INV, url=f"https://{host}/p", host=host, tier=tier, direction=direction,
                    quote=quote, **link, **extra)


def behaviour(kind: str) -> RiskSignal:
    return RiskSignal(investigation_id=INV, kind=kind, weight=inv.WEIGHTS[kind], input_quote="transfer today")


ORG = entity(EntityType.org, "T. Rowe Price Group Sdn. Bhd.")
SC = entity(EntityType.org, "Securities Commission")
DOMAIN = entity(EntityType.domain, "troweprice-my-invest.com")
PHONE = entity(EntityType.phone, "+60 12-000 5678")
LICENSED = claim("The fund is approved by the Securities Commission.", ClaimCategory.regulatory)
RETURNS = claim("The fund pays a fixed 12% monthly return.", ClaimCategory.investment)
ENTITIES = [ORG, SC, DOMAIN, PHONE]


def kinds(signals: list[RiskSignal]) -> set[str]:
    return {s.kind for s in signals}


# --- §7.3 verdicts ---


@pytest.mark.parametrize(
    ("rows", "behaviours", "verdict"),
    [
        ([(Tier.a, Direction.warns)], set(), Verdict.contradicted),
        ([(Tier.b, Direction.contradicts)], set(), Verdict.contradicted),
        ([(Tier.a, Direction.supports), (Tier.b, Direction.contradicts)], set(), Verdict.contradicted),  # against wins
        ([(Tier.b, Direction.supports)], set(), Verdict.supported),
        ([(Tier.c, Direction.warns), (Tier.e, Direction.supports)], set(), Verdict.unverified),  # weak only
        ([], set(), Verdict.insufficient),
        ([(Tier.a, Direction.neutral)], set(), Verdict.insufficient),
        ([], {"guaranteed_returns"}, Verdict.suspicious),
        ([(Tier.d, Direction.supports)], {"guaranteed_returns"}, Verdict.suspicious),
        ([(Tier.a, Direction.supports)], {"guaranteed_returns"}, Verdict.supported),
        ([], {"payment_pressure"}, Verdict.insufficient),  # wrong kind for an investment claim
    ],
)
def test_claim_verdicts(rows: list[tuple[Tier, Direction]], behaviours: set[str], verdict: Verdict) -> None:
    evidence = [ev(t, d, "q", about=RETURNS, host=f"site{i}.com") for i, (t, d) in enumerate(rows)]
    assert inv.claim_verdicts([RETURNS], evidence, behaviours)[RETURNS.id][0] == verdict


# --- §7.4 signals ---


def test_regulatory_warning_needs_a_tier_a_page_naming_it() -> None:
    warns = ev(Tier.a, Direction.warns, "T Rowe Price Group Sdn.Bhd (potential clone entity)", about=ORG, host="bnm.gov.my")
    signals = inv.derive_signals(INV, ENTITIES, [], [warns])
    assert kinds(signals) == {"regulatory_warning"} and signals[0].evidence_id == warns.id and signals[0].weight == 40
    generic = ev(Tier.a, Direction.warns, "The Securities Commission keeps this list", about=ORG, host="sc.com.my")
    news = ev(Tier.c, Direction.warns, "T Rowe Price Group Sdn.Bhd is a clone", about=ORG, host="thestar.com.my")
    assert inv.derive_signals(INV, ENTITIES, [], [generic, news]) == []


def test_regulatory_claim_contradicted_or_confirmed_by_tier_a() -> None:
    against = ev(Tier.a, Direction.contradicts, "T Rowe Price Group Sdn.Bhd is not licensed", about=LICENSED, host="sc.com.my")
    assert kinds(inv.derive_signals(INV, ENTITIES, [LICENSED], [against])) == {"false_regulatory_claim"}
    support = ev(Tier.a, Direction.supports, "T Rowe Price Group Sdn.Bhd holds a licence", about=LICENSED, host="sc.com.my")
    assert kinds(inv.derive_signals(INV, ENTITIES, [LICENSED], [support])) == {"verified_regulatory_status"}
    weak = ev(Tier.b, Direction.contradicts, "T Rowe Price is not licensed here", about=LICENSED, host="troweprice.com")
    assert inv.derive_signals(INV, ENTITIES, [LICENSED], [weak]) == []  # tier A only


def official(value: str, kind: EntityType = EntityType.domain, tier: Tier = Tier.d, host: str = "wikipedia.org") -> Evidence:
    return ev(tier, Direction.supports, f"Website {value}", about=ORG, host=host, official=(kind, inv.canonical(kind, value)))


def test_domain_mismatch_and_impersonation() -> None:
    site = official("troweprice.com")
    signals = inv.derive_signals(INV, ENTITIES, [], [site])
    assert kinds(signals) == {"official_domain_mismatch"} and signals[0].evidence_id == site.id
    assert "troweprice-my-invest.com isn't T. Rowe Price Group Sdn. Bhd.'s official website (troweprice.com)" == signals[0].reason
    warning = ev(Tier.a, Direction.warns, "Beware of troweprice-my-invest.com", about=DOMAIN, host="sc.com.my")
    assert kinds(inv.derive_signals(INV, ENTITIES, [], [site, warning])) == {
        "official_domain_mismatch", "confirmed_impersonation", "regulatory_warning",
    }


def test_domain_matches_official_or_is_confirmed_by_the_official_site() -> None:
    sub = entity(EntityType.domain, "invest.troweprice.com")
    assert kinds(inv.derive_signals(INV, [ORG, sub], [], [official("troweprice.com")])) == {"verified_domain"}
    other = entity(EntityType.domain, "maybank2u.com.my")  # a second official domain, named on the official site
    confirmed = ev(Tier.b, Direction.supports, "Log in at maybank2u.com.my", about=other, host="maybank.com")
    # The domain checks out, but nothing strong (A/B) is about the organisation itself: identity stays unconfirmed.
    assert kinds(inv.derive_signals(INV, [ORG, other], [], [official("maybank.com"), confirmed])) == {"verified_domain"}


def test_contact_mismatch_only_against_the_official_site() -> None:
    on_site = official("+1 410-345-2000", EntityType.phone, Tier.b, "troweprice.com")
    assert kinds(inv.derive_signals(INV, [ORG, PHONE], [], [on_site])) == {"contact_mismatch"}
    broker = official("+1 410-345-2000", EntityType.phone, Tier.d, "leadiq.com")  # data broker: ignored
    assert inv.derive_signals(INV, [ORG, PHONE], [], [broker]) == []
    same = official("012-000 5678", EntityType.phone, Tier.b, "troweprice.com")  # +60 vs 0: same number
    assert kinds(inv.derive_signals(INV, [ORG, PHONE], [], [same])) == {"official_identity_confirmed"}


def test_identity_confirmed_needs_every_contact_to_match() -> None:
    maybank = entity(EntityType.org, "Maybank")
    domain = entity(EntityType.domain, "maybank2u.com.my")
    phone = entity(EntityType.phone, "1-300-88-6688")
    evidence = [official("maybank2u.com.my", host="wikipedia.org"),
                ev(Tier.b, Direction.supports, "Hotline 1 300 88 6688", about=maybank, host="maybank2u.com.my",
                   official=(EntityType.phone, "1300886688"))]
    evidence[0].entity_id = maybank.id
    signals = inv.derive_signals(INV, [maybank, domain, phone], [], evidence)
    assert kinds(signals) == {"verified_domain", "official_identity_confirmed"}
    assert sum(s.weight for s in signals) == -45
    wrong = entity(EntityType.phone, "+60 11-0000 1234")
    assert "official_identity_confirmed" not in kinds(inv.derive_signals(INV, [maybank, domain, phone, wrong], [], evidence))


def test_no_official_contacts_no_identity_signals() -> None:
    assert inv.derive_signals(INV, ENTITIES, [], []) == []


# --- §7.5 level, confidence, identity ---


def signal(kind: str, evidence: Evidence | None = None) -> RiskSignal:
    return RiskSignal(investigation_id=INV, kind=kind, weight=inv.WEIGHTS[kind], evidence_id=evidence.id if evidence else None,
                      input_quote=None if evidence else "quote")


A_WARN = ev(Tier.a, Direction.warns, "q", about=ORG, host="bnm.gov.my")
D_PAGE = ev(Tier.d, Direction.supports, "q", about=ORG, host="blog.com")


@pytest.mark.parametrize(
    ("signals", "evidence", "score", "level"),
    [
        ([signal("regulatory_warning", A_WARN), signal("guaranteed_returns"), signal("payment_pressure")], [A_WARN], 70, RiskLevel.high),
        ([signal("regulatory_warning", A_WARN), signal("false_regulatory_claim", A_WARN), signal("payment_pressure")], [A_WARN], 85, RiskLevel.critical),
        ([signal("regulatory_warning", A_WARN)], [A_WARN], 40, RiskLevel.elevated),
        ([signal("payment_pressure")], [], 15, RiskLevel.guarded),
        ([signal("payment_pressure"), signal("guaranteed_returns")], [], 30, RiskLevel.elevated),
        # 55 without a strong source: capped at ELEVATED
        ([signal("official_domain_mismatch", D_PAGE), signal("payment_pressure"), signal("guaranteed_returns")], [D_PAGE], 55, RiskLevel.elevated),
        ([], [], 0, RiskLevel.insufficient),  # nothing found is not safety
        ([], [D_PAGE], 0, RiskLevel.insufficient),
        ([signal("verified_domain", A_WARN)], [A_WARN], -20, RiskLevel.low),
    ],
)
def test_levels(signals: list[RiskSignal], evidence: list[Evidence], score: int, level: RiskLevel) -> None:
    assert inv.assess(signals, evidence)[:2] == (score, level)


def test_confidence() -> None:
    def conf(*rows: Evidence) -> Confidence:
        return inv.assess([], list(rows))[2]

    c1 = ev(Tier.c, Direction.warns, "q", about=ORG, host="thestar.com.my")
    c2 = ev(Tier.c, Direction.warns, "q", about=ORG, host="malaymail.com")
    assert conf(A_WARN) == Confidence.high
    assert conf(c1, c2) == Confidence.high
    assert conf(c1) == Confidence.medium
    assert conf(D_PAGE, ev(Tier.e, Direction.warns, "q", host="reddit.com")) == Confidence.low
    assert conf() == Confidence.low
    agree = ev(Tier.b, Direction.supports, "q", about=ORG, host="troweprice.com")
    assert conf(A_WARN, agree) == Confidence.medium  # strong sources disagree about the org: one level down


def test_identity() -> None:
    def identity(*names: str) -> Identity:
        return inv.assess([signal(n, A_WARN) for n in names], [A_WARN])[3]

    assert identity("official_domain_mismatch") == Identity.mismatch
    assert identity("contact_mismatch", "official_identity_confirmed") == Identity.mismatch
    assert identity("official_identity_confirmed") == Identity.verified
    assert identity("regulatory_warning") == Identity.unverified


def test_every_derived_signal_cites_evidence() -> None:
    evidence = [official("troweprice.com"), ev(Tier.a, Direction.warns, "Beware of troweprice-my-invest.com", about=DOMAIN, host="sc.com.my"),
                ev(Tier.a, Direction.contradicts, "T Rowe Price Group Sdn.Bhd: not licensed", about=LICENSED, host="sc.com.my"),
                official("+1 410-345-2000", EntityType.phone, Tier.b, "troweprice.com")]
    signals = inv.derive_signals(INV, ENTITIES, [LICENSED], evidence)
    assert len(signals) == 5 and all(isinstance(s.evidence_id, UUID) for s in signals)
    assert {s.evidence_id for s in signals} <= {e.id for e in evidence}
