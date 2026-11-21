"""Pydantic v2 request schemas for the TradeGraph HTTP API."""

from __future__ import annotations

from decimal import Decimal
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from tradegraph.optimization.export_service import ExportSpec
from tradegraph.optimization.service import MaterialInput, ShipmentSpec


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class MaterialPayload(BaseModel):
    hs_code: str = Field(..., min_length=4, description="Material HS8/HS6 code (e.g. 7208.54.10)")
    non_originating: bool = True
    origin_country: str | None = Field(default=None, description="ISO-2 or KB slug for regional cumulation (e.g. JP)")
    origin_proof_verified: bool | None = Field(default=None, description="True if partner origin proof is verified")

    def to_domain(self) -> MaterialInput:
        return MaterialInput(
            hs_code=self.hs_code.strip(),
            non_originating=self.non_originating,
            origin_country=self.origin_country.strip() if self.origin_country else None,
            origin_proof_verified=self.origin_proof_verified,
        )


class ShipmentRequestBase(BaseModel):
    hs_code: str = Field(..., min_length=4, description="Target product HS8 code (e.g. 7209.26.10)")
    dispatch_country: str = Field(..., min_length=2, description="Exporting country ISO-2 or KB slug (e.g. TH, CN, JP)")
    destination_country: str = Field(default="ID", description="Destination country (currently ID / indonesia)")
    as_of_date: str = Field(default="2025-09-15", description="Assessment date YYYY-MM-DD")
    cif_value: Decimal | None = Field(default=None, ge=0, description="Customs CIF value")
    currency: str = Field(default="USD", min_length=3, max_length=6)
    has_api: bool = Field(default=True, description="True if importer holds API-P or API-U (PPh 22 = 2.5% vs 7.5%)")
    api_type: str | None = Field(default=None, description="Optional importer API kind: API-P or API-U")
    materials: list[MaterialPayload] = Field(default_factory=list)
    rvc_percent: Decimal | None = Field(default=None, ge=0, le=100)
    wo_confirmed: bool | None = None
    process_kind: str | None = None
    process_satisfied: bool | None = None
    de_minimis_fob: tuple[Decimal, Decimal] | None = Field(
        default=None, description="[fob_value, failed_ctc_material_value]",
    )
    de_minimis_weight: tuple[Decimal, Decimal] | None = Field(
        default=None, description="[total_product_weight, failed_ctc_material_weight]",
    )
    performed_operations: list[str] | None = None
    dv20_percent: Decimal | None = Field(default=None, ge=0, le=100)
    highest_contributor_country: str | None = None
    provided_origin_docs: list[str] = Field(default_factory=list)
    provided_lartas_docs: list[str] = Field(default_factory=list)
    include_plan: bool = False
    include_explain: bool = False

    @field_validator("as_of_date")
    @classmethod
    def _validate_date(cls, value: str) -> str:
        text = value.strip()
        if not _ISO_DATE_RE.match(text):
            raise ValueError("as_of_date must use YYYY-MM-DD format.")
        return text

    def to_shipment_spec(self) -> ShipmentSpec:
        return ShipmentSpec(
            hs_code=self.hs_code.strip(),
            dispatch_country=self.dispatch_country.strip(),
            destination_country=self.destination_country.strip(),
            as_of_date=self.as_of_date,
            cif_value=self.cif_value,
            currency=self.currency.strip().upper(),
            has_api=self.has_api,
            api_type=self.api_type.strip() if self.api_type else None,
            materials=tuple(m.to_domain() for m in self.materials),
            rvc_percent=self.rvc_percent,
            wo_confirmed=self.wo_confirmed,
            process_kind=self.process_kind.strip() if self.process_kind else None,
            process_satisfied=self.process_satisfied,
            de_minimis_fob=self.de_minimis_fob,
            de_minimis_weight=self.de_minimis_weight,
            performed_operations=tuple(self.performed_operations) if self.performed_operations is not None else None,
            dv20_percent=self.dv20_percent,
            highest_contributor_country=(
                self.highest_contributor_country.strip() if self.highest_contributor_country else None
            ),
            provided_origin_docs=tuple(d.strip() for d in self.provided_origin_docs if d.strip()),
            provided_lartas_docs=tuple(d.strip() for d in self.provided_lartas_docs if d.strip()),
            include_plan=self.include_plan,
            include_explain=self.include_explain,
        )


class OptimizeRequest(ShipmentRequestBase):
    agreements: list[str] | None = Field(default=None, description="Optional agreement filter (e.g. ['atiga', 'rcep'])")


class AssessRequest(ShipmentRequestBase):
    pass


class ExportRequest(BaseModel):
    hs_code: str = Field(..., min_length=4, description="Outbound product HS8 code (e.g. 8001.10.00, 8507.60.33)")
    origin_country: str = Field(default="ID", description="Exporting country (currently ID / indonesia)")
    destination_country: str = Field(..., min_length=2, description="Destination country ISO-2 or slug (e.g. JP, KR, SG, CN, AU)")
    as_of_date: str = Field(default="2025-09-15", description="Export date YYYY-MM-DD")
    fob_value: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=6)
    quantity_mt: Decimal | None = Field(default=None, ge=0, description="Export volume in metric tons (MT) for Bea Keluar calculation")
    reference_price_usd: Decimal | None = Field(default=None, ge=0, description="Kemendag Harga Referensi (HR) in USD/MT for tiered Bea Keluar (CPO, palm oil, cocoa)")
    export_price_usd_per_mt: Decimal | None = Field(default=None, ge=0, description="DJBC Harga Ekspor (HPE) in USD/MT for ad-valorem Bea Keluar")
    materials: list[MaterialPayload] = Field(default_factory=list)
    rvc_percent: Decimal | None = Field(default=None, ge=0, le=100)
    wo_confirmed: bool | None = None
    process_kind: str | None = None
    process_satisfied: bool | None = None
    de_minimis_fob: tuple[Decimal, Decimal] | None = None
    de_minimis_weight: tuple[Decimal, Decimal] | None = None
    performed_operations: list[str] | None = None
    transshipment_hub: str | None = Field(default=None, description="Optional intermediate hub country ISO-2 or slug (e.g. SG)")
    requires_back_to_back: bool = Field(default=False, description="True if splitting/re-exporting under Back-to-Back CO / Movement Certificate")
    third_party_invoice_country: str | None = Field(default=None, description="Optional country issuing Third-Party / Non-Party Invoice")
    provided_export_docs: list[str] = Field(default_factory=list, description="Export docs already held (pebDeclaration, eksportirTerdaftar, persetujuanEkspor, laporanSurveyorEkspor)")
    include_explain: bool = False

    @field_validator("as_of_date")
    @classmethod
    def _validate_date(cls, value: str) -> str:
        text = value.strip()
        if not _ISO_DATE_RE.match(text):
            raise ValueError("as_of_date must use YYYY-MM-DD format.")
        return text

    def to_export_spec(self) -> ExportSpec:
        return ExportSpec(
            hs_code=self.hs_code.strip(),
            origin_country=self.origin_country.strip(),
            destination_country=self.destination_country.strip(),
            as_of_date=self.as_of_date,
            fob_value=self.fob_value,
            currency=self.currency.strip().upper(),
            quantity_mt=self.quantity_mt,
            reference_price_usd=self.reference_price_usd,
            export_price_usd_per_mt=self.export_price_usd_per_mt,
            materials=tuple(m.to_domain() for m in self.materials),
            rvc_percent=self.rvc_percent,
            wo_confirmed=self.wo_confirmed,
            process_kind=self.process_kind.strip() if self.process_kind else None,
            process_satisfied=self.process_satisfied,
            de_minimis_fob=self.de_minimis_fob,
            de_minimis_weight=self.de_minimis_weight,
            performed_operations=tuple(self.performed_operations) if self.performed_operations is not None else None,
            transshipment_hub=self.transshipment_hub.strip() if self.transshipment_hub else None,
            requires_back_to_back=self.requires_back_to_back,
            third_party_invoice_country=(
                self.third_party_invoice_country.strip() if self.third_party_invoice_country else None
            ),
            provided_export_docs=tuple(d.strip() for d in self.provided_export_docs if d.strip()),
            include_explain=self.include_explain,
        )


class BatchScreenRequest(BaseModel):
    mode: Literal["import", "export"] = Field(..., description="Screening direction: import (PIB) or export (PEB + Bea Keluar)")
    screening_depth: Literal["full", "fast_lartas"] = Field(
        default="fast_lartas",
        description="For import mode: 'fast_lartas' screens Lartas + MFN/FTA schedule taxes instantly; 'full' invokes OWL/HermiT RoO reasoning per row.",
    )
    include_explain: bool = Field(default=True, description="Include deduplicated primary legal citations in summary/Excel report")
    items: list[dict[str, object]] = Field(..., min_length=1, max_length=250, description="Shipment rows to screen")


class ImpactAnalyzeRequest(BaseModel):
    mode: Literal["import", "export"] = Field(..., description="Portfolio direction: import (PIB) or export (PEB + Bea Keluar)")
    baseline_date: str = Field(..., description="Baseline comparison date YYYY-MM-DD")
    target_date: str = Field(..., description="Target comparison date YYYY-MM-DD (must be > baseline_date)")
    screening_depth: Literal["fast_lartas", "full"] = Field(
        default="fast_lartas",
        description="For import mode: 'fast_lartas' compares Lartas + tariff/PMK revocations instantly; 'full' runs OWL/HermiT per row.",
    )
    include_explain: bool = Field(default=True, description="Include deduplicated primary legal citations")
    items: list[dict[str, object]] = Field(..., min_length=1, max_length=250, description="Portfolio shipment rows")

    @field_validator("baseline_date", "target_date")
    @classmethod
    def _validate_iso_date(cls, value: str) -> str:
        text = value.strip()
        if not _ISO_DATE_RE.match(text):
            raise ValueError("Dates must use YYYY-MM-DD format.")
        return text


class PersistCaseRequest(BaseModel):
    mode: Literal["import", "export"] = Field(..., description="Compliance case direction: import (PIB) or export (PEB)")
    reason: str | None = Field(default=None, description="Optional audit note recorded with the persisted snapshot")
    shipment: dict[str, object] = Field(..., description="Import OptimizeRequest or ExportRequest payload")


class MonitorScanRequest(BaseModel):
    targets: list[str] | None = Field(
        default=None,
        max_length=5,
        description="Bounded watchlist target IDs (max 5 per HTTP request, e.g. ['bea-keluar', 'lartas-ekspor'])",
    )
    portals: list[Literal["kemenkeu", "kemendag"]] | None = Field(
        default=None,
        description="Optional portal filter when targets is omitted",
    )
    custom_query: str | None = Field(default=None, min_length=2, max_length=120, description="Optional ad-hoc JDIH search query")
    custom_portal: Literal["kemenkeu", "kemendag"] = Field(default="kemenkeu")
    tahun: int | None = Field(default=None, ge=2000, le=2100)
    limit_per_target: int = Field(default=2, ge=1, le=5, description="Strict per-target scrape cap (1..5)")
    delay_seconds: float = Field(default=0.3, ge=0.0, le=5.0)
    as_of_date: str | None = Field(default=None, description="Reference date YYYY-MM-DD (defaults to today)")
    regulations: list[dict[str, object]] | None = Field(
        default=None,
        max_length=50,
        description="Optional offline replay regulation records; bypasses live JDIH network calls when provided",
    )
    generate_staging: bool = Field(default=True, description="Generate net-new delta OWL 2 DL staging Turtle and manifest patch preview")
    validate_merge: bool = Field(default=False, description="Validate trial merge against SHACL/KB compiler in memory")
    run_owl_validator: bool = Field(default=False, description="Also run Java OWLAPI/HermiT validator when validate_merge=True")

    @field_validator("as_of_date")
    @classmethod
    def _validate_optional_date(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        text = value.strip()
        if not _ISO_DATE_RE.match(text):
            raise ValueError("as_of_date must use YYYY-MM-DD format.")
        return text


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    email: str = Field(..., min_length=5, max_length=160)
    company: str = Field(default="", max_length=160)
    password: str = Field(..., min_length=6, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=160)
    password: str = Field(..., min_length=1, max_length=128)


class DemoLoginRequest(BaseModel):
    role: Literal["user", "admin"] = Field(default="user")


class RoleUpdateRequest(BaseModel):
    role: Literal["user", "admin"] = Field(...)
