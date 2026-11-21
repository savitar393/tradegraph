"""Generate trial OWL 2 DL Turtle from verified JSONL records and validate against the canonical KB."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Iterable

from rdflib import BNode, Graph, Literal, Namespace, OWL, RDF, RDFS, URIRef, XSD

from tradegraph.kb.loader import ENGINE_PROFILE_ID, KnowledgeBaseError, compile_kb
from tradegraph.kb.rdf import parse_turtle
from tradegraph.kb.validation import validate_owl
from tradegraph.sources.parsers import verify_records


TG = Namespace("urn:tradegraph:kb#")
CATALOG = Namespace("urn:tradegraph:catalog#")
SWRL = Namespace("http://www.w3.org/2003/11/swrl#")
SWRLB = Namespace("http://www.w3.org/2003/11/swrlb#")
SH = Namespace("http://www.w3.org/ns/shacl#")

AGREEMENT_MAP: dict[str, tuple[URIRef, str]] = {
    "ATIGA": (TG.atiga, "atiga"),
    "RCEP": (TG.rcep, "rcep"),
    "ACFTA": (TG.acfta, "acfta"),
    "IJEPA": (TG.ijepa, "ijepa"),
    "IKCEPA": (TG.ikcepa, "ikcepa"),
    "IACEPA": (TG.iacepa, "iacepa"),
}

CRITERION_KIND_MAP: dict[str, URIRef] = {
    "WO": TG.criterionWO,
    "RVC": TG.criterionRVC,
    "CC": TG.criterionCC,
    "CTH": TG.criterionCTH,
    "CTSH": TG.criterionCTSH,
    "TEXTILE_PROCESS": TG.criterionTextileProcess,
    "CUT_AND_SEWN_AMS": TG.criterionCutAndSewnAMS,
    "REFINING_PROCESS": TG.criterionRefiningProcess,
    "CHEMICAL_REACTION": TG.criterionChemicalReaction,
}

LEGAL_RELATION_PROPS: dict[str, tuple[URIRef | None, URIRef]] = {
    "AMENDS": (TG.amendsDocument, TG.relAmends),
    "REVOKES": (TG.revokesDocument, TG.relRevokes),
    "PARTIALLY_REVOKES": (TG.partiallyRevokesDocument, TG.relPartiallyRevokes),
    "RELATED": (None, TG.relRelated),
}


@dataclass(frozen=True, slots=True)
class StagingBuildSummary:
    records_processed: int
    verified_records: int
    flagged_records: int
    flagged_diagnostics: tuple[str, ...]
    triples_generated: int
    source_documents_emitted: int
    source_clauses_emitted: int
    hs_subheadings_emitted: int
    hs_codes_emitted: int
    psr_rules_emitted: int
    origin_thresholds_emitted: int
    legal_relations_emitted: int
    skipped_existing_iris: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrialValidationResult:
    kb_compiled: bool
    owl_validated: bool | None
    canonical_sha256: str
    merged_sha256: str | None
    canonical_owl_triples: int
    merged_owl_triples: int | None
    diagnostics: tuple[str, ...]


def _slugify(value: str, *, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        slug = "item"
    if len(slug) > max_len:
        suffix = sha256(value.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug[: max_len - 9].rstrip('-')}-{suffix}"
    return slug


def _normalize_reg_ref(text: str | None, *, default_kind: str = "reg") -> str | None:
    if not text:
        return None
    m = re.search(r"(\d+)\s*(?:/PMK\.\d+/|Tahun\s+)(\d{4})", text, re.I)
    if m:
        low = text.lower()
        kind = (
            "pmk" if ("pmk" in low or "keuangan" in low)
            else "permendag" if ("permendag" in low or "perdagangan" in low)
            else "bpom" if "bpom" in low
            else default_kind
        )
        return f"{kind}-{int(m.group(1))}-{m.group(2)}"
    return None


def _infer_kind_from_url(url: str | None) -> str:
    low = (url or "").lower()
    if "kemendag" in low or "permendag" in low:
        return "permendag"
    if "kemenkeu" in low or "pmk" in low:
        return "pmk"
    if "bpom" in low:
        return "bpom"
    return "reg"


def _load_existing_index(
    canonical_kb_path: Path | None,
) -> tuple[dict[str, URIRef], dict[str, URIRef], set[URIRef]]:
    if canonical_kb_path is None:
        return {}, {}, set()
    graph = parse_turtle(canonical_kb_path.read_bytes(), canonical_kb_path.resolve().as_uri())
    source_by_key: dict[str, URIRef] = {}
    source_by_reg_ref: dict[str, URIRef] = {}
    for subj, obj in graph.subject_objects(TG.sourceKey):
        if isinstance(subj, URIRef):
            source_by_key[str(obj)] = subj
    for subj in graph.subjects(RDF.type, TG.SourceDocument):
        if not isinstance(subj, URIRef):
            continue
        see_also = graph.value(subj, RDFS.seeAlso)
        default_kind = _infer_kind_from_url(str(see_also) if see_also is not None else None)
        for pred in (TG.regulationNumber, RDFS.label):
            for val in graph.objects(subj, pred):
                ref = _normalize_reg_ref(str(val), default_kind=default_kind)
                if ref and ref not in source_by_reg_ref:
                    source_by_reg_ref[ref] = subj
    existing_iris = {
        term for term in graph.subjects(RDF.type, OWL.NamedIndividual)
        if isinstance(term, URIRef)
    }
    return source_by_key, source_by_reg_ref, existing_iris


def _add_reified_axiom(
    graph: Graph,
    *,
    source: URIRef,
    prop: URIRef,
    target: URIRef | Literal,
    axiom_id: str,
    clause: URIRef,
    review_status: str,
) -> None:
    node = BNode()
    graph.add((node, RDF.type, OWL.Axiom))
    graph.add((node, OWL.annotatedSource, source))
    graph.add((node, OWL.annotatedProperty, prop))
    graph.add((node, OWL.annotatedTarget, target))
    graph.add((node, TG.axiomId, Literal(axiom_id)))
    graph.add((node, TG.axiomSource, clause))
    graph.add((node, TG.axiomReviewStatus, Literal(review_status)))


def _ensure_source_document(
    graph: Graph,
    *,
    source_by_key: dict[str, URIRef],
    emitted_docs: set[URIRef],
    source_key: str,
    source_id: str | None,
    title: str | None,
    url: str | None,
) -> URIRef:
    if source_key in source_by_key:
        return source_by_key[source_key]
    doc_iri = TG[f"staged-source-{_slugify(source_key)}"]
    source_by_key[source_key] = doc_iri
    if doc_iri not in emitted_docs:
        emitted_docs.add(doc_iri)
        graph.add((doc_iri, RDF.type, OWL.NamedIndividual))
        graph.add((doc_iri, RDF.type, TG.SourceDocument))
        if title:
            graph.add((doc_iri, RDFS.label, Literal(title)))
        if url:
            graph.add((doc_iri, RDFS.seeAlso, URIRef(url)))
        graph.add((doc_iri, TG.sourceKey, Literal(source_key)))
        if source_id:
            graph.add((doc_iri, TG.sourceRecordId, Literal(source_id)))
    return doc_iri


def _ensure_chapter(graph: Graph, digits2: str, existing: set[URIRef], emitted: set[URIRef]) -> URIRef:
    iri = TG[f"hs2-{digits2}"]
    if iri not in existing and iri not in emitted:
        emitted.add(iri)
        graph.add((iri, RDF.type, OWL.NamedIndividual))
        graph.add((iri, RDF.type, TG.HSChapter))
        graph.add((iri, TG.chapterCode, Literal(digits2)))
    return iri


def _ensure_heading(
    graph: Graph, digits4: str, existing: set[URIRef], emitted_headings: set[URIRef], emitted_chapters: set[URIRef],
) -> URIRef:
    chap_iri = _ensure_chapter(graph, digits4[:2], existing, emitted_chapters)
    iri = TG[f"hs4-{digits4}"]
    if iri not in existing and iri not in emitted_headings:
        emitted_headings.add(iri)
        graph.add((iri, RDF.type, OWL.NamedIndividual))
        graph.add((iri, RDF.type, TG.HSHeading))
        graph.add((iri, TG.headingCode, Literal(digits4)))
        graph.add((iri, TG.headingChapter, chap_iri))
    return iri


def _ensure_rvc_threshold(
    graph: Graph,
    *,
    decimal_pct: Decimal,
    agreement_slug: str,
    agreement_label: str,
    existing: set[URIRef],
    emitted_thresholds: set[URIRef],
    clause_iri: URIRef,
    review_status: str | None,
) -> URIRef:
    plain = format(decimal_pct, "f")
    if "." in plain:
        plain = plain.rstrip("0").rstrip(".") or "0"
    pct_slug = plain.replace(".", "p")
    thresh_iri = TG[f"{agreement_slug}-rvc-{pct_slug}-threshold"]
    if thresh_iri not in existing and thresh_iri not in emitted_thresholds:
        emitted_thresholds.add(thresh_iri)
        lexical = f"{plain}.0" if "." not in plain else plain
        lit = Literal(lexical, datatype=XSD.decimal)
        graph.add((thresh_iri, RDF.type, OWL.NamedIndividual))
        graph.add((thresh_iri, RDF.type, TG.OriginThreshold))
        graph.add((thresh_iri, RDFS.label, Literal(f"{agreement_label} RVC {plain}% threshold")))
        graph.add((thresh_iri, TG.minimumRvcPercent, lit))
        graph.add((thresh_iri, TG.hasSourceClause, clause_iri))
        if review_status:
            _add_reified_axiom(
                graph,
                source=thresh_iri,
                prop=TG.minimumRvcPercent,
                target=lit,
                axiom_id=f"staged-{agreement_slug}-rvc-threshold-{pct_slug}",
                clause=clause_iri,
                review_status=review_status,
            )
    return thresh_iri


def generate_staging_turtle(
    records: Iterable[dict],
    *,
    canonical_kb_path: Path | None = None,
    verify: bool = True,
    review_status: str | None = None,
) -> tuple[str, StagingBuildSummary]:
    """Build OWL 2 DL trial Turtle from structured records without modifying the canonical KB."""
    materialized = [raw.to_dict() if hasattr(raw, "to_dict") else raw for raw in records]
    flagged_diagnostics: list[str] = []
    if verify:
        checked = verify_records(materialized)
        usable = checked.verified
        flagged_count = len(checked.flagged)
        for item in checked.flagged[:20]:
            ident = item.record.get("locator") or item.record.get("number") or item.record.get("record_kind")
            flagged_diagnostics.append(f"{ident}: {'; '.join(item.reasons)}")
    else:
        usable = tuple(materialized)
        flagged_count = 0

    source_by_key, source_by_reg_ref, existing_iris = _load_existing_index(canonical_kb_path)

    graph = Graph()
    graph.bind("", TG)
    graph.bind("catalog", CATALOG)
    graph.bind("owl", OWL)
    graph.bind("rdf", RDF)
    graph.bind("rdfs", RDFS)
    graph.bind("sh", SH)
    graph.bind("swrl", SWRL)
    graph.bind("swrlb", SWRLB)
    graph.bind("xsd", XSD)

    emitted_docs: set[URIRef] = set()
    emitted_clauses: set[URIRef] = set()
    emitted_chapters: set[URIRef] = set()
    emitted_headings: set[URIRef] = set()
    emitted_hs6: set[URIRef] = set()
    emitted_hs8: set[URIRef] = set()
    emitted_psrs: set[URIRef] = set()
    emitted_thresholds: set[URIRef] = set()
    emitted_relations: set[URIRef] = set()
    skipped_existing: set[str] = set()

    for index, item in enumerate(usable, 1):
        kind = item.get("record_kind")

        if kind == "regulation":
            portal = str(item.get("portal") or "jdih")
            slug = str(item.get("slug") or item.get("number") or index)
            number_str = str(item.get("number") or slug)
            url = item.get("url")
            reg_ref = _normalize_reg_ref(f"{number_str} {item.get('title', '')}")
            if reg_ref and reg_ref in source_by_reg_ref:
                doc_iri = source_by_reg_ref[reg_ref]
            else:
                stable_hash = sha256((url or slug).encode("utf-8")).hexdigest()
                key = f"{portal.upper()}-{_slugify(number_str, max_len=28).upper()}-{stable_hash[:8].upper()}"
                reg_id = f"scraped-{stable_hash[:16]}"
                doc_iri = _ensure_source_document(
                    graph,
                    source_by_key=source_by_key,
                    emitted_docs=emitted_docs,
                    source_key=key,
                    source_id=reg_id,
                    title=f"{number_str} - {item.get('title', '')}".strip(" -"),
                    url=url,
                )
                if reg_ref:
                    source_by_reg_ref[reg_ref] = doc_iri

            if item.get("number"):
                graph.add((doc_iri, TG.regulationNumber, Literal(str(item["number"]))))
            if item.get("status"):
                graph.add((doc_iri, TG.regulationStatus, Literal(str(item["status"]))))
            if item.get("enacted_date"):
                graph.add((doc_iri, TG.enactedOn, Literal(str(item["enacted_date"]))))
            if item.get("promulgated_date"):
                graph.add((doc_iri, TG.promulgatedOn, Literal(str(item["promulgated_date"]))))
            effective = item.get("effective_date") or item.get("promulgated_date") or item.get("enacted_date")
            if effective:
                graph.add((doc_iri, TG.effectiveFromDate, Literal(str(effective))))

            for rel_idx, rel in enumerate(item.get("relations") or (), 1):
                rel_kind = str(rel.get("relation_kind") or "RELATED")
                target_num = rel.get("target_number")
                target_title = rel.get("target_title")
                target_url = rel.get("target_url")
                default_kind = "permendag" if portal == "jdih-kemendag" else ("pmk" if portal == "jdih-kemenkeu" else "reg")
                target_ref = _normalize_reg_ref(f"{target_num or ''} {target_title or ''}", default_kind=default_kind)
                if target_ref and target_ref in source_by_reg_ref:
                    target_iri = source_by_reg_ref[target_ref]
                else:
                    t_basis = target_url or target_num or target_title or f"{slug}-rel-{rel_idx}"
                    t_hash = sha256(str(t_basis).encode("utf-8")).hexdigest()
                    t_key = f"REF-{_slugify(str(target_num or t_basis), max_len=28).upper()}-{t_hash[:8].upper()}"
                    target_iri = _ensure_source_document(
                        graph,
                        source_by_key=source_by_key,
                        emitted_docs=emitted_docs,
                        source_key=t_key,
                        source_id=f"ref-{t_hash[:16]}",
                        title=f"{target_num or ''} - {target_title or ''}".strip(" -") or None,
                        url=target_url,
                    )
                    if target_num:
                        graph.add((target_iri, TG.regulationNumber, Literal(str(target_num))))
                    if target_ref:
                        source_by_reg_ref[target_ref] = target_iri

                # Normalize inverse relations (AMENDED_BY / REVOKED_BY) to canonical actor -> target direction.
                acting, affected = doc_iri, target_iri
                canonical_kind = rel_kind
                if rel_kind == "AMENDED_BY":
                    acting, affected, canonical_kind = target_iri, doc_iri, "AMENDS"
                elif rel_kind == "REVOKED_BY":
                    acting, affected, canonical_kind = target_iri, doc_iri, "REVOKES"
                elif rel_kind == "PARTIALLY_REVOKED_BY":
                    acting, affected, canonical_kind = target_iri, doc_iri, "PARTIALLY_REVOKES"

                direct_prop, kind_iri = LEGAL_RELATION_PROPS.get(canonical_kind, (None, TG.relRelated))
                if direct_prop is not None:
                    graph.add((acting, direct_prop, affected))

                rel_hash = sha256(f"{acting}|{canonical_kind}|{affected}".encode("utf-8")).hexdigest()[:12]
                event_iri = TG[f"legal-rel-{rel_hash}"]
                if event_iri not in existing_iris and event_iri not in emitted_relations:
                    emitted_relations.add(event_iri)
                    graph.add((event_iri, RDF.type, OWL.NamedIndividual))
                    graph.add((event_iri, RDF.type, TG.LegalRelationEvent))
                    graph.add((event_iri, TG.relationActingDocument, acting))
                    graph.add((event_iri, TG.relationTargetDocument, affected))
                    graph.add((event_iri, TG.hasLegalRelationKind, kind_iri))
                    rel_eff = rel.get("effective_date") or effective
                    if rel_eff:
                        graph.add((event_iri, TG.relationEffectiveDate, Literal(str(rel_eff))))
            continue

        source_key = str(item.get("source_key") or "UNKNOWN")
        doc_iri = _ensure_source_document(
            graph,
            source_by_key=source_by_key,
            emitted_docs=emitted_docs,
            source_key=source_key,
            source_id=item.get("source_id"),
            title=item.get("source_title"),
            url=item.get("source_url"),
        )

        if kind == "psr":
            row_no = int(item["row_no"])
            agreement_code = str(item.get("agreement") or "ATIGA").upper()
            agreement_iri, agreement_slug = AGREEMENT_MAP.get(
                agreement_code, (TG[agreement_code.lower()], agreement_code.lower()),
            )
            hs_level = str(item.get("hs_level") or "SUBHEADING").upper()
            hs_code = str(item.get("hs_code") or item.get("hs6") or "")
            hs_digits = str(item.get("hs_digits") or item.get("hs6_digits") or "")

            clause_iri = TG[f"staged-psr-{_slugify(source_key)}-row-{row_no}"]
            if clause_iri not in emitted_clauses:
                emitted_clauses.add(clause_iri)
                graph.add((clause_iri, RDF.type, OWL.NamedIndividual))
                graph.add((clause_iri, RDF.type, TG.SourceClause))
                graph.add((clause_iri, TG.clauseInDocument, doc_iri))
                graph.add((clause_iri, TG.clauseLocator, Literal(str(item["locator"]))))
                comment = f"{agreement_code} PSR {hs_code}: {item.get('concise_rule', '')} | {item.get('full_rule', '')}".strip(" |")
                graph.add((clause_iri, RDFS.comment, Literal(comment)))

            if hs_level == "CHAPTER":
                target_iri = _ensure_chapter(graph, hs_digits, existing_iris, emitted_chapters)
                graph.add((target_iri, TG.hasSourceClause, clause_iri))
                psr_link_pred = TG.psrForChapter
                psr_suffix = f"c{hs_digits}"
            elif hs_level == "HEADING":
                target_iri = _ensure_heading(graph, hs_digits, existing_iris, emitted_headings, emitted_chapters)
                graph.add((target_iri, TG.hasSourceClause, clause_iri))
                psr_link_pred = TG.psrForHeading
                psr_suffix = f"h{hs_digits}"
            else:
                head_iri = _ensure_heading(graph, hs_digits[:4], existing_iris, emitted_headings, emitted_chapters)
                hs6_iri = TG[f"hs6-{hs_digits}"]
                if hs6_iri in existing_iris:
                    skipped_existing.add(str(hs6_iri))
                    graph.add((hs6_iri, TG.hasSourceClause, clause_iri))
                elif hs6_iri not in emitted_hs6:
                    emitted_hs6.add(hs6_iri)
                    graph.add((hs6_iri, RDF.type, OWL.NamedIndividual))
                    graph.add((hs6_iri, RDF.type, TG.HSSubheading))
                    graph.add((hs6_iri, TG.subheadingCode, Literal(hs_digits)))
                    graph.add((hs6_iri, TG.subheadingHeading, head_iri))
                    if item.get("description"):
                        graph.add((hs6_iri, RDFS.label, Literal(f"{hs_code} {item['description']}")))
                    graph.add((hs6_iri, TG.hasSourceClause, clause_iri))
                    if review_status:
                        _add_reified_axiom(
                            graph,
                            source=hs6_iri,
                            prop=TG.subheadingCode,
                            target=Literal(hs_digits),
                            axiom_id=f"staged-hs6-{hs_digits}",
                            clause=clause_iri,
                            review_status=review_status,
                        )
                target_iri = hs6_iri
                psr_link_pred = TG.psrForSubheading
                psr_suffix = hs_digits

            psr_iri = TG[f"psr-{agreement_slug}-{psr_suffix}"]
            if psr_iri not in existing_iris and psr_iri not in emitted_psrs:
                emitted_psrs.add(psr_iri)
                graph.add((psr_iri, RDF.type, OWL.NamedIndividual))
                graph.add((psr_iri, RDF.type, TG.ProductSpecificRule))
                graph.add((psr_iri, TG.psrAgreement, agreement_iri))
                graph.add((psr_iri, psr_link_pred, target_iri))
                graph.add((psr_iri, TG.hasSourceClause, clause_iri))
                if item.get("concise_rule"):
                    graph.add((psr_iri, TG.conciseRuleText, Literal(str(item["concise_rule"]))))

                for alt_idx, alt in enumerate(item.get("criteria_alternatives") or (), 1):
                    mapped_tokens = [
                        token for token in alt
                        if isinstance(token, dict) and token.get("kind") in CRITERION_KIND_MAP
                    ]
                    if not mapped_tokens:
                        continue
                    alt_iri = TG[f"psr-{agreement_slug}-{psr_suffix}-alt-{alt_idx}"]
                    graph.add((alt_iri, RDF.type, OWL.NamedIndividual))
                    graph.add((alt_iri, RDF.type, TG.OriginRuleAlternative))
                    graph.add((psr_iri, TG.hasPsrAlternative, alt_iri))
                    for token in mapped_tokens:
                        tkind = token["kind"]
                        graph.add((alt_iri, TG.requiresCriterionKind, CRITERION_KIND_MAP[tkind]))
                        if tkind == "RVC" and token.get("threshold_percent"):
                            try:
                                decimal_pct = Decimal(str(token["threshold_percent"]))
                            except InvalidOperation:
                                decimal_pct = None
                            if decimal_pct is not None:
                                thresh_iri = _ensure_rvc_threshold(
                                    graph,
                                    decimal_pct=decimal_pct,
                                    agreement_slug=agreement_slug,
                                    agreement_label=agreement_code,
                                    existing=existing_iris,
                                    emitted_thresholds=emitted_thresholds,
                                    clause_iri=clause_iri,
                                    review_status=review_status,
                                )
                                graph.add((alt_iri, TG.alternativeRvcThreshold, thresh_iri))
                        if token.get("except_from"):
                            for code_token in re.findall(r"\d{2,4}(?:\.\d{2})?", str(token["except_from"])):
                                clean = code_token.replace(".", "")
                                if len(clean) == 2:
                                    ex_iri = _ensure_chapter(graph, clean, existing_iris, emitted_chapters)
                                    graph.add((alt_iri, TG.excludesChapter, ex_iri))
                                elif len(clean) == 4:
                                    ex_iri = _ensure_heading(
                                        graph, clean, existing_iris, emitted_headings, emitted_chapters,
                                    )
                                    graph.add((alt_iri, TG.excludesHeading, ex_iri))
                                elif len(clean) == 6:
                                    ex_head = _ensure_heading(
                                        graph, clean[:4], existing_iris, emitted_headings, emitted_chapters,
                                    )
                                    ex_sub = TG[f"hs6-{clean}"]
                                    if ex_sub not in existing_iris and ex_sub not in emitted_hs6:
                                        emitted_hs6.add(ex_sub)
                                        graph.add((ex_sub, RDF.type, OWL.NamedIndividual))
                                        graph.add((ex_sub, RDF.type, TG.HSSubheading))
                                        graph.add((ex_sub, TG.subheadingCode, Literal(clean)))
                                        graph.add((ex_sub, TG.subheadingHeading, ex_head))
                                    graph.add((alt_iri, TG.excludesSubheading, ex_sub))
            continue

        if kind == "tariff":
            row_no = int(item["row_no"])
            hs8_digits = str(item["hs8_digits"])
            hs8_code = str(item["hs8"])
            hs6_digits = str(item["hs6_digits"])
            agr_code = str(item.get("agreement") or "ATIGA").upper()
            eff_from = item.get("effective_from")
            eff_until = item.get("effective_until_exclusive")
            partners = tuple(str(p) for p in (item.get("partner_countries") or ()))
            mfn_excluded = bool(item.get("mfn_excluded"))

            period_tag = f"-{eff_from}" if eff_from and agr_code == "RCEP" else ""
            if agr_code == "ATIGA" and not period_tag:
                clause_iri = TG[f"staged-tariff-{_slugify(source_key)}-row-{row_no}"]
            else:
                clause_iri = TG[f"staged-tariff-{_slugify(source_key)}-{hs8_digits}{period_tag}"]
            if clause_iri not in emitted_clauses:
                emitted_clauses.add(clause_iri)
                graph.add((clause_iri, RDF.type, OWL.NamedIndividual))
                graph.add((clause_iri, RDF.type, TG.SourceClause))
                graph.add((clause_iri, TG.clauseInDocument, doc_iri))
                graph.add((clause_iri, TG.clauseLocator, Literal(str(item["locator"]))))
                rate_note = f"{agr_code} duty={'MFN' if mfn_excluded else (item.get('tariff_rate_percent') or item.get('tariff_rate_raw') or 'unknown')}"
                graph.add((clause_iri, RDFS.comment, Literal(rate_note)))

            head_iri = _ensure_heading(graph, hs6_digits[:4], existing_iris, emitted_headings, emitted_chapters)
            hs6_iri = TG[f"hs6-{hs6_digits}"]
            if hs6_iri not in existing_iris and hs6_iri not in emitted_hs6:
                emitted_hs6.add(hs6_iri)
                graph.add((hs6_iri, RDF.type, OWL.NamedIndividual))
                graph.add((hs6_iri, RDF.type, TG.HSSubheading))
                graph.add((hs6_iri, TG.subheadingCode, Literal(hs6_digits)))
                graph.add((hs6_iri, TG.subheadingHeading, head_iri))
                graph.add((hs6_iri, TG.hasSourceClause, clause_iri))

            hs8_iri = TG[f"hs-{hs8_digits}"]
            if hs8_iri in existing_iris:
                skipped_existing.add(str(hs8_iri))
                graph.add((hs8_iri, TG.hasSourceClause, clause_iri))
            elif hs8_iri not in emitted_hs8:
                emitted_hs8.add(hs8_iri)
                graph.add((hs8_iri, RDF.type, OWL.NamedIndividual))
                graph.add((hs8_iri, RDF.type, TG.HSCode))
                desc = item.get("description_id") or item.get("description_en") or hs8_code
                graph.add((hs8_iri, RDFS.label, Literal(f"{hs8_code} {desc}", lang="id")))
                graph.add((hs8_iri, TG.codeValue, Literal(hs8_code)))
                graph.add((hs8_iri, TG.nomenclature, Literal("AHTN 2022")))
                graph.add((hs8_iri, TG.tariffSubheading, hs6_iri))
                graph.add((hs8_iri, TG.hasSourceClause, clause_iri))
                if review_status:
                    _add_reified_axiom(
                        graph,
                        source=hs8_iri,
                        prop=TG.codeValue,
                        target=Literal(hs8_code),
                        axiom_id=f"staged-hs-{hs8_digits}",
                        clause=clause_iri,
                        review_status=review_status,
                    )

            rate_str = item.get("tariff_rate_percent")
            if agr_code == "MFN" and rate_str is not None:
                try:
                    rate_dec = Decimal(str(rate_str))
                except InvalidOperation:
                    rate_dec = None
                if rate_dec is not None:
                    plain_rate = format(rate_dec, "f")
                    if "." not in plain_rate:
                        plain_rate = f"{plain_rate}.0"
                    graph.add((hs8_iri, TG.mfnDutyPercent, Literal(plain_rate, datatype=XSD.decimal)))
                continue

            if mfn_excluded or rate_str is not None:
                rate_dec = None
                if rate_str is not None and not mfn_excluded:
                    try:
                        rate_dec = Decimal(str(rate_str))
                    except InvalidOperation:
                        rate_dec = None
                if mfn_excluded or rate_dec is not None:
                    agr_iri, agr_slug = AGREEMENT_MAP.get(agr_code, (TG.atiga, "atiga"))
                    partner_tag = f"-{_slugify(source_key)}" if partners else ""
                    entry_iri = TG[f"tariff-{agr_slug}{partner_tag}-{hs8_digits}{period_tag}"]
                    if entry_iri not in existing_iris:
                        graph.add((entry_iri, RDF.type, OWL.NamedIndividual))
                        graph.add((entry_iri, RDF.type, TG.TariffRateEntry))
                        graph.add((entry_iri, TG.tariffAgreement, agr_iri))
                        graph.add((entry_iri, TG.tariffTargetCode, hs8_iri))
                        for p_name in partners:
                            graph.add((entry_iri, TG.tariffPartnerCountry, TG[p_name]))
                        if eff_from:
                            graph.add((entry_iri, TG.tariffEffectiveFrom, Literal(str(eff_from))))
                        if eff_until:
                            graph.add((entry_iri, TG.tariffEffectiveUntilExclusive, Literal(str(eff_until))))
                        if mfn_excluded:
                            graph.add((entry_iri, TG.tariffExcludedToMfn, Literal(True)))
                        elif rate_dec is not None:
                            plain_rate = format(rate_dec, "f")
                            if "." not in plain_rate:
                                plain_rate = f"{plain_rate}.0"
                            graph.add((entry_iri, TG.preferentialDutyPercent, Literal(plain_rate, datatype=XSD.decimal)))
                        graph.add((entry_iri, TG.hasSourceClause, clause_iri))
            continue

        if kind == "rcep_appendix":
            row_no = int(item["row_no"])
            hs8_digits = str(item["hs8_digits"])
            hs8_code = str(item.get("hs8") or f"{hs8_digits[:4]}.{hs8_digits[4:6]}.{hs8_digits[6:8]}")
            hs6_digits = hs8_digits[:6]
            clause_iri = TG[f"staged-rcep-app-{_slugify(source_key)}-row-{row_no}"]
            if clause_iri not in emitted_clauses:
                emitted_clauses.add(clause_iri)
                graph.add((clause_iri, RDF.type, OWL.NamedIndividual))
                graph.add((clause_iri, RDF.type, TG.SourceClause))
                graph.add((clause_iri, TG.clauseInDocument, doc_iri))
                graph.add((clause_iri, TG.clauseLocator, Literal(str(item["locator"]))))
            head_iri = _ensure_heading(graph, hs6_digits[:4], existing_iris, emitted_headings, emitted_chapters)
            hs6_iri = TG[f"hs6-{hs6_digits}"]
            if hs6_iri not in existing_iris and hs6_iri not in emitted_hs6:
                emitted_hs6.add(hs6_iri)
                graph.add((hs6_iri, RDF.type, OWL.NamedIndividual))
                graph.add((hs6_iri, RDF.type, TG.HSSubheading))
                graph.add((hs6_iri, TG.subheadingCode, Literal(hs6_digits)))
                graph.add((hs6_iri, TG.subheadingHeading, head_iri))
                graph.add((hs6_iri, TG.hasSourceClause, clause_iri))
            hs8_iri = TG[f"hs-{hs8_digits}"]
            if hs8_iri not in existing_iris and hs8_iri not in emitted_hs8:
                emitted_hs8.add(hs8_iri)
                graph.add((hs8_iri, RDF.type, OWL.NamedIndividual))
                graph.add((hs8_iri, RDF.type, TG.HSCode))
                graph.add((hs8_iri, TG.codeValue, Literal(hs8_code)))
                graph.add((hs8_iri, TG.nomenclature, Literal("AHTN 2022")))
                graph.add((hs8_iri, TG.tariffSubheading, hs6_iri))
            graph.add((hs8_iri, TG.subjectToRcepTariffDifferential, Literal(True)))
            graph.add((hs8_iri, TG.hasSourceClause, clause_iri))
            continue

        if kind == "lartas":
            row_no = int(item.get("row_no") or index)
            hs8_digits = str(item["hs8_digits"])
            hs8_code = str(item["hs8"])
            hs6_digits = hs8_digits[:6]
            clause_iri = TG[f"staged-lartas-{_slugify(source_key)}-{hs8_digits}"]
            if clause_iri not in emitted_clauses:
                emitted_clauses.add(clause_iri)
                graph.add((clause_iri, RDF.type, OWL.NamedIndividual))
                graph.add((clause_iri, RDF.type, TG.SourceClause))
                graph.add((clause_iri, TG.clauseInDocument, doc_iri))
                graph.add((clause_iri, TG.clauseLocator, Literal(str(item["locator"]))))
                if item.get("commodity_group"):
                    graph.add((clause_iri, RDFS.comment, Literal(f"Lartas {hs8_code}: {item['commodity_group']}")))

            head_iri = _ensure_heading(graph, hs6_digits[:4], existing_iris, emitted_headings, emitted_chapters)
            hs6_iri = TG[f"hs6-{hs6_digits}"]
            if hs6_iri not in existing_iris and hs6_iri not in emitted_hs6:
                emitted_hs6.add(hs6_iri)
                graph.add((hs6_iri, RDF.type, OWL.NamedIndividual))
                graph.add((hs6_iri, RDF.type, TG.HSSubheading))
                graph.add((hs6_iri, TG.subheadingCode, Literal(hs6_digits)))
                graph.add((hs6_iri, TG.subheadingHeading, head_iri))
            hs8_iri = TG[f"hs-{hs8_digits}"]
            if hs8_iri not in existing_iris and hs8_iri not in emitted_hs8:
                emitted_hs8.add(hs8_iri)
                graph.add((hs8_iri, RDF.type, OWL.NamedIndividual))
                graph.add((hs8_iri, RDF.type, TG.HSCode))
                desc = item.get("description") or hs8_code
                graph.add((hs8_iri, RDFS.label, Literal(f"{hs8_code} {desc}", lang="id")))
                graph.add((hs8_iri, TG.codeValue, Literal(hs8_code)))
                graph.add((hs8_iri, TG.nomenclature, Literal("AHTN 2022")))
                graph.add((hs8_iri, TG.tariffSubheading, hs6_iri))
            graph.add((hs8_iri, TG.hasSourceClause, clause_iri))

            rule_iri = TG[f"lartas-{_slugify(source_key)}-{hs8_digits}"]
            if rule_iri not in existing_iris:
                graph.add((rule_iri, RDF.type, OWL.NamedIndividual))
                graph.add((rule_iri, RDF.type, TG.ImportRestrictionRule))
                graph.add((rule_iri, TG.lartasTargetCode, hs8_iri))
                graph.add((rule_iri, TG.lartasJurisdiction, TG.jurisdictionIndonesiaCustoms))
                mode_map = {
                    "BORDER": TG.supervisionBorder,
                    "POST_BORDER": TG.supervisionPostBorder,
                    "PROHIBITED": TG.supervisionProhibited,
                }
                graph.add((rule_iri, TG.lartasSupervisionMode, mode_map[str(item["supervision_mode"])]))
                if item.get("prohibited"):
                    graph.add((rule_iri, TG.lartasProhibited, Literal(True)))
                if item.get("commodity_group"):
                    graph.add((rule_iri, TG.lartasCommodityGroup, Literal(str(item["commodity_group"]))))
                if item.get("effective_from"):
                    graph.add((rule_iri, TG.lartasEffectiveFrom, Literal(str(item["effective_from"]))))
                for doc_name in item.get("required_doc_types") or ():
                    graph.add((rule_iri, TG.lartasRequiresDocumentType, TG[str(doc_name)]))
                for api_name in item.get("allowed_api") or ():
                    graph.add((rule_iri, TG.lartasAllowedApi, TG[str(api_name)]))
                graph.add((rule_iri, TG.hasSourceClause, clause_iri))
            continue

        if kind == "export_lartas":
            hs8_digits = str(item["hs8_digits"])
            hs8_code = str(item["hs8"])
            hs6_digits = hs8_digits[:6]
            clause_iri = TG[f"staged-export-lartas-{_slugify(source_key)}-{hs8_digits}"]
            if clause_iri not in emitted_clauses:
                emitted_clauses.add(clause_iri)
                graph.add((clause_iri, RDF.type, OWL.NamedIndividual))
                graph.add((clause_iri, RDF.type, TG.SourceClause))
                graph.add((clause_iri, TG.clauseInDocument, doc_iri))
                graph.add((clause_iri, TG.clauseLocator, Literal(str(item["locator"]))))
                if item.get("commodity_group"):
                    graph.add((clause_iri, RDFS.comment, Literal(f"Export Lartas {hs8_code}: {item['commodity_group']}")))

            head_iri = _ensure_heading(graph, hs6_digits[:4], existing_iris, emitted_headings, emitted_chapters)
            hs6_iri = TG[f"hs6-{hs6_digits}"]
            if hs6_iri not in existing_iris and hs6_iri not in emitted_hs6:
                emitted_hs6.add(hs6_iri)
                graph.add((hs6_iri, RDF.type, OWL.NamedIndividual))
                graph.add((hs6_iri, RDF.type, TG.HSSubheading))
                graph.add((hs6_iri, TG.subheadingCode, Literal(hs6_digits)))
                graph.add((hs6_iri, TG.subheadingHeading, head_iri))
            hs8_iri = TG[f"hs-{hs8_digits}"]
            if hs8_iri not in existing_iris and hs8_iri not in emitted_hs8:
                emitted_hs8.add(hs8_iri)
                graph.add((hs8_iri, RDF.type, OWL.NamedIndividual))
                graph.add((hs8_iri, RDF.type, TG.HSCode))
                desc = item.get("description") or hs8_code
                graph.add((hs8_iri, RDFS.label, Literal(f"{hs8_code} {desc}", lang="id")))
                graph.add((hs8_iri, TG.codeValue, Literal(hs8_code)))
                graph.add((hs8_iri, TG.nomenclature, Literal("AHTN 2022")))
                graph.add((hs8_iri, TG.tariffSubheading, hs6_iri))
            graph.add((hs8_iri, TG.hasSourceClause, clause_iri))

            rule_iri = TG[f"export-lartas-{_slugify(source_key)}-{hs8_digits}"]
            if rule_iri not in existing_iris:
                graph.add((rule_iri, RDF.type, OWL.NamedIndividual))
                graph.add((rule_iri, RDF.type, TG.ExportRestrictionRule))
                graph.add((rule_iri, TG.exportLartasTargetCode, hs8_iri))
                graph.add((rule_iri, TG.exportLartasJurisdiction, TG.jurisdictionIndonesiaCustoms))
                exp_mode_map = {
                    "RESTRICTED_EXPORT": TG.supervisionExportRestricted,
                    "PROHIBITED_EXPORT": TG.supervisionExportProhibited,
                }
                graph.add((rule_iri, TG.exportLartasSupervisionMode, exp_mode_map[str(item["supervision_mode"])]))
                if item.get("prohibited"):
                    graph.add((rule_iri, TG.exportLartasProhibited, Literal(True)))
                if item.get("commodity_group"):
                    graph.add((rule_iri, TG.exportLartasCommodityGroup, Literal(str(item["commodity_group"]))))
                if item.get("effective_from"):
                    graph.add((rule_iri, TG.exportLartasEffectiveFrom, Literal(str(item["effective_from"]))))
                for doc_name in item.get("required_doc_types") or ():
                    graph.add((rule_iri, TG.exportLartasRequiresDocumentType, TG[str(doc_name)]))
                graph.add((rule_iri, TG.hasSourceClause, clause_iri))
            continue

        if kind == "export_duty":
            hs8_digits = str(item["hs8_digits"])
            hs8_code = str(item["hs8"])
            hs6_digits = hs8_digits[:6]
            sec = str(item.get("lampiran_section") or "X").lower()
            tier = item.get("tier_column")
            tier_tag = f"-col{tier}" if tier is not None else "-flat"
            clause_iri = TG[f"staged-bk-{_slugify(source_key)}-{sec}-{hs8_digits}{tier_tag}"]
            if clause_iri not in emitted_clauses:
                emitted_clauses.add(clause_iri)
                graph.add((clause_iri, RDF.type, OWL.NamedIndividual))
                graph.add((clause_iri, RDF.type, TG.SourceClause))
                graph.add((clause_iri, TG.clauseInDocument, doc_iri))
                graph.add((clause_iri, TG.clauseLocator, Literal(str(item["locator"]))))
                if item.get("commodity_group"):
                    graph.add((clause_iri, RDFS.comment, Literal(f"Bea Keluar {hs8_code}: {item['commodity_group']}")))

            head_iri = _ensure_heading(graph, hs6_digits[:4], existing_iris, emitted_headings, emitted_chapters)
            hs6_iri = TG[f"hs6-{hs6_digits}"]
            if hs6_iri not in existing_iris and hs6_iri not in emitted_hs6:
                emitted_hs6.add(hs6_iri)
                graph.add((hs6_iri, RDF.type, OWL.NamedIndividual))
                graph.add((hs6_iri, RDF.type, TG.HSSubheading))
                graph.add((hs6_iri, TG.subheadingCode, Literal(hs6_digits)))
                graph.add((hs6_iri, TG.subheadingHeading, head_iri))
            hs8_iri = TG[f"hs-{hs8_digits}"]
            if hs8_iri not in existing_iris and hs8_iri not in emitted_hs8:
                emitted_hs8.add(hs8_iri)
                graph.add((hs8_iri, RDF.type, OWL.NamedIndividual))
                graph.add((hs8_iri, RDF.type, TG.HSCode))
                desc = item.get("description") or hs8_code
                graph.add((hs8_iri, RDFS.label, Literal(f"{hs8_code} {desc}", lang="id")))
                graph.add((hs8_iri, TG.codeValue, Literal(hs8_code)))
                graph.add((hs8_iri, TG.nomenclature, Literal("AHTN 2022")))
                graph.add((hs8_iri, TG.tariffSubheading, hs6_iri))
            graph.add((hs8_iri, TG.hasSourceClause, clause_iri))

            rule_iri = TG[f"bk-{_slugify(source_key)}-{sec}-{hs8_digits}{tier_tag}"]
            if rule_iri not in existing_iris:
                graph.add((rule_iri, RDF.type, OWL.NamedIndividual))
                graph.add((rule_iri, RDF.type, TG.ExportDutyRule))
                graph.add((rule_iri, TG.exportDutyTargetCode, hs8_iri))
                graph.add((rule_iri, TG.exportDutyJurisdiction, TG.jurisdictionIndonesiaCustoms))
                basis_str = str(item["duty_basis"])
                basis_iri = TG.exportDutySpecificUsdPerMt if basis_str == "SPECIFIC_USD_PER_MT" else TG.exportDutyAdValorem
                graph.add((rule_iri, TG.exportDutyBasis, basis_iri))
                rate_dec = Decimal(str(item["rate_value"]))
                rate_lit = Literal(f"{rate_dec:.2f}", datatype=XSD.decimal)
                if basis_str == "SPECIFIC_USD_PER_MT":
                    graph.add((rule_iri, TG.exportDutySpecificUsdPerMt, rate_lit))
                else:
                    graph.add((rule_iri, TG.exportDutyRatePercent, rate_lit))
                if item.get("reference_price_min_exclusive") is not None:
                    graph.add((rule_iri, TG.exportDutyRefPriceMinExclusive, Literal(str(item["reference_price_min_exclusive"]), datatype=XSD.decimal)))
                if item.get("reference_price_max_inclusive") is not None:
                    graph.add((rule_iri, TG.exportDutyRefPriceMaxInclusive, Literal(str(item["reference_price_max_inclusive"]), datatype=XSD.decimal)))
                if tier is not None:
                    graph.add((rule_iri, TG.exportDutyTierColumn, Literal(int(tier), datatype=XSD.integer)))
                if item.get("commodity_group"):
                    graph.add((rule_iri, TG.exportDutyCommodityGroup, Literal(str(item["commodity_group"]))))
                if item.get("effective_from"):
                    graph.add((rule_iri, TG.exportDutyEffectiveFrom, Literal(str(item["effective_from"]))))
                if item.get("effective_to_exclusive"):
                    graph.add((rule_iri, TG.exportDutyEffectiveUntilExclusive, Literal(str(item["effective_to_exclusive"]))))
                graph.add((rule_iri, TG.hasSourceClause, clause_iri))
            continue

        if kind in {"statute_clause", "guidance_section"}:
            locator = str(item.get("locator") or item.get("pasal") or item.get("heading") or index)
            clause_iri = TG[f"staged-clause-{_slugify(source_key)}-{_slugify(locator)}"]
            if clause_iri not in emitted_clauses:
                emitted_clauses.add(clause_iri)
                graph.add((clause_iri, RDF.type, OWL.NamedIndividual))
                graph.add((clause_iri, RDF.type, TG.SourceClause))
                graph.add((clause_iri, TG.clauseInDocument, doc_iri))
                graph.add((clause_iri, TG.clauseLocator, Literal(locator)))
                excerpt = str(item.get("text") or "")[:300]
                if excerpt:
                    graph.add((clause_iri, RDFS.comment, Literal(excerpt)))
            continue

        raise ValueError(f"Unsupported record_kind for RDF staging: {kind!r}")

    turtle = graph.serialize(format="turtle")
    summary = StagingBuildSummary(
        records_processed=len(materialized),
        verified_records=len(usable),
        flagged_records=flagged_count,
        flagged_diagnostics=tuple(flagged_diagnostics),
        triples_generated=len(graph),
        source_documents_emitted=len(emitted_docs),
        source_clauses_emitted=len(emitted_clauses),
        hs_subheadings_emitted=len(emitted_hs6),
        hs_codes_emitted=len(emitted_hs8),
        psr_rules_emitted=len(emitted_psrs),
        origin_thresholds_emitted=len(emitted_thresholds),
        legal_relations_emitted=len(emitted_relations),
        skipped_existing_iris=tuple(sorted(skipped_existing)),
    )
    return turtle, summary


def validate_trial_merge(
    canonical_kb_path: Path,
    staging_turtle_path: Path,
    *,
    engine_profile_id: str = ENGINE_PROFILE_ID,
    run_owl_validator: bool = True,
) -> TrialValidationResult:
    """Merge staging Turtle into a temporary copy of the canonical KB and validate without touching the original."""
    canonical_bytes = canonical_kb_path.read_bytes()
    canonical_sha = sha256(canonical_bytes).hexdigest()
    canonical_compiled = compile_kb(canonical_kb_path, engine_profile_id)
    canonical_owl_count = len(canonical_compiled.dataset.graph(canonical_compiled.owl_projection_ref))

    staging_bytes = staging_turtle_path.read_bytes()
    merged_graph = parse_turtle(canonical_bytes, canonical_kb_path.resolve().as_uri())
    staging_graph = parse_turtle(staging_bytes, staging_turtle_path.resolve().as_uri())
    for triple in staging_graph:
        merged_graph.add(triple)

    diagnostics: list[str] = []
    with TemporaryDirectory(prefix="tradegraph-staging-validate-") as workspace:
        merged_path = Path(workspace) / "merged-trial.ttl"
        merged_graph.serialize(merged_path, format="turtle", encoding="utf-8")
        try:
            merged_compiled = compile_kb(merged_path, engine_profile_id)
        except KnowledgeBaseError as error:
            return TrialValidationResult(
                kb_compiled=False,
                owl_validated=None,
                canonical_sha256=canonical_sha,
                merged_sha256=None,
                canonical_owl_triples=canonical_owl_count,
                merged_owl_triples=None,
                diagnostics=(str(error),),
            )

        merged_owl_count = len(merged_compiled.dataset.graph(merged_compiled.owl_projection_ref))
        owl_ok: bool | None = None
        if run_owl_validator:
            report = validate_owl(merged_compiled)
            if report.conforms:
                owl_ok = True
            elif any("Maven and semantic/pom.xml are required" in msg for msg in report.findings):
                owl_ok = None
                diagnostics.append("OWLAPI/HermiT check skipped: Maven (mvn) is not on PATH.")
            else:
                owl_ok = False
                diagnostics.extend(report.findings)

        return TrialValidationResult(
            kb_compiled=True,
            owl_validated=owl_ok,
            canonical_sha256=canonical_sha,
            merged_sha256=merged_compiled.snapshot.sha256,
            canonical_owl_triples=canonical_owl_count,
            merged_owl_triples=merged_owl_count,
            diagnostics=tuple(diagnostics),
        )
