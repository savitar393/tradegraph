from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from rdflib import BNode, Dataset, Literal as RDFLiteral, URIRef


@dataclass(frozen=True, slots=True)
class EffectivePeriod:
    start: date
    end_exclusive: date | None = None


@dataclass(frozen=True, slots=True)
class SourceRecord:
    id: str
    url: str
    authority: str | None
    acquired_at: datetime
    content_hash: str
    local_ref: str
    status: Literal["ACQUIRED"]
    effective_period: EffectivePeriod | None


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    iri: str
    status: str


@dataclass(frozen=True, slots=True)
class KBSnapshot:
    kb_id: str
    version: str
    sha256: str
    engine_profile_id: str
    coverage: tuple[CoverageEntry, ...]
    artifact_ref: str


@dataclass(frozen=True, slots=True)
class FieldDefinition:
    iri: str
    label: str
    property_iri: str
    value_type_iri: str


@dataclass(frozen=True, slots=True)
class QueryDefinition:
    iri: str
    label: str
    sparql: str
    parameters: tuple[str, ...]
    requires_exact_decimal: bool = False


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    iri: str
    label: str
    preconditions: tuple[URIRef | BNode, ...]
    effects: tuple[URIRef | BNode, ...]
    planning: PlanningAction | None = None


@dataclass(frozen=True, slots=True)
class EvidenceDefinition:
    iri: str
    label: str
    sparql: str
    parameters: tuple[str, ...]
    shape_iri: str
    admit: bool = False


@dataclass(frozen=True, slots=True)
class PolicyDefinition:
    iri: str
    label: str
    scope_iri: str
    kind: Literal["CRITERION", "OBLIGATION"]
    source_clauses: tuple[str, ...]
    satisfied_query: str
    failed_query: str | None = None
    exception_query: str | None = None
    jurisdiction_iri: str | None = None
    role_iri: str | None = None


@dataclass(frozen=True, slots=True)
class CalculationParameter:
    name: str
    datatype_iri: str
    context_binding: str | None


@dataclass(frozen=True, slots=True)
class Quantity:
    value: Decimal
    unit_iri: str
    source_clauses: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ActionEffect:
    operation: Literal["ADD", "DELETE"]
    sparql: str


@dataclass(frozen=True, slots=True)
class PlanningAction:
    parameters: tuple[CalculationParameter, ...]
    grounding_sparql: str | None
    conditions: tuple[str, ...]
    changes: tuple[ActionEffect, ...]
    dependencies: tuple[str, ...]
    goal_iris: tuple[str, ...]
    source_clauses: tuple[str, ...]
    cost: Quantity | None = None
    duration: Quantity | None = None


@dataclass(frozen=True, slots=True)
class ObjectiveDefinition:
    metric: Literal["STEPS", "COST", "DURATION"]
    unit_iri: str | None


@dataclass(frozen=True, slots=True)
class CalculationDefinition:
    iri: str
    label: str
    sparql: str
    parameters: tuple[CalculationParameter, ...]
    closure_sparql: str
    scope_parameter: str


@dataclass(frozen=True, slots=True)
class CompiledKB:
    snapshot: KBSnapshot
    source_bytes: bytes
    dataset: Dataset
    owl_projection_ref: URIRef
    rule_projection_ref: URIRef
    shapes_graph_ref: URIRef
    definitions_graph_ref: URIRef
    reference_graph_ref: URIRef
    field_catalog: Mapping[str, FieldDefinition]
    query_catalog: Mapping[str, QueryDefinition]
    action_catalog: Mapping[str, ActionDefinition]
    rule_atom_counts: Mapping[str, tuple[int, int]]
    evidence_catalog: Mapping[str, EvidenceDefinition] = field(default_factory=dict)
    calculation_catalog: Mapping[str, CalculationDefinition] = field(default_factory=dict)
    policy_catalog: Mapping[str, PolicyDefinition] = field(default_factory=dict)
    decision_queries: Mapping[str, QueryDefinition] = field(default_factory=dict)
    coverage_query: QueryDefinition | None = None
    goal_catalog: Mapping[str, QueryDefinition] = field(default_factory=dict)
    objective_catalog: Mapping[str, ObjectiveDefinition] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvidenceFinding:
    definition_id: str
    shape_id: str
    focus_iri: str | None
    status: Literal["SATISFIED", "UNDETERMINED"]
    diagnostics: tuple[str, ...] = ()
    kind: Literal["VALIDATION"] = "VALIDATION"
    finding_id: str | None = field(default=None, compare=False)
    shacl_report_turtle: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    conforms: bool
    findings: tuple[str, ...]
    scope_id: str | None = None
    shape_ids: tuple[str, ...] = ()
    evidence_findings: tuple[EvidenceFinding, ...] = ()
    kb_sha256: str | None = None
    case_id: str | None = None
    case_revision: int | None = None
    evidence_scope_closed: bool | None = None
    report_id: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class CalculationClosure:
    scope: URIRef
    confirmed: bool
    sparql: str


@dataclass(frozen=True, slots=True)
class CalculationFinding:
    query_id: str
    kb_sha256: str
    case_id: str
    case_revision: int
    facts_sha256: str
    formula: str
    parameters: Mapping[str, URIRef | RDFLiteral]
    closure: CalculationClosure
    value: Decimal | None = None
    unit_iri: str | None = None
    currency_iri: str | None = None
    diagnostics: tuple[str, ...] = ()
    kind: Literal["CALCULATED"] = "CALCULATED"

    @property
    def calculation_available(self) -> bool:
        return self.value is not None


type RDFTriple = tuple[URIRef | BNode, URIRef, URIRef | BNode | RDFLiteral]


@dataclass(frozen=True, slots=True)
class AssessmentContext:
    kb_sha256: str
    engine_profile_id: str
    jurisdiction_iri: str | None = None
    procedure_iri: str | None = None
    agreement_iri: str | None = None
    transaction_at: datetime | None = None
    evaluation_at: datetime | None = None
    evidence_scope_id: str | None = None
    product_iri: str | None = None
    batch_iri: str | None = None
    kb_version: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceClaim:
    claim_iri: str
    subject_iri: str
    predicate_iri: str
    source_iri: str
    value: URIRef | RDFLiteral | None = None
    verification_status: Literal["UNVERIFIED", "VERIFIED", "REJECTED"] = "UNVERIFIED"


@dataclass(frozen=True, slots=True)
class FactPatch:
    reason: str
    add_triples: tuple[RDFTriple, ...] = ()
    remove_triples: tuple[RDFTriple, ...] = ()
    context: AssessmentContext | None = None
    evidence_claims: tuple[EvidenceClaim, ...] | None = None
    evidence_scope_closed: bool | None = None


@dataclass(frozen=True, slots=True)
class CaseSnapshot:
    case_id: str
    revision: int
    parent_revision: int | None
    facts_turtle: str
    evidence_refs: tuple[str, ...]
    context: AssessmentContext
    evidence_claims: tuple[EvidenceClaim, ...]
    evidence_scope_closed: bool | None
    created_at: datetime
    reason: str | None


type ExecutionStatus = Literal["CONVERGED", "UNSUPPORTED", "TIMEOUT", "LIMIT_REACHED", "FAILED"]
type Consistency = Literal["CONSISTENT", "INCONSISTENT"]
type NamedFact = tuple[URIRef, URIRef, URIRef | RDFLiteral]
type QueryValue = bool | tuple[Mapping[str, URIRef | BNode | RDFLiteral], ...]


@dataclass(frozen=True, slots=True)
class ReasoningLimits:
    max_iterations: int = 8
    timeout_ms: int = 30_000

    def __post_init__(self):
        for name in ("max_iterations", "timeout_ms"):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value < 2**31 - 1:
                raise ValueError(f"{name} must be a positive integer below 2147483647")


@dataclass(frozen=True, slots=True)
class ReasoningJob:
    kb_sha256: str
    case_snapshot: CaseSnapshot
    query_ids: tuple[str, ...]
    limits: ReasoningLimits = ReasoningLimits()


@dataclass(frozen=True, slots=True)
class ReasoningContext:
    kb_sha256: str
    engine_profile_id: str
    case_id: str
    case_revision: int
    assessment_context_iri: str
    assessment_context: AssessmentContext


@dataclass(frozen=True, slots=True)
class AxiomWitness:
    axiom_ids: tuple[str, ...]
    source_clauses: tuple[str, ...]
    rendering: str
    fact: NamedFact | None


@dataclass(frozen=True, slots=True)
class RuleWitness:
    rule_id: str
    source_clauses: tuple[str, ...]
    bindings: tuple[tuple[str, URIRef | RDFLiteral], ...]
    premises: tuple[NamedFact, ...]


@dataclass(frozen=True, slots=True)
class FactSupport:
    context: ReasoningContext
    kind: Literal["OWL_ENTAILED", "RULE_DERIVED"]
    iteration: int
    explanation_status: Literal["AVAILABLE", "EXPLANATION_UNAVAILABLE"] = "EXPLANATION_UNAVAILABLE"
    axioms: tuple[AxiomWitness, ...] = ()
    rule: RuleWitness | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InferenceResult:
    execution_status: ExecutionStatus
    consistency: Consistency | None
    context: ReasoningContext
    inferred_facts: tuple[NamedFact, ...] = ()
    query_results: Mapping[str, QueryValue] = field(default_factory=dict)
    supports: tuple[FactSupport, ...] = ()
    completeness_claim: Literal["NOT_CLAIMED"] = "NOT_CLAIMED"
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AssessmentRequest:
    kb_sha256: str
    case_id: str
    revision: int
    query_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Finding:
    triple: NamedFact
    support: FactSupport
    finding_id: str | None = field(default=None, compare=False)

    @property
    def kind(self) -> Literal["OWL_ENTAILED", "RULE_DERIVED"]:
        return self.support.kind


@dataclass(frozen=True, slots=True)
class DomainFinding:
    policy_id: str
    label: str
    kind: Literal["CRITERION", "OBLIGATION"]
    status: Literal["SATISFIED", "NOT_SATISFIED", "UNDETERMINED", "NOT_APPLICABLE"]
    scope_iri: str
    source_clauses: tuple[str, ...]
    evidence: tuple[str, ...] = ()
    responsible_parties: tuple[str, ...] = ()
    deadlines: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    decision_query_ids: tuple[str, ...] = ()
    proof_facts: tuple[NamedFact, ...] = ()
    finding_id: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class AssessmentReport:
    run_id: str
    context: ReasoningContext
    execution_status: ExecutionStatus
    consistency: Consistency | None
    coverage: tuple[CoverageEntry, ...]
    findings: tuple[Finding, ...]
    query_results: Mapping[str, QueryValue]
    completeness_claim: Literal["NOT_CLAIMED"]
    diagnostics: tuple[str, ...]
    validation_report: ValidationReport | None = None
    domain_findings: tuple[DomainFinding, ...] = ()
    coverage_status: Literal["SUPPORTED", "PARTIAL", "OUT_OF_COVERAGE"] | None = None
    run_at: datetime | None = None

    @property
    def kb_sha256(self) -> str:
        return self.context.kb_sha256

    @property
    def case_revision(self) -> int:
        return self.context.case_revision


@dataclass(frozen=True, slots=True)
class FactChanges:
    added: tuple[RDFTriple, ...] = ()
    removed: tuple[RDFTriple, ...] = ()


@dataclass(frozen=True, slots=True)
class FindingChange:
    kind: Literal["INFERENCE", "POLICY", "VALIDATION"]
    key: str
    before: str | None
    after: str | None
    before_id: str | None = None
    after_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScenarioDiff:
    base_run_id: str
    scenario_run_id: str
    fact_changes: FactChanges
    finding_changes: tuple[FindingChange, ...]
    scenario_report: AssessmentReport
    context_changes: tuple[tuple[str, str | datetime | None, str | datetime | None], ...] = ()


@dataclass(frozen=True, slots=True)
class PlanningLimits:
    max_states: int = 16
    max_depth: int = 4
    timeout_ms: int = 60_000

    def __post_init__(self):
        for name in ("max_states", "max_depth", "timeout_ms"):
            value = getattr(self, name)
            minimum = 0 if name == "max_depth" else 1
            if type(value) is not int or not minimum <= value < 2**31 - 1:
                raise ValueError(f"Invalid planning limit {name}")


@dataclass(frozen=True, slots=True)
class PlanRequest:
    base_run_id: str
    goal_iri: str
    allowed_action_ids: tuple[str, ...] = ()
    limits: PlanningLimits = PlanningLimits()
    objective_id: str = "fewest-actions"


@dataclass(frozen=True, slots=True)
class PlanStep:
    action_id: str
    bindings: tuple[tuple[str, URIRef | RDFLiteral], ...]
    effects: FactChanges
    source_clauses: tuple[str, ...]
    cost: Quantity | None = None
    duration: Quantity | None = None
    kind: Literal["HYPOTHETICAL"] = "HYPOTHETICAL"


@dataclass(frozen=True, slots=True)
class PlanCandidate:
    steps: tuple[PlanStep, ...]
    assumptions: tuple[str, ...]
    predicted_findings: tuple[Finding | DomainFinding | EvidenceFinding, ...]
    cost: Decimal | None
    cost_unit_iri: str | None
    duration: Decimal | None
    duration_unit_iri: str | None
    search_status: Literal["COMPLETE", "TRUNCATED"]
    optimality_claim: bool
    goal_reached: bool
    preconditions_hold: bool
    assessment_run_id: str
    case_revision: int
    diagnostics: tuple[str, ...] = ()
