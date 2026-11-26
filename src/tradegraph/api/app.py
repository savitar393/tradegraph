"""FastAPI application exposing TradeGraph Multi-FTA optimization, Lartas, regulatory lineage, and provenance."""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
import json
import logging
import os
from pathlib import Path
import re
from typing import Literal

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from rdflib import Literal as RDFLiteral, RDF, RDFS, URIRef

from tradegraph.api.schemas import (
    AssessRequest,
    BatchScreenRequest,
    CounterfactualRequest,
    DemoLoginRequest,
    DocumentBundleRequest,
    DocumentGenerateRequest,
    DocumentPrepareRequest,
    ExportRequest,
    HsClassifyRequest,
    ImpactAnalyzeRequest,
    LoginRequest,
    MonitorScanRequest,
    OptimizeRequest,
    PersistCaseRequest,
    RegimeConfigureRequest,
    RegisterRequest,
    RoleUpdateRequest,
    RooDiagnoseRequest,
    TemporaryImportRequest,
)
from tradegraph.auth.service import (
    AuthError,
    AuthService,
    DEMO_ADMIN_EMAIL,
    DEMO_ADMIN_PASSWORD,
    DEMO_CUSTOMER_EMAIL,
    DEMO_CUSTOMER_PASSWORD,
    UserProfile,
)
from tradegraph.cases.repository import CaseNotFound
from tradegraph.documents.service import DocumentGeneratorService
from tradegraph.graph.persistence import CasePersistenceService
from tradegraph.kb.loader import ENGINE_PROFILE_ID, TG, compile_kb
from tradegraph.kb.queries import prepare_local_query
from tradegraph.kb.validation import resolve_runtime_classpath
from tradegraph.optimization.batch_service import (
    BatchScreener,
    load_batch_rows_from_csv,
    load_batch_rows_from_xlsx,
)
from tradegraph.optimization.counterfactual_service import CounterfactualOptimizerService
from tradegraph.optimization.export_service import ExportComplianceAdvisor, ExportSpec
from tradegraph.optimization.hs_classifier_service import HsClassifierService
from tradegraph.optimization.impact_service import RegulatoryImpactAnalyzer
from tradegraph.optimization.regime_advisor_service import CustomsRegimeAdvisor
from tradegraph.optimization.service import MultiFtaOptimizer, ShipmentSpec
from tradegraph.optimization.temporary_import_service import TemporaryImportAdvisor
from tradegraph.reasoning.client import ReasoningClient
from tradegraph.sources.monitor import DEFAULT_WATCHLIST, RegulatoryMonitor


logger = logging.getLogger(__name__)


DEFAULT_KB_PATH = Path(__file__).resolve().parents[3] / "knowledge" / "tradegraph.ttl"
DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parents[3] / "sources" / "manifest.json"
_LOCAL_WORKBENCH = Path(__file__).resolve().parent / "workbench.html"
WORKBENCH_HTML_PATH = (
    _LOCAL_WORKBENCH
    if _LOCAL_WORKBENCH.is_file()
    else Path("/app/src/tradegraph/api/workbench.html")
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_PRESETS: tuple[dict[str, object], ...] = (
    {
        "id": "steel-rcep-dv20-lartas",
        "title": "Cold-Rolled Steel (TH → ID) — RCEP Cumulation + DV20 + Permendag 22/2025",
        "summary": "7209.26.10 using verified JP coil (7208.54.10), RVC 30%, missing DV20 & PI/LS → 4-step clearance plan.",
        "payload": {
            "hs_code": "7209.26.10",
            "dispatch_country": "TH",
            "destination_country": "ID",
            "as_of_date": "2025-09-15",
            "cif_value": "100000",
            "currency": "USD",
            "has_api": True,
            "api_type": "API-P",
            "materials": [
                {"hs_code": "7208.54.10", "non_originating": True, "origin_country": "JP", "origin_proof_verified": True}
            ],
            "rvc_percent": "30.00",
            "highest_contributor_country": "japan",
            "provided_origin_docs": [],
            "provided_lartas_docs": [],
            "include_plan": True,
            "include_explain": True,
        },
    },
    {
        "id": "dishwasher-cn-roo-switch",
        "title": "Household Dishwasher (CN → ID) — Low RVC Switches ACFTA → RCEP",
        "summary": "8422.11.00 (Unrestricted): ACFTA 0% fails RVC40 at 30%; RCEP 6% passes via CTSH and saves $4,540.",
        "payload": {
            "hs_code": "8422.11.00",
            "dispatch_country": "CN",
            "destination_country": "ID",
            "as_of_date": "2025-09-15",
            "cif_value": "100000",
            "currency": "USD",
            "has_api": True,
            "api_type": "API-U",
            "materials": [{"hs_code": "8422.90.10", "non_originating": True}],
            "rvc_percent": "30.00",
            "provided_origin_docs": [],
            "provided_lartas_docs": [],
            "include_plan": True,
            "include_explain": True,
        },
    },
    {
        "id": "penicillin-jp-bpom-revocation",
        "title": "Penicillin G (JP → ID, Aug 2026) — PMK 60/2026 Revocation Alert + BPOM 28/2023",
        "summary": "3004.10.15: IJEPA 0% triggers PMK revocation alert, BPOM SKI Border + Izin Edar required.",
        "payload": {
            "hs_code": "3004.10.15",
            "dispatch_country": "JP",
            "destination_country": "ID",
            "as_of_date": "2026-08-15",
            "cif_value": "50000",
            "currency": "USD",
            "has_api": True,
            "api_type": "API-P",
            "materials": [{"hs_code": "3004.10.19", "non_originating": True}],
            "rvc_percent": "45.00",
            "process_kind": "criterionChemicalReaction",
            "process_satisfied": True,
            "provided_origin_docs": ["ijepaFormJiepa", "rcepFormRcep"],
            "provided_lartas_docs": [],
            "include_plan": True,
            "include_explain": True,
        },
    },
    {
        "id": "pump-cn-2026-partial-revocation",
        "title": "Centrifugal Pump (CN → ID, 2026) — RCEP 2% Beats ACFTA 5% + Permendag 21/2025 Active",
        "summary": "8413.70.11: Permendag 47/2025 Pasal 8(b) partial revocation keeps LS BORDER active; CLEAR when LS provided.",
        "payload": {
            "hs_code": "8413.70.11",
            "dispatch_country": "CN",
            "destination_country": "ID",
            "as_of_date": "2026-06-15",
            "cif_value": "40000",
            "currency": "USD",
            "has_api": True,
            "api_type": "API-U",
            "materials": [{"hs_code": "8413.91.30", "non_originating": True}],
            "rvc_percent": "45.00",
            "provided_origin_docs": ["rcepFormRcep"],
            "provided_lartas_docs": ["laporanSurveyor"],
            "include_plan": False,
            "include_explain": True,
        },
    },
    {
        "id": "used-clothing-prohibited-2026",
        "title": "Used Clothing (TH → ID, 2026) — Permendag 47/2025 PROHIBITED Hard-Block",
        "summary": "6309.00.00 banned eff. 2026-01-01 under Permendag 47/2025 Lampiran IV; blocks all FTA claims.",
        "payload": {
            "hs_code": "6309.00.00",
            "dispatch_country": "TH",
            "destination_country": "ID",
            "as_of_date": "2026-06-15",
            "cif_value": "20000",
            "currency": "USD",
            "has_api": True,
            "api_type": "API-U",
            "materials": [],
            "provided_origin_docs": [],
            "provided_lartas_docs": [],
            "include_plan": True,
            "include_explain": True,
        },
    },
    {
        "id": "ev-battery-kr-ikcepa-vs-rcep",
        "title": "EV Li-Ion Battery (KR → ID) — IK-CEPA 0% vs RCEP 6% RoO Switch",
        "summary": "8507.60.33 with 8507.90.92 separators: at RVC 30% IK-CEPA fails CTH/RVC40 so RCEP CTSH wins; at RVC 45% IK-CEPA 0% wins!",
        "payload": {
            "hs_code": "8507.60.33",
            "dispatch_country": "KR",
            "destination_country": "ID",
            "as_of_date": "2025-09-15",
            "cif_value": "100000",
            "currency": "USD",
            "has_api": True,
            "api_type": "API-P",
            "materials": [{"hs_code": "8507.90.92", "non_originating": True}],
            "rvc_percent": "30.00",
            "provided_origin_docs": [],
            "provided_lartas_docs": [],
            "include_plan": True,
            "include_explain": True,
        },
    },
    {
        "id": "steel-au-iacepa-beats-rcep",
        "title": "Cold-Rolled Steel (AU → ID) — IA-CEPA 4.5% Beats RCEP 6.6% + Skips DV20",
        "summary": "7209.26.10 (Permendag 22/2025 PI+LS): IA-CEPA (PIB 65) at 4.50% beats RCEP Australia (6.60% + DV20) and saves $11,917.50.",
        "payload": {
            "hs_code": "7209.26.10",
            "dispatch_country": "AU",
            "destination_country": "ID",
            "as_of_date": "2025-09-15",
            "cif_value": "100000",
            "currency": "USD",
            "has_api": True,
            "api_type": "API-P",
            "materials": [{"hs_code": "7208.10.00", "non_originating": True}],
            "rvc_percent": "45.00",
            "provided_origin_docs": ["iacepaOriginDeclaration"],
            "provided_lartas_docs": [],
            "include_plan": True,
            "include_explain": True,
        },
    },
)

_EXPORT_PRESETS: tuple[dict[str, object], ...] = (
    {
        "id": "export-tin-jp-sg-back-to-back",
        "title": "Refined Tin (ID → JP via SG Hub) — Back-to-Back RCEP vs Blocked IJEPA + ET/PE/LS",
        "summary": "8001.10.00: Permendag 9/2025 requires ET+PE+LS; IJEPA blocks Back-to-Back CO, so RCEP via SG hub + HK Third-Party Invoice wins.",
        "payload": {
            "hs_code": "8001.10.00",
            "origin_country": "ID",
            "destination_country": "JP",
            "as_of_date": "2025-09-15",
            "fob_value": "250000",
            "currency": "USD",
            "rvc_percent": "65.00",
            "transshipment_hub": "SG",
            "requires_back_to_back": True,
            "third_party_invoice_country": "HK",
            "provided_export_docs": [],
            "include_explain": True,
        },
    },
    {
        "id": "export-nickel-ore-prohibited",
        "title": "Nickel Ore (ID → CN) — Permendag 8/2025 PROHIBITED_EXPORT Ban",
        "summary": "2604.00.00 banned under Permendag 8/2025 Lampiran row 228; blocks PEB and all FTA origin certificates.",
        "payload": {
            "hs_code": "2604.00.00",
            "origin_country": "ID",
            "destination_country": "CN",
            "as_of_date": "2025-09-15",
            "fob_value": "500000",
            "currency": "USD",
            "wo_confirmed": True,
            "provided_export_docs": [],
            "include_explain": True,
        },
    },
    {
        "id": "export-ev-battery-kr-cumulation",
        "title": "EV Li-Ion Battery (ID → KR) — Regional Cumulation Sourcing Optimizer",
        "summary": "8507.60.33 with non-originating 8507.90.92 separators (RVC 30%): IK-CEPA fails CTH & recommends KR cumulation; RCEP passes CTSH.",
        "payload": {
            "hs_code": "8507.60.33",
            "origin_country": "ID",
            "destination_country": "KR",
            "as_of_date": "2025-09-15",
            "fob_value": "150000",
            "currency": "USD",
            "materials": [{"hs_code": "8507.90.92", "non_originating": True, "origin_country": "CN", "origin_proof_verified": False}],
            "rvc_percent": "30.00",
            "third_party_invoice_country": "SG",
            "provided_export_docs": [],
            "include_explain": True,
        },
    },
    {
        "id": "export-coal-cn-et-ls",
        "title": "Anthracite Coal (ID → CN) — Permendag 9/2025 ET+LS + ACFTA/RCEP",
        "summary": "2701.11.00 requires ET+LS (no PE); wholly obtained coal qualifies under both ACFTA and RCEP.",
        "payload": {
            "hs_code": "2701.11.00",
            "origin_country": "ID",
            "destination_country": "CN",
            "as_of_date": "2025-09-15",
            "fob_value": "400000",
            "currency": "USD",
            "wo_confirmed": True,
            "rvc_percent": "90.00",
            "provided_export_docs": ["eksportirTerdaftar"],
            "include_explain": True,
        },
    },
    {
        "id": "export-cpo-cn-bea-keluar",
        "title": "Crude Palm Oil / CPO (ID → CN) — PMK 68/2025 Specific Bea Keluar (USD 124/MT)",
        "summary": "1511.10.00 at Kemendag Reference Price USD 950/MT (Col. 7): USD 124/MT x 1,000 MT = USD 124,000 Bea Keluar + RCEP/ACFTA WO.",
        "payload": {
            "hs_code": "1511.10.00",
            "origin_country": "ID",
            "destination_country": "CN",
            "as_of_date": "2025-11-15",
            "fob_value": "950000",
            "currency": "USD",
            "quantity_mt": "1000",
            "reference_price_usd": "950.00",
            "wo_confirmed": True,
            "rvc_percent": "95.00",
            "provided_export_docs": [],
            "include_explain": True,
        },
    },
    {
        "id": "export-cocoa-pmk68-rate-cut",
        "title": "Cocoa Beans (ID → MY) — PMK 68/2025 50% Bea Keluar Cut (15% → 7.5%)",
        "summary": "1801.00.10 at HR USD 4,000/MT (Col. 4): PMK 68/2025 cuts Bea Keluar from 15% (PMK 38/2024) to 7.5% effective 2025-10-22.",
        "payload": {
            "hs_code": "1801.00.10",
            "origin_country": "ID",
            "destination_country": "MY",
            "as_of_date": "2025-11-15",
            "fob_value": "200000",
            "currency": "USD",
            "quantity_mt": "50",
            "reference_price_usd": "4000.00",
            "export_price_usd_per_mt": "4000.00",
            "wo_confirmed": True,
            "rvc_percent": "95.00",
            "provided_export_docs": [],
            "include_explain": True,
        },
    },
)

_BATCH_SAMPLES: dict[str, dict[str, object]] = {
    "export": {
        "title": "Sample Indonesian Export Portfolio (5 SKUs — CPO, Cocoa, Copper, Tin Back-to-Back, Nickel Ban)",
        "mode": "export",
        "csv": "\n".join([
            "sku,hs_code,destination_country,as_of_date,fob_value,quantity_mt,reference_price_usd,export_price_usd_per_mt,rvc_percent,wo_confirmed,transshipment_hub,requires_back_to_back,third_party_invoice_country,provided_export_docs",
            "EXP-CPO-01,1511.10.00,CN,2025-11-15,950000,1000,950,,95,true,,,,",
            "EXP-COCOA-02,1801.00.10,MY,2025-11-15,200000,50,4000,4000,95,true,,,,",
            "EXP-CU-03,2603.00.00,JP,2025-11-15,600000,300,,2000,80,true,,,,",
            "EXP-TIN-04,8001.10.00,JP,2025-11-15,250000,,,,65,,SG,true,HK,",
            "EXP-NI-05,2604.00.00,CN,2025-11-15,500000,5000,,,100,true,,,,",
        ]),
    },
    "import": {
        "title": "Sample Indonesian Import Manifest (5 SKUs — Steel, EV Battery, Pump, Antibiotic, Worn Clothing Ban)",
        "mode": "import",
        "csv": "\n".join([
            "sku,hs_code,dispatch_country,as_of_date,cif_value,api_type,rvc_percent,provided_lartas_docs",
            "IMP-STL-01,7209.26.10,AU,2025-09-15,100000,API-P,45,",
            "IMP-EVB-02,8507.60.33,KR,2025-09-15,150000,API-P,45,",
            "IMP-PMP-03,8413.70.11,CN,2026-06-15,40000,API-U,45,laporanSurveyor",
            "IMP-MED-04,3004.10.15,MY,2025-09-15,60000,API-U,50,",
            "IMP-BAN-05,6309.00.00,TH,2026-06-15,20000,API-U,50,",
        ]),
    },
}

_IMPACT_SCENARIOS: tuple[dict[str, object], ...] = (
    {
        "id": "pmk68-2025-bea-keluar-shock",
        "title": "PMK 68/2025 Export Duty Shock (2025-09-15 → 2025-11-15)",
        "summary": "Cocoa Beans BK cut 50% (15%→7.5%, -$15k), Getah Pinus newly taxed 25% (+$25k), CPO Lampiran C updated.",
        "mode": "export",
        "baseline_date": "2025-09-15",
        "target_date": "2025-11-15",
        "screening_depth": "fast_lartas",
        "csv": "\n".join([
            "sku,hs_code,destination_country,fob_value,quantity_mt,reference_price_usd,export_price_usd_per_mt,wo_confirmed",
            "COCOA-01,1801.00.10,MY,200000,50,4000,4000,true",
            "PINE-02,1301.90.90,SG,100000,,,,true",
            "CPO-03,1511.10.00,CN,950000,1000,950,,true",
            "COPPER-04,2603.00.00,JP,600000,300,,2000,true",
        ]),
    },
    {
        "id": "import-2026-permendag47-rcep-staging",
        "title": "2026 Import Shock: Permendag 47/2025 Ban + RCEP Tariff Cuts (2025-09-15 → 2026-06-15)",
        "summary": "6309.00.00 turns PROHIBITED (Permendag 47/2025); RCEP 2026 staging cuts duties on pumps, steel & dishwashers.",
        "mode": "import",
        "baseline_date": "2025-09-15",
        "target_date": "2026-06-15",
        "screening_depth": "fast_lartas",
        "csv": "\n".join([
            "sku,hs_code,dispatch_country,cif_value,api_type,agreements,provided_lartas_docs",
            "BAN-WORN-01,6309.00.00,TH,20000,API-U,,",
            "PUMP-CN-02,8413.70.11,CN,100000,API-U,rcep,laporanSurveyor",
            "STEEL-TH-03,7209.26.10,TH,100000,API-P,rcep,",
            "DISH-CN-04,8422.11.00,CN,50000,API-U,rcep,",
        ]),
    },
    {
        "id": "pmk60-2026-ijepa-revocation-cliff",
        "title": "PMK 60/2026 IJEPA Revocation Cliff (2026-06-15 → 2026-08-15)",
        "summary": "PMK 60/2026 revokes PMK 50/PMK.010/2022 (IJEPA tariff) effective 2026-08-01; flags JP routes as BREAKING.",
        "mode": "import",
        "baseline_date": "2026-06-15",
        "target_date": "2026-08-15",
        "screening_depth": "fast_lartas",
        "csv": "\n".join([
            "sku,hs_code,dispatch_country,cif_value,api_type,agreements,provided_lartas_docs",
            "PUMP-JP-01,8413.70.11,JP,80000,API-P,ijepa,laporanSurveyor",
            "PUMP-CN-02,8413.70.11,CN,80000,API-P,rcep,laporanSurveyor",
        ]),
    },
)

_SCREENING_PRESETS: tuple[dict[str, object], ...] = (
    {
        "id": "screen-export-nickel-prohibited",
        "title": "Ekspor Bijih Nikel (2604.00.00) — DILARANG EKSPOR (Permendag 8/2025)",
        "direction": "export",
        "payload": {"hs_code": "2604.00.00", "as_of_date": "2025-09-15", "provided_docs": []},
    },
    {
        "id": "screen-export-tin-permits",
        "title": "Ekspor Timah Murni (8001.10.00) — Boleh dengan Izin ET + PE + LS + PEB",
        "direction": "export",
        "payload": {"hs_code": "8001.10.00", "as_of_date": "2025-09-15", "provided_docs": []},
    },
    {
        "id": "screen-import-worn-clothing-prohibited",
        "title": "Impor Pakaian Bekas (6309.00.00, 2026) — DILARANG IMPOR (Permendag 47/2025)",
        "direction": "import",
        "payload": {"hs_code": "6309.00.00", "as_of_date": "2026-06-15", "api_type": "API-U", "provided_docs": []},
    },
    {
        "id": "screen-import-steel-border",
        "title": "Impor Baja Canai Dingin (7209.26.10) — Wajib Izin PI + LS Border (Permendag 22/2025)",
        "direction": "import",
        "payload": {"hs_code": "7209.26.10", "as_of_date": "2025-09-15", "api_type": "API-P", "provided_docs": []},
    },
    {
        "id": "screen-import-dishwasher-clear",
        "title": "Impor Mesin Pencuci Piring (8422.11.00) — BEBAS LARTAS (Langsung Boleh)",
        "direction": "import",
        "payload": {"hs_code": "8422.11.00", "as_of_date": "2025-09-15", "api_type": "API-U", "provided_docs": []},
    },
)

_ROO_PRESETS: tuple[dict[str, object], ...] = (
    {
        "id": "roo-ev-battery-kr-switch",
        "title": "Baterai EV (KR → ID) — RVC 30% Gagal IK-CEPA (CTH/RVC40), Lolos RCEP (CTSH)",
        "payload": {
            "direction": "import",
            "hs_code": "8507.60.33",
            "partner_country": "KR",
            "as_of_date": "2025-09-15",
            "materials": [{"hs_code": "8507.90.92", "non_originating": True, "origin_country": "CN", "origin_proof_verified": False}],
            "rvc_percent": "30.00",
            "include_explain": True,
        },
    },
    {
        "id": "roo-steel-th-cumulation-dv20",
        "title": "Baja Canai Dingin (TH → ID) — Kumulasi JP RCEP + Uji DV20 Lampiran II",
        "payload": {
            "direction": "import",
            "hs_code": "7209.26.10",
            "partner_country": "TH",
            "as_of_date": "2025-09-15",
            "materials": [{"hs_code": "7208.54.10", "non_originating": True, "origin_country": "JP", "origin_proof_verified": True}],
            "rvc_percent": "30.00",
            "highest_contributor_country": "japan",
            "include_explain": True,
        },
    },
    {
        "id": "roo-dishwasher-cn-ctsh",
        "title": "Mesin Cuci Piring (CN → ID) — RVC 30% Gagal ACFTA, Lolos RCEP via CTSH",
        "payload": {
            "direction": "import",
            "hs_code": "8422.11.00",
            "partner_country": "CN",
            "as_of_date": "2025-09-15",
            "materials": [{"hs_code": "8422.90.10", "non_originating": True}],
            "rvc_percent": "30.00",
            "include_explain": True,
        },
    },
    {
        "id": "roo-export-ev-battery-kr-cumulation",
        "title": "Ekspor Baterai EV (ID → KR) — Diagnostik Kumulasi Bahan Baku Separator",
        "payload": {
            "direction": "export",
            "hs_code": "8507.60.33",
            "partner_country": "KR",
            "as_of_date": "2025-09-15",
            "materials": [{"hs_code": "8507.90.92", "non_originating": True, "origin_country": "CN", "origin_proof_verified": False}],
            "rvc_percent": "30.00",
            "third_party_invoice_country": "SG",
            "include_explain": True,
        },
    },
)

_TEMPORARY_IMPORT_PRESETS: tuple[dict[str, object], ...] = (
    {
        "id": "temp-exhibition-complete",
        "title": "Pameran Pompa Sentrifugal (TH → ID) — Lengkap & Bebas Lartas Umum (Pasal 47)",
        "summary": "HS 8413.70.11 untuk pameran dengan izin, jaminan & rencana ekspor kembali; pengecualian kendali impor umum aktif.",
        "payload": {
            "hs_code": "8413.70.11",
            "dispatch_country": "TH",
            "destination_country": "ID",
            "as_of_date": "2025-09-15",
            "procedure": "reExportTemporaryImport",
            "intended_use": "exhibitionUse",
            "proposed_use": "exhibitionUse",
            "requested_settlement": "reexportSettlement",
            "identifiable_for_reexport": True,
            "consumed_in_use": False,
            "essential_change_in_use": False,
            "permit_end_date": "2025-10-15",
            "provided_docs": [
                "temporaryImportPermit",
                "temporaryImportGuarantee",
                "reexportPlan",
                "temporaryReexportNotice",
                "temporaryReexportRealization",
            ],
            "notice_date": "2025-10-10",
            "realization_date": "2025-11-02",
            "include_plan": True,
            "include_explain": True,
        },
    },
    {
        "id": "temp-exhibition-plan-needed",
        "title": "Pameran Pompa — Belum Ajukan Pemberitahuan Ekspor Kembali (Planning 2 Langkah)",
        "summary": "Izin & jaminan sudah ada, sistem menyusun rencana 2 langkah persiapan ekspor kembali (PMK 178/2017 Pasal 17).",
        "payload": {
            "hs_code": "8413.70.11",
            "dispatch_country": "TH",
            "destination_country": "ID",
            "as_of_date": "2025-09-15",
            "procedure": "temporaryImport",
            "intended_use": "exhibitionUse",
            "proposed_use": "exhibitionUse",
            "requested_settlement": "reexportSettlement",
            "identifiable_for_reexport": True,
            "consumed_in_use": False,
            "essential_change_in_use": False,
            "permit_end_date": "2025-10-15",
            "provided_docs": ["temporaryImportPermit", "temporaryImportGuarantee", "reexportPlan"],
            "include_plan": True,
            "include_explain": True,
        },
    },
    {
        "id": "temp-exhibition-change-conference",
        "title": "Perubahan Tujuan Pameran → Konferensi (Disetujui Sebelum Izin Berakhir)",
        "summary": "Menunjukkan penalaran temporal PMK 178/2017 Pasal 16 atas persetujuan perubahan tujuan dalam periode izin.",
        "payload": {
            "hs_code": "8413.70.11",
            "dispatch_country": "TH",
            "destination_country": "ID",
            "as_of_date": "2025-09-15",
            "procedure": "temporaryImport",
            "intended_use": "exhibitionUse",
            "proposed_use": "conferenceUse",
            "requested_settlement": "reexportSettlement",
            "identifiable_for_reexport": True,
            "consumed_in_use": False,
            "essential_change_in_use": False,
            "permit_end_date": "2025-10-15",
            "provided_docs": [
                "temporaryImportPermit",
                "temporaryImportGuarantee",
                "reexportPlan",
                "changeOfUseApproval",
            ],
            "change_approval_date": "2025-10-01",
            "include_plan": True,
            "include_explain": True,
        },
    },
    {
        "id": "temp-exhibition-commercial-sale-blocked",
        "title": "Penjualan Komersial Biasa — DITOLAK (Melanggar Syarat Penyelesaian Domestik)",
        "summary": "Barang pameran tidak boleh dialihkan untuk penjualan komersial biasa tanpa dasar penyelesaian sah.",
        "payload": {
            "hs_code": "8413.70.11",
            "dispatch_country": "TH",
            "destination_country": "ID",
            "as_of_date": "2025-09-15",
            "procedure": "temporaryImport",
            "intended_use": "exhibitionUse",
            "proposed_use": "commercialResaleUse",
            "requested_settlement": "domesticSettlement",
            "settlement_ground": "ordinaryCommercialTransfer",
            "identifiable_for_reexport": True,
            "consumed_in_use": False,
            "essential_change_in_use": False,
            "permit_end_date": "2025-10-15",
            "provided_docs": ["temporaryImportPermit", "temporaryImportGuarantee", "reexportPlan"],
            "include_plan": False,
            "include_explain": True,
        },
    },
    {
        "id": "temp-reexport-late-violation",
        "title": "Ekspor Kembali Terlambat (> H+30) — Melanggar Tenggat PMK 178/2017 Pasal 17",
        "summary": "Pemberitahuan lewat tanggal akhir izin (2025-10-20) & realisasi lewat H+30 (2025-11-30) -> NOT_SATISFIED.",
        "payload": {
            "hs_code": "8413.70.11",
            "dispatch_country": "TH",
            "destination_country": "ID",
            "as_of_date": "2025-09-15",
            "procedure": "reExportTemporaryImport",
            "intended_use": "exhibitionUse",
            "proposed_use": "exhibitionUse",
            "requested_settlement": "reexportSettlement",
            "identifiable_for_reexport": True,
            "consumed_in_use": False,
            "essential_change_in_use": False,
            "permit_end_date": "2025-10-15",
            "provided_docs": [
                "temporaryImportPermit",
                "temporaryImportGuarantee",
                "reexportPlan",
                "temporaryReexportNotice",
                "temporaryReexportRealization",
            ],
            "notice_date": "2025-10-20",
            "realization_date": "2025-11-30",
            "include_plan": False,
            "include_explain": True,
        },
    },
    {
        "id": "temp-consumable-fail",
        "title": "Barang Habis Pakai — Gagal Syarat Fisik Impor Sementara (PMK 178 Pasal 2)",
        "summary": "Barang yang habis dipakai selama penggunaan tidak memenuhi kriteria fisik Impor Sementara.",
        "payload": {
            "hs_code": "8413.70.11",
            "dispatch_country": "TH",
            "destination_country": "ID",
            "as_of_date": "2025-09-15",
            "procedure": "temporaryImport",
            "intended_use": "exhibitionUse",
            "proposed_use": "exhibitionUse",
            "requested_settlement": "reexportSettlement",
            "identifiable_for_reexport": True,
            "consumed_in_use": True,
            "essential_change_in_use": False,
            "permit_end_date": "2025-10-15",
            "provided_docs": ["temporaryImportPermit", "temporaryImportGuarantee", "reexportPlan"],
            "include_plan": False,
            "include_explain": True,
        },
    },
    {
        "id": "temp-outside-coverage",
        "title": "Uji Batas Cakupan KB (Tanggal 2025-09-16) — OUT_OF_COVERAGE (Open-World)",
        "summary": "Menunjukkan kejujuran sistem KBS: di luar cakupan regulasi terverifikasi, sistem melapor OUT_OF_COVERAGE.",
        "payload": {
            "hs_code": "8413.70.11",
            "dispatch_country": "TH",
            "destination_country": "ID",
            "as_of_date": "2025-09-16",
            "procedure": "temporaryImport",
            "intended_use": "exhibitionUse",
            "proposed_use": "exhibitionUse",
            "requested_settlement": "reexportSettlement",
            "identifiable_for_reexport": True,
            "consumed_in_use": False,
            "essential_change_in_use": False,
            "permit_end_date": "2025-10-15",
            "provided_docs": ["temporaryImportPermit", "temporaryImportGuarantee", "reexportPlan"],
            "include_plan": False,
            "include_explain": True,
        },
    },
)

_HS_CLASSIFIER_PRESETS: tuple[dict[str, object], ...] = (
    {
        "id": "hs-steel-cold-rolled",
        "title": "Baja Gulungan Canai Dingin Industri — Bedakan HRC vs CRC vs Tinplate",
        "payload": {"query_text": "baja gulungan", "sector": "steel", "processing_state": "cold_rolled"},
    },
    {
        "id": "hs-ev-battery-disambig",
        "title": "Baterai Lithium-Ion — Bedakan Baterai EV vs Non-EV vs Separator",
        "payload": {"query_text": "baterai lithium", "sector": "electronics"},
    },
    {
        "id": "hs-palm-oil-disambig",
        "title": "Produk Minyak Sawit — Bedakan CPO Mentah vs RBD Palm Olein",
        "payload": {"query_text": "sawit", "sector": "agro"},
    },
    {
        "id": "hs-secondhand-prohibited",
        "title": "Pakaian Bekas / Thrifting — Klasifikasi & Deteksi Larangan Impor",
        "payload": {"query_text": "pakaian bekas", "sector": "textile", "processing_state": "used_secondhand", "as_of_date": "2026-06-15"},
    },
)

_COUNTERFACTUAL_PRESETS: tuple[dict[str, object], ...] = (
    {
        "id": "cf-ev-battery-kr-unlock-ikcepa",
        "title": "Baterai EV (KR → ID, RVC 30%) — Simulasi Buka IK-CEPA 0% vs RCEP 6%",
        "summary": "HermiT memverifikasi intervensi kumulasi KR & kenaikan RVC +10% untuk memangkas BM dari 6% menjadi 0%.",
        "payload": {
            "direction": "import",
            "hs_code": "8507.60.33",
            "partner_country": "KR",
            "as_of_date": "2025-09-15",
            "cif_or_fob_value": "100000",
            "api_type": "API-P",
            "materials": [{"hs_code": "8507.90.92", "non_originating": True, "origin_country": "CN", "origin_proof_verified": False}],
            "rvc_percent": "30.00",
        },
    },
    {
        "id": "cf-dishwasher-cn-unlock-acfta",
        "title": "Mesin Cuci Piring (CN → ID, RVC 30%) — Simulasi Buka ACFTA 0% vs RCEP 6%",
        "summary": "HermiT menguji peningkatan RVC +10% untuk membuka tarif ACFTA 0% dan menghitung penghematan USD bersih.",
        "payload": {
            "direction": "import",
            "hs_code": "8422.11.00",
            "partner_country": "CN",
            "as_of_date": "2025-09-15",
            "cif_or_fob_value": "100000",
            "api_type": "API-U",
            "materials": [{"hs_code": "8422.90.10", "non_originating": True}],
            "rvc_percent": "30.00",
        },
    },
    {
        "id": "cf-steel-th-dv20-repair",
        "title": "Baja Canai Dingin (TH → ID) — Simulasi Perbaikan RCEP DV20 ≥ 20% FOB",
        "summary": "Saat DV20 awal hanya 12% FOB (tarif beralih ke JP), HermiT menguji pemenuhan DV20 ≥ 20% FOB.",
        "payload": {
            "direction": "import",
            "hs_code": "7209.26.10",
            "partner_country": "TH",
            "as_of_date": "2025-09-15",
            "cif_or_fob_value": "100000",
            "api_type": "API-P",
            "materials": [{"hs_code": "7208.54.10", "non_originating": True, "origin_country": "JP", "origin_proof_verified": True}],
            "rvc_percent": "30.00",
            "dv20_percent": "12.00",
            "highest_contributor_country": "japan",
        },
    },
    {
        "id": "cf-export-tin-direct-vs-b2b",
        "title": "Ekspor Timah (ID → JP via SG Back-to-Back) — Simulasi Buka IJEPA Langsung",
        "summary": "HermiT memverifikasi pengalihan dari Back-to-Back transit ke pengiriman langsung untuk membuka Form JIEPA.",
        "payload": {
            "direction": "export",
            "hs_code": "8001.10.00",
            "partner_country": "JP",
            "as_of_date": "2025-09-15",
            "cif_or_fob_value": "250000",
            "rvc_percent": "65.00",
            "transshipment_hub": "SG",
            "requires_back_to_back": True,
            "third_party_invoice_country": "HK",
        },
    },
)

_REGIME_PRESETS: tuple[dict[str, object], ...] = (
    {
        "id": "regime-steel-kite-export-mfg",
        "title": "Baja Bahan Baku Olah-Ekspor (API-P / KITE) — KITE Pembebasan vs Kawasan Berikat vs PIB",
        "payload": {
            "hs_code": "7209.26.10",
            "as_of_date": "2025-09-15",
            "direction": "import",
            "trade_purpose": "manufacturing_for_export",
            "reexport_plan": "PROCESSED_EXPORT",
            "company_license": "KITE_HOLDER",
            "customs_value_usd": "100000",
            "has_fta_partner": True,
        },
    },
    {
        "id": "regime-pump-exhibition-temp",
        "title": "Pompa Sentrifugal untuk Pameran (Ekspor Kembali Utuh) — Impor Sementara Bebas Lartas Pasal 47",
        "payload": {
            "hs_code": "8413.70.11",
            "as_of_date": "2025-09-15",
            "direction": "import",
            "trade_purpose": "exhibition_event",
            "reexport_plan": "SAME_CONDITION_REEXPORT",
            "company_license": "API-U",
            "identifiable_for_reexport": True,
            "consumed_in_use": False,
            "essential_change_in_use": False,
            "customs_value_usd": "40000",
            "has_fta_partner": True,
        },
    },
    {
        "id": "regime-ev-battery-bonded-zone",
        "title": "Komponen Baterai EV di Kawasan Berikat (PKB) — Penangguhan BM & Penundaan Lartas BC 2.3",
        "payload": {
            "hs_code": "8507.90.92",
            "as_of_date": "2025-09-15",
            "direction": "import",
            "trade_purpose": "industrial_bonded_zone",
            "reexport_plan": "PROCESSED_EXPORT",
            "company_license": "KB_OPERATOR",
            "customs_value_usd": "150000",
            "has_fta_partner": True,
        },
    },
    {
        "id": "regime-dishwasher-local-fta",
        "title": "Mesin Cuci Piring untuk Distribusi Lokal — Impor Umum PIB BC 2.0 + Preferensi FTA",
        "payload": {
            "hs_code": "8422.11.00",
            "as_of_date": "2025-09-15",
            "direction": "import",
            "trade_purpose": "commercial_trading",
            "reexport_plan": "NO_REEXPORT",
            "company_license": "API-U",
            "customs_value_usd": "100000",
            "has_fta_partner": True,
        },
    },
    {
        "id": "regime-prohibited-hard-block",
        "title": "Pakaian Bekas (6309.00.00, 2026) — Seluruh Rezim Fasilitas Impor Diblokir",
        "payload": {
            "hs_code": "6309.00.00",
            "as_of_date": "2026-06-15",
            "direction": "import",
            "trade_purpose": "exhibition_event",
            "reexport_plan": "SAME_CONDITION_REEXPORT",
            "company_license": "API-U",
            "customs_value_usd": "20000",
            "has_fta_partner": True,
        },
    },
)


def _build_catalog(optimizer: MultiFtaOptimizer) -> dict[str, object]:
    owl = optimizer.kb.dataset.graph(optimizer.kb.owl_projection_ref)

    featured_hs: list[dict[str, object]] = []
    all_hs: list[dict[str, str]] = []
    for subj in sorted(owl.subjects(RDF.type, TG.HSCode), key=str):
        if not isinstance(subj, URIRef):
            continue
        code = owl.value(subj, TG.codeValue)
        if code is None:
            continue
        code_str = str(code)
        label = str(owl.value(subj, RDFS.label) or code_str)
        all_hs.append({"code": code_str, "label": label})

        mfn = owl.value(subj, TG.mfnDutyPercent)
        lartas_rules = list(owl.subjects(TG.lartasTargetCode, subj))
        export_rules = list(owl.subjects(TG.exportLartasTargetCode, subj))
        export_duty_rules = list(owl.subjects(TG.exportDutyTargetCode, subj))
        if mfn is not None or lartas_rules or export_rules or export_duty_rules:
            modes = sorted({
                str(owl.value(owl.value(r, TG.lartasSupervisionMode), RDFS.label) or "RESTRICTED")
                for r in lartas_rules
                if owl.value(r, TG.lartasSupervisionMode) is not None
            })
            export_modes = sorted({
                str(owl.value(owl.value(r, TG.exportLartasSupervisionMode), RDFS.label) or "RESTRICTED_EXPORT")
                for r in export_rules
                if owl.value(r, TG.exportLartasSupervisionMode) is not None
            })
            featured_hs.append({
                "code": code_str,
                "iri": str(subj),
                "label": label,
                "mfn_duty_percent": str(mfn) if mfn is not None else None,
                "requires_rcep_dv20": str(owl.value(subj, TG.subjectToRcepTariffDifferential)).lower() == "true",
                "lartas_modes": modes,
                "export_lartas_modes": export_modes,
                "export_duty_applicable": bool(export_duty_rules),
            })
    featured_hs.sort(key=lambda item: str(item["code"]))
    all_hs.sort(key=lambda item: item["code"])

    countries = sorted(
        (
            {
                "slug": str(subj).split("#")[-1],
                "code": str(owl.value(subj, TG.countryCode) or ""),
                "label": str(owl.value(subj, RDFS.label) or str(subj).split("#")[-1]),
            }
            for subj in owl.subjects(RDF.type, TG.Country)
            if isinstance(subj, URIRef)
        ),
        key=lambda item: item["label"],
    )

    agreements: list[dict[str, object]] = []
    origin_docs_map: dict[str, dict[str, str]] = {}
    for subj in sorted(owl.subjects(RDF.type, TG.TradeAgreement), key=str):
        if not isinstance(subj, URIRef):
            continue
        slug = str(subj).split("#")[-1]
        doc_ids: list[str] = []
        for doc in sorted(owl.objects(subj, TG.agreementOriginDocumentType), key=str):
            if not isinstance(doc, URIRef):
                continue
            doc_id = str(doc).split("#")[-1]
            doc_ids.append(doc_id)
            doc_label = owl.value(doc, RDFS.label)
            origin_docs_map.setdefault(doc_id, {
                "id": doc_id,
                "label": str(doc_label) if doc_label is not None else doc_id,
                "agreement": slug,
            })
        b2b = owl.value(subj, TG.allowsBackToBackCoo)
        agreements.append({
            "slug": slug,
            "label": str(owl.value(subj, RDFS.label) or slug.upper()),
            "facility_code": str(owl.value(subj, TG.pibFacilityCode)) if owl.value(subj, TG.pibFacilityCode) else None,
            "back_to_back_allowed": str(b2b).lower() == "true" if b2b is not None else None,
            "origin_docs": doc_ids,
        })

    lartas_docs_map: dict[str, dict[str, str]] = {}
    for rule in owl.subjects(RDF.type, TG.ImportRestrictionRule):
        for doc in owl.objects(rule, TG.lartasRequiresDocumentType):
            if isinstance(doc, URIRef):
                doc_id = str(doc).split("#")[-1]
                lartas_docs_map.setdefault(doc_id, {
                    "id": doc_id,
                    "label": str(owl.value(doc, RDFS.label) or doc_id),
                })

    export_lartas_docs_map: dict[str, dict[str, str]] = {
        "pebDeclaration": {
            "id": "pebDeclaration",
            "label": str(owl.value(TG.pebDeclaration, RDFS.label) or "pebDeclaration"),
        },
    }
    for rule in owl.subjects(RDF.type, TG.ExportRestrictionRule):
        for doc in owl.objects(rule, TG.exportLartasRequiresDocumentType):
            if isinstance(doc, URIRef):
                doc_id = str(doc).split("#")[-1]
                export_lartas_docs_map.setdefault(doc_id, {
                    "id": doc_id,
                    "label": str(owl.value(doc, RDFS.label) or doc_id),
                })

    process_kinds = sorted(
        (
            {
                "id": str(subj).split("#")[-1],
                "label": str(owl.value(subj, RDFS.label) or str(subj).split("#")[-1]),
            }
            for subj in owl.subjects(RDF.type, TG.OriginCriterionKind)
            if isinstance(subj, URIRef) and str(subj).split("#")[-1] in {
                "criterionChemicalReaction",
                "criterionTextileProcess",
                "criterionCutAndSewnAMS",
                "criterionRefiningProcess",
            }
        ),
        key=lambda item: item["label"],
    )

    minimal_ops_map: dict[str, dict[str, str]] = {}
    for agr in owl.subjects(RDF.type, TG.TradeAgreement):
        for op in owl.objects(agr, TG.disqualifyingOperation):
            if isinstance(op, URIRef):
                op_id = str(op).split("#")[-1]
                minimal_ops_map.setdefault(op_id, {
                    "id": op_id,
                    "label": str(owl.value(op, RDFS.label) or op_id),
                })

    return {
        "kb_id": optimizer.kb.snapshot.kb_id,
        "kb_version": optimizer.kb.snapshot.version,
        "kb_sha256": optimizer.kb.snapshot.sha256,
        "engine_profile_id": optimizer.kb.snapshot.engine_profile_id,
        "hs_codes": featured_hs,
        "all_hs_codes_count": len(all_hs),
        "all_hs_codes_sample": all_hs[:100],
        "countries": countries,
        "agreements": agreements,
        "origin_doc_types": sorted(origin_docs_map.values(), key=lambda item: (item["agreement"], item["id"])),
        "lartas_doc_types": sorted(lartas_docs_map.values(), key=lambda item: item["id"]),
        "export_lartas_doc_types": sorted(export_lartas_docs_map.values(), key=lambda item: item["id"]),
        "process_kinds": process_kinds,
        "minimal_operations": sorted(minimal_ops_map.values(), key=lambda item: item["id"]),
        "presets": list(_PRESETS),
        "export_presets": list(_EXPORT_PRESETS),
        "screening_presets": list(_SCREENING_PRESETS),
        "roo_presets": list(_ROO_PRESETS),
        "temporary_import_presets": list(_TEMPORARY_IMPORT_PRESETS),
        "hs_classifier_presets": list(_HS_CLASSIFIER_PRESETS),
        "counterfactual_presets": list(_COUNTERFACTUAL_PRESETS),
        "regime_presets": list(_REGIME_PRESETS),
        "batch_samples": _BATCH_SAMPLES,
        "impact_scenarios": list(_IMPACT_SCENARIOS),
    }


def _query_regulations(optimizer: MultiFtaOptimizer, as_of_date: str) -> dict[str, object]:
    owl = optimizer.kb.dataset.graph(optimizer.kb.owl_projection_ref)
    active_def = optimizer.kb.query_catalog.get("active-regulations")
    active_rows: list[dict[str, object]] = []
    if active_def is not None:
        query = prepare_local_query(active_def.sparql, "SelectQuery")
        seen: set[str] = set()
        for row in owl.query(query, initBindings={"asOfDate": RDFLiteral(as_of_date)}):
            d = row.asdict()
            doc_iri = d["document"]
            doc_id = str(doc_iri).split("#")[-1]
            if doc_id in seen:
                continue
            seen.add(doc_id)
            urls = sorted(str(u) for u in owl.objects(doc_iri, RDFS.seeAlso))
            url = next((u for u in urls if "download" in u or u.endswith(".pdf")), urls[0] if urls else None)
            active_rows.append({
                "document_id": doc_id,
                "regulation_number": str(d["number"]) if d.get("number") else None,
                "title": str(owl.value(doc_iri, RDFS.label) or doc_id),
                "effective_from": str(d["effectiveFrom"]) if d.get("effectiveFrom") else None,
                "url": url,
            })
    active_rows.sort(key=lambda item: (item["effective_from"] or "", item["document_id"]))

    relations: list[dict[str, object]] = []
    for ev in sorted(owl.subjects(RDF.type, TG.LegalRelationEvent), key=str):
        src = owl.value(ev, TG.relationActingDocument) or owl.value(ev, TG.relationSourceDocument)
        tgt = owl.value(ev, TG.relationTargetDocument)
        kind = owl.value(ev, TG.hasLegalRelationKind)
        eff = owl.value(ev, TG.relationEffectiveDate)
        relations.append({
            "event_id": str(ev).split("#")[-1],
            "kind": str(kind).split("#")[-1] if kind is not None else None,
            "source_document": str(src).split("#")[-1] if src is not None else None,
            "source_number": str(owl.value(src, TG.regulationNumber)) if isinstance(src, URIRef) and owl.value(src, TG.regulationNumber) else None,
            "target_document": str(tgt).split("#")[-1] if tgt is not None else None,
            "target_number": str(owl.value(tgt, TG.regulationNumber)) if isinstance(tgt, URIRef) and owl.value(tgt, TG.regulationNumber) else None,
            "effective_date": str(eff) if eff is not None else None,
            "active_on_date": (str(eff) <= as_of_date) if eff is not None else True,
        })
    for src, tgt in sorted(owl.subject_objects(TG.partiallyRevokesDocument), key=lambda pair: (str(pair[0]), str(pair[1]))):
        relations.append({
            "event_id": f"partial-{str(src).split('#')[-1]}-{str(tgt).split('#')[-1]}",
            "kind": "partiallyRevokesDocument",
            "source_document": str(src).split("#")[-1],
            "source_number": str(owl.value(src, TG.regulationNumber)) if isinstance(src, URIRef) and owl.value(src, TG.regulationNumber) else None,
            "target_document": str(tgt).split("#")[-1],
            "target_number": str(owl.value(tgt, TG.regulationNumber)) if isinstance(tgt, URIRef) and owl.value(tgt, TG.regulationNumber) else None,
            "effective_date": str(owl.value(src, TG.effectiveFromDate)) if isinstance(src, URIRef) and owl.value(src, TG.effectiveFromDate) else None,
            "active_on_date": (
                str(owl.value(src, TG.effectiveFromDate)) <= as_of_date
                if isinstance(src, URIRef) and owl.value(src, TG.effectiveFromDate) else True
            ),
        })

    return {
        "kb_version": optimizer.kb.snapshot.version,
        "as_of_date": as_of_date,
        "active_count": len(active_rows),
        "active_regulations": active_rows,
        "legal_relations": relations,
    }


def create_app(
    optimizer: MultiFtaOptimizer | None = None,
    *,
    kb_path: Path = DEFAULT_KB_PATH,
    manifest_path: Path | None = None,
    regulatory_monitor: RegulatoryMonitor | None = None,
    engine_profile: str = ENGINE_PROFILE_ID,
    case_db_path: Path | None = None,
    fuseki_url: str | None = None,
    fuseki_user: str | None = None,
    fuseki_password: str | None = None,
    sync_kb_on_startup: bool | None = None,
) -> FastAPI:
    """Create a FastAPI app backed by a single warm MultiFtaOptimizer, canonical KB, and optional SQLite/Fuseki store."""
    if optimizer is None:
        compiled = compile_kb(kb_path, engine_profile)
        optimizer = MultiFtaOptimizer(compiled, resolve_runtime_classpath())

    resolved_manifest_path = manifest_path or Path(
        os.environ.get("TRADEGRAPH_MANIFEST_PATH", "").strip() or DEFAULT_MANIFEST_PATH
    )
    monitor = regulatory_monitor or RegulatoryMonitor(
        resolved_manifest_path,
        kb_path,
        compiled_kb=optimizer.kb,
        engine_profile_id=engine_profile,
    )

    persistence = CasePersistenceService.from_env(
        case_db_path=case_db_path,
        fuseki_url=fuseki_url,
        fuseki_user=fuseki_user,
        fuseki_password=fuseki_password,
    )
    auth_db = case_db_path or (
        Path(os.environ.get("TRADEGRAPH_CASE_DB_PATH", "").strip())
        if os.environ.get("TRADEGRAPH_CASE_DB_PATH", "").strip()
        else None
    )
    auth_service = AuthService(auth_db)
    should_sync_kb = (
        sync_kb_on_startup
        if sync_kb_on_startup is not None
        else os.environ.get("TRADEGRAPH_SYNC_KB_ON_STARTUP", "").strip().lower() in {"1", "true", "yes"}
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if should_sync_kb and persistence.fuseki is not None:
            try:
                sync_info = persistence.sync_kb(optimizer.kb)
                logger.info("Fuseki KB sync on startup: %s", sync_info)
            except Exception as err:
                logger.warning("Fuseki KB sync on startup failed (will stay degraded until reachable): %s", err)
        yield
        ReasoningClient.shutdown_workers()

    catalog_snapshot = _build_catalog(optimizer)
    kb_sha_tag = optimizer.kb.snapshot.sha256[:16]
    catalog_etag = f'W/"catalog-{kb_sha_tag}"'
    hs_facets_etag = f'W/"hs-facets-{kb_sha_tag}"'
    doc_templates_etag = f'W/"doc-templates-{kb_sha_tag}"'
    workbench_cache: dict[str, object] = {"sig": None, "html": "", "etag": ""}
    _FAVICON_SVG = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 28">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0%" stop-color="#0284c7"/><stop offset="100%" stop-color="#10b981"/>'
        '</linearGradient></defs>'
        '<rect width="28" height="28" rx="7" fill="url(#g)"/>'
        '<path d="M14 4L23 9V19L14 24L5 19V9L14 4Z" fill="none" stroke="#fff" stroke-width="2"/>'
        '<path d="M8 16.5L14 11.5L20 16.5" fill="none" stroke="#a7f3d0" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="14" cy="11.5" r="2.2" fill="#fff"/>'
        '</svg>'
    )

    def _load_workbench_cached() -> tuple[str, str]:
        st = WORKBENCH_HTML_PATH.stat()
        sig = (st.st_mtime_ns, st.st_size)
        if workbench_cache["sig"] != sig:
            workbench_cache["html"] = WORKBENCH_HTML_PATH.read_text(encoding="utf-8")
            workbench_cache["etag"] = f'W/"wb-{st.st_mtime_ns:x}-{st.st_size:x}"'
            workbench_cache["sig"] = sig
        return str(workbench_cache["html"]), str(workbench_cache["etag"])

    app = FastAPI(
        title="TradeGraph Semantic Trade Compliance API",
        version=optimizer.kb.snapshot.version,
        lifespan=lifespan,
        description=(
            "OWL 2 DL + HermiT + SWRL + SHACL cross-border trade compliance engine for Multi-FTA RoO, "
            "tariff optimization, RCEP DV20, Indonesian Lartas/NTM, Export Bea Keluar, Batch Screening, "
            "Regulatory Impact Diff, and SQLite/Fuseki persistence."
        ),
    )
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)
    app.state.optimizer = optimizer
    export_advisor = ExportComplianceAdvisor(optimizer)
    app.state.export_advisor = export_advisor
    batch_screener = BatchScreener(optimizer, export_advisor)
    app.state.batch_screener = batch_screener
    impact_analyzer = RegulatoryImpactAnalyzer(optimizer, batch_screener)
    app.state.impact_analyzer = impact_analyzer
    doc_generator = DocumentGeneratorService(optimizer)
    app.state.doc_generator = doc_generator
    temp_import_advisor = TemporaryImportAdvisor(optimizer)
    app.state.temp_import_advisor = temp_import_advisor
    hs_classifier = HsClassifierService(optimizer, export_advisor)
    app.state.hs_classifier = hs_classifier
    counterfactual_optimizer = CounterfactualOptimizerService(optimizer, export_advisor)
    app.state.counterfactual_optimizer = counterfactual_optimizer
    regime_advisor = CustomsRegimeAdvisor(optimizer, export_advisor)
    app.state.regime_advisor = regime_advisor
    app.state.persistence = persistence
    app.state.regulatory_monitor = monitor
    app.state.auth_service = auth_service

    def _extract_token(authorization: str | None) -> str | None:
        if not authorization:
            return None
        raw = authorization.strip()
        if raw.lower().startswith("bearer "):
            return raw[7:].strip()
        return raw or None

    def _require_user(authorization: str | None) -> UserProfile:
        token = _extract_token(authorization)
        user = auth_service.get_user_by_token(token)
        if user is None:
            raise HTTPException(status_code=401, detail="Sesi login tidak ditemukan atau telah berakhir. Silakan login kembali.")
        return user

    def _require_admin(authorization: str | None) -> UserProfile:
        user = _require_user(authorization)
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Akses ditolak: Halaman dan fitur ini khusus untuk Administrator.")
        return user

    def _reject_if_non_admin_token(authorization: str | None) -> None:
        token = _extract_token(authorization)
        if not token:
            return
        user = auth_service.get_user_by_token(token)
        if user is None:
            raise HTTPException(status_code=401, detail="Token sesi tidak valid.")
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Akses ditolak: Hanya Administrator yang dapat menjalankan operasi ini.")

    hs_facets_snapshot = hs_classifier.get_facets()
    doc_templates_snapshot = {
        "kb_version": optimizer.kb.snapshot.version,
        "count": len(doc_generator.list_templates()),
        "templates": [tpl.to_dict() for tpl in doc_generator.list_templates()],
    }

    def _etag_matches(if_none_match: str | None, etag: str) -> bool:
        if not if_none_match:
            return False
        return any(part.strip() in {etag, "*"} for part in if_none_match.split(","))

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(
            content=_FAVICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def workbench(
        if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    ) -> Response:
        html_content, etag = _load_workbench_cached()
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if _etag_matches(if_none_match, etag):
            return Response(status_code=304, headers=headers)
        return HTMLResponse(content=html_content, headers=headers)

    @app.get("/api/v1/health")
    def health() -> dict[str, object]:
        snap = optimizer.kb.snapshot
        return {
            "status": "ok",
            "kb_id": snap.kb_id,
            "kb_version": snap.version,
            "kb_sha256": snap.sha256,
            "engine_profile_id": snap.engine_profile_id,
            "queries": sorted(optimizer.kb.query_catalog),
            "actions": sorted(optimizer.kb.action_catalog),
            "persistence": {
                "sqlite_enabled": persistence.repository is not None,
                "fuseki_enabled": persistence.fuseki is not None,
            },
        }

    @app.get("/api/v1/ready")
    def ready() -> JSONResponse:
        is_ready, payload = persistence.readiness(optimizer.kb, optimizer.classpath)
        return JSONResponse(content=payload, status_code=200 if is_ready else 503)

    @app.get("/api/v1/catalog")
    def catalog(
        if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    ) -> Response:
        headers = {"ETag": catalog_etag, "Cache-Control": "public, max-age=300"}
        if _etag_matches(if_none_match, catalog_etag):
            return Response(status_code=304, headers=headers)
        return JSONResponse(content=catalog_snapshot, headers=headers)

    @app.get("/api/v1/lartas")
    def check_lartas(
        hs_code: str = Query(..., min_length=4, description="HS8 code (e.g. 7209.26.10)"),
        as_of_date: str = Query(default="2025-09-15", description="Date YYYY-MM-DD"),
        api_type: str | None = Query(default=None, description="Optional importer API kind: API-P or API-U"),
        provided_docs: str | None = Query(default=None, description="Comma-separated Lartas doc IDs already held"),
        explain: bool = Query(default=True, description="Include full resolved legal citations"),
    ) -> dict[str, object]:
        if not _ISO_DATE_RE.match(as_of_date.strip()):
            raise HTTPException(status_code=422, detail="as_of_date must use YYYY-MM-DD format.")
        try:
            hs_iri = optimizer.resolve_hs_iri(hs_code)
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        docs = tuple(item.strip() for item in (provided_docs or "").split(",") if item.strip())
        spec = ShipmentSpec(
            hs_code=hs_code.strip(),
            dispatch_country="TH",
            destination_country="ID",
            as_of_date=as_of_date.strip(),
            api_type=api_type.strip() if api_type else None,
            provided_lartas_docs=docs,
            include_explain=explain,
        )
        summary = optimizer._assess_lartas(hs_iri, TG.jurisdictionIndonesiaCustoms, spec)
        return {
            "kb_version": optimizer.kb.snapshot.version,
            "hs_code": str(optimizer._owl.value(hs_iri, TG.codeValue) or hs_code.strip()),
            "hs_iri": str(hs_iri),
            "as_of_date": spec.as_of_date,
            "lartas": summary.to_dict(),
        }

    @app.get("/api/v1/regulations")
    def regulations(
        as_of_date: str = Query(default="2026-08-15", description="Date YYYY-MM-DD"),
    ) -> dict[str, object]:
        if not _ISO_DATE_RE.match(as_of_date.strip()):
            raise HTTPException(status_code=422, detail="as_of_date must use YYYY-MM-DD format.")
        return _query_regulations(optimizer, as_of_date.strip())

    @app.post("/api/v1/optimize")
    def optimize_shipment(request: OptimizeRequest) -> dict[str, object]:
        spec = request.to_shipment_spec()
        agreements = (
            tuple(item.strip().lower() for item in request.agreements if item.strip())
            if request.agreements else None
        )
        try:
            result = optimizer.optimize(spec, agreements=agreements)
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        return result.to_dict()

    @app.post("/api/v1/assess/{agreement}")
    def assess_single_scheme(agreement: str, request: AssessRequest) -> dict[str, object]:
        spec = request.to_shipment_spec()
        try:
            evaluation = optimizer.assess_scheme(spec, agreement.strip().lower())
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        return evaluation.to_dict()

    @app.get("/api/v1/export-lartas")
    def check_export_lartas(
        hs_code: str = Query(..., min_length=4, description="HS8 code (e.g. 8001.10.00, 2604.00.00)"),
        as_of_date: str = Query(default="2025-09-15", description="Date YYYY-MM-DD"),
        provided_docs: str | None = Query(default=None, description="Comma-separated export docs already held"),
        explain: bool = Query(default=True, description="Include full resolved legal citations"),
    ) -> dict[str, object]:
        if not _ISO_DATE_RE.match(as_of_date.strip()):
            raise HTTPException(status_code=422, detail="as_of_date must use YYYY-MM-DD format.")
        try:
            hs_iri = optimizer.resolve_hs_iri(hs_code)
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        docs = tuple(item.strip() for item in (provided_docs or "").split(",") if item.strip())
        spec = ExportSpec(
            hs_code=hs_code.strip(),
            destination_country="SG",
            as_of_date=as_of_date.strip(),
            provided_export_docs=docs,
            include_explain=explain,
        )
        summary = export_advisor.assess_export_lartas(hs_iri, spec)
        return {
            "kb_version": optimizer.kb.snapshot.version,
            "hs_code": str(optimizer._owl.value(hs_iri, TG.codeValue) or hs_code.strip()),
            "hs_iri": str(hs_iri),
            "as_of_date": spec.as_of_date,
            "export_lartas": summary.to_dict(),
        }

    @app.post("/api/v1/export/evaluate")
    def evaluate_export(request: ExportRequest) -> dict[str, object]:
        spec = request.to_export_spec()
        try:
            result = export_advisor.evaluate(spec)
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        return result.to_dict()

    @app.get("/api/v1/export-duty")
    def check_export_duty(
        hs_code: str = Query(..., min_length=4, description="HS8 code (e.g. 1511.10.00, 1801.00.10, 2603.00.00)"),
        as_of_date: str = Query(default="2025-11-15", description="Export date YYYY-MM-DD"),
        quantity_mt: str | None = Query(default=None, description="Quantity in metric tons (MT)"),
        reference_price_usd: str | None = Query(default=None, description="Kemendag Harga Referensi (HR) in USD/MT"),
        export_price_usd_per_mt: str | None = Query(default=None, description="DJBC Harga Ekspor (HPE) in USD/MT"),
        fob_value: str | None = Query(default=None, description="Optional FOB value in USD"),
        explain: bool = Query(default=True, description="Include full resolved legal citations"),
    ) -> dict[str, object]:
        if not _ISO_DATE_RE.match(as_of_date.strip()):
            raise HTTPException(status_code=422, detail="as_of_date must use YYYY-MM-DD format.")
        try:
            hs_iri = optimizer.resolve_hs_iri(hs_code)
            qty = Decimal(quantity_mt.strip()) if quantity_mt and quantity_mt.strip() else None
            hr = Decimal(reference_price_usd.strip()) if reference_price_usd and reference_price_usd.strip() else None
            hpe = Decimal(export_price_usd_per_mt.strip()) if export_price_usd_per_mt and export_price_usd_per_mt.strip() else None
            fob = Decimal(fob_value.strip()) if fob_value and fob_value.strip() else None
        except (ValueError, InvalidOperation) as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        spec = ExportSpec(
            hs_code=hs_code.strip(),
            destination_country="CN",
            as_of_date=as_of_date.strip(),
            fob_value=fob,
            quantity_mt=qty,
            reference_price_usd=hr,
            export_price_usd_per_mt=hpe,
            include_explain=explain,
        )
        lartas = export_advisor.assess_export_lartas(hs_iri, spec)
        duty = export_advisor.assess_export_duty(hs_iri, spec, prohibited=lartas.prohibited)
        return {
            "kb_version": optimizer.kb.snapshot.version,
            "hs_code": str(optimizer._owl.value(hs_iri, TG.codeValue) or hs_code.strip()),
            "hs_iri": str(hs_iri),
            "as_of_date": spec.as_of_date,
            "export_duty": duty.to_dict(),
        }

    def _format_batch_response(result, output_format: Literal["json", "csv", "xlsx"]):
        ts = result.generated_at[:10]
        if output_format == "csv":
            return Response(
                content=result.to_csv_bytes(),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="tradegraph-{result.mode}-audit-{ts}.csv"'},
            )
        if output_format == "xlsx":
            return Response(
                content=result.to_xlsx_bytes(),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f'attachment; filename="tradegraph-{result.mode}-audit-{ts}.xlsx"'},
            )
        return result.to_dict()

    @app.post("/api/v1/batch/screen")
    def screen_batch(
        request: BatchScreenRequest,
        format: Literal["json", "csv", "xlsx"] = Query(default="json", description="Response format: json, csv, or xlsx"),
    ):
        result = batch_screener.screen(
            request.items,
            mode=request.mode,
            screening_depth=request.screening_depth,
            include_explain=request.include_explain,
        )
        return _format_batch_response(result, format)

    @app.post("/api/v1/batch/upload")
    async def upload_batch(
        file: UploadFile = File(..., description="CSV, XLSX, or JSON batch file"),
        mode: Literal["import", "export"] = Form(default="export"),
        screening_depth: Literal["full", "fast_lartas"] = Form(default="fast_lartas"),
        include_explain: bool = Form(default=True),
        format: Literal["json", "csv", "xlsx"] = Query(default="json", description="Response format: json, csv, or xlsx"),
    ):
        raw_bytes = await file.read()
        filename = (file.filename or "batch.csv").lower()
        try:
            if filename.endswith((".xlsx", ".xlsm")):
                rows = load_batch_rows_from_xlsx(raw_bytes)
            elif filename.endswith(".json"):
                parsed = json.loads(raw_bytes.decode("utf-8-sig"))
                items = parsed.get("items") if isinstance(parsed, dict) else parsed
                if not isinstance(items, list):
                    raise ValueError("JSON upload must be a list of shipment objects or {'items': [...]}.")
                rows = [item for item in items if isinstance(item, dict)]
            else:
                rows = load_batch_rows_from_csv(raw_bytes)
        except Exception as err:
            raise HTTPException(status_code=422, detail=f"Failed to parse batch file: {err}") from err
        if not rows:
            raise HTTPException(status_code=422, detail="Uploaded batch file contains no shipment rows.")
        if len(rows) > 250:
            raise HTTPException(status_code=422, detail="Batch upload exceeds the 250-row limit.")
        result = batch_screener.screen(
            rows,
            mode=mode,
            screening_depth=screening_depth,
            include_explain=include_explain,
        )
        return _format_batch_response(result, format)

    @app.get("/api/v1/regulations/diff")
    def regulations_diff(
        from_date: str = Query(..., description="Baseline date YYYY-MM-DD"),
        to_date: str = Query(..., description="Target date YYYY-MM-DD"),
    ) -> dict[str, object]:
        try:
            report = impact_analyzer.diff_regulations(from_date, to_date)
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        return report.to_dict()

    def _format_impact_response(result, output_format: Literal["json", "csv", "xlsx"]):
        tag = f"{result.baseline_date}_to_{result.target_date}"
        if output_format == "csv":
            return Response(
                content=result.to_csv_bytes(),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="tradegraph-{result.mode}-impact-{tag}.csv"'},
            )
        if output_format == "xlsx":
            return Response(
                content=result.to_xlsx_bytes(),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f'attachment; filename="tradegraph-{result.mode}-impact-{tag}.xlsx"'},
            )
        return result.to_dict()

    @app.post("/api/v1/impact/analyze")
    def analyze_impact(
        request: ImpactAnalyzeRequest,
        format: Literal["json", "csv", "xlsx"] = Query(default="json", description="Response format: json, csv, or xlsx"),
    ):
        try:
            result = impact_analyzer.analyze_portfolio(
                request.items,
                mode=request.mode,
                baseline_date=request.baseline_date,
                target_date=request.target_date,
                screening_depth=request.screening_depth,
                include_explain=request.include_explain,
            )
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        return _format_impact_response(result, format)

    @app.post("/api/v1/impact/upload")
    async def upload_impact(
        file: UploadFile = File(..., description="CSV, XLSX, or JSON portfolio file"),
        mode: Literal["import", "export"] = Form(default="export"),
        baseline_date: str = Form(..., description="Baseline date YYYY-MM-DD"),
        target_date: str = Form(..., description="Target date YYYY-MM-DD"),
        screening_depth: Literal["fast_lartas", "full"] = Form(default="fast_lartas"),
        include_explain: bool = Form(default=True),
        format: Literal["json", "csv", "xlsx"] = Query(default="json", description="Response format: json, csv, or xlsx"),
    ):
        raw_bytes = await file.read()
        filename = (file.filename or "portfolio.csv").lower()
        try:
            if filename.endswith((".xlsx", ".xlsm")):
                rows = load_batch_rows_from_xlsx(raw_bytes)
            elif filename.endswith(".json"):
                parsed = json.loads(raw_bytes.decode("utf-8-sig"))
                items = parsed.get("items") if isinstance(parsed, dict) else parsed
                if not isinstance(items, list):
                    raise ValueError("JSON upload must be a list of shipment objects or {'items': [...]}.")
                rows = [item for item in items if isinstance(item, dict)]
            else:
                rows = load_batch_rows_from_csv(raw_bytes)
        except Exception as err:
            raise HTTPException(status_code=422, detail=f"Failed to parse portfolio file: {err}") from err
        if not rows:
            raise HTTPException(status_code=422, detail="Uploaded portfolio file contains no shipment rows.")
        if len(rows) > 250:
            raise HTTPException(status_code=422, detail="Portfolio upload exceeds the 250-row limit.")
        try:
            result = impact_analyzer.analyze_portfolio(
                rows,
                mode=mode,
                baseline_date=baseline_date,
                target_date=target_date,
                screening_depth=screening_depth,
                include_explain=include_explain,
            )
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        return _format_impact_response(result, format)

    @app.post("/api/v1/fuseki/sync-kb")
    def sync_kb_to_fuseki(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, object]:
        _reject_if_non_admin_token(authorization)
        if persistence.fuseki is None:
            raise HTTPException(status_code=503, detail="Fuseki persistence is not configured (set TRADEGRAPH_FUSEKI_URL).")
        try:
            return persistence.sync_kb(optimizer.kb)
        except Exception as err:
            raise HTTPException(status_code=503, detail=f"Fuseki KB sync failed: {err}") from err

    @app.post("/api/v1/cases/persist")
    def persist_case(request: PersistCaseRequest) -> dict[str, object]:
        if not persistence.enabled:
            raise HTTPException(
                status_code=503,
                detail="Persistence is not configured (set TRADEGRAPH_CASE_DB_PATH and/or TRADEGRAPH_FUSEKI_URL).",
            )
        try:
            if request.mode == "import":
                opt_req = OptimizeRequest.model_validate(request.shipment)
                spec = opt_req.to_shipment_spec()
                agreements = (
                    tuple(item.strip().lower() for item in opt_req.agreements if item.strip())
                    if opt_req.agreements else None
                )
                result = optimizer.optimize(spec, agreements=agreements)
                persisted = persistence.persist_import_case(optimizer.kb, spec, result, reason=request.reason)
                return {"persisted": persisted, "assessment": result.to_dict()}
            exp_req = ExportRequest.model_validate(request.shipment)
            spec_exp = exp_req.to_export_spec()
            result_exp = export_advisor.evaluate(spec_exp)
            persisted = persistence.persist_export_case(optimizer.kb, spec_exp, result_exp, reason=request.reason)
            return {"persisted": persisted, "assessment": result_exp.to_dict()}
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except Exception as err:
            raise HTTPException(status_code=503, detail=f"Failed to persist compliance case: {err}") from err

    @app.get("/api/v1/cases")
    def list_persisted_cases(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, object]:
        try:
            return persistence.list_cases(limit=limit)
        except Exception as err:
            raise HTTPException(status_code=503, detail=f"Failed to list persisted cases: {err}") from err

    @app.get("/api/v1/cases/{case_id}")
    def get_persisted_case(
        case_id: str,
        revision: int | None = Query(default=None, ge=1),
        verify_fuseki: bool = Query(default=False, description="Verify live named graph triple counts against Fuseki"),
    ) -> dict[str, object]:
        try:
            return persistence.get_case(case_id, revision=revision, verify_fuseki=verify_fuseki)
        except CaseNotFound as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        except Exception as err:
            raise HTTPException(status_code=503, detail=f"Failed to load case {case_id}: {err}") from err

    @app.get("/api/v1/monitor/targets")
    def list_monitor_targets() -> dict[str, object]:
        return {
            "kb_version": optimizer.kb.snapshot.version,
            "count": len(DEFAULT_WATCHLIST),
            "targets": [t.to_dict() for t in monitor.list_targets()],
        }

    @app.get("/api/v1/monitor/freshness")
    def monitor_local_freshness(
        as_of_date: str | None = Query(default=None, description="Reference date YYYY-MM-DD (defaults to today)"),
        stale_days: int = Query(default=180, ge=1, le=3650),
        verify_hashes: bool = Query(default=False, description="Compute live SHA-256 over local archived files"),
        check_local_files: bool = Query(default=True, description="Flag missing local raw PDFs as critical"),
    ) -> dict[str, object]:
        if not monitor.manifest_path.is_file():
            raise HTTPException(
                status_code=503,
                detail=f"Source manifest not found: {monitor.manifest_path}",
            )
        try:
            audit = monitor.audit_local_freshness(
                as_of_date=as_of_date,
                stale_days=stale_days,
                verify_hashes=verify_hashes,
                check_local_files=check_local_files,
            )
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        return audit.to_dict()

    @app.post("/api/v1/monitor/scan")
    def monitor_scan(
        request: MonitorScanRequest,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, object]:
        _reject_if_non_admin_token(authorization)
        if not monitor.manifest_path.is_file():
            raise HTTPException(
                status_code=503,
                detail=f"Source manifest not found: {monitor.manifest_path}",
            )
        try:
            if request.regulations is not None:
                scan_report = monitor.evaluate_regulations(
                    request.regulations,
                    targets_checked=("api-replay",),
                    mode="replay",
                    as_of_date=request.as_of_date,
                )
            else:
                if request.targets is None and not request.custom_query and not request.portals:
                    # Default HTTP live scan to a bounded representative subset (2 targets) unless caller selects explicitly
                    default_ids = ["bea-keluar", "lartas-impor"]
                else:
                    default_ids = request.targets
                scan_report = monitor.scan_live(
                    target_ids=default_ids,
                    portals=request.portals,
                    custom_query=request.custom_query,
                    custom_portal=request.custom_portal,
                    tahun=request.tahun,
                    limit_per_target=request.limit_per_target,
                    delay_seconds=request.delay_seconds,
                    as_of_date=request.as_of_date,
                )
            payload = scan_report.to_dict(include_regulations=False)
            if request.generate_staging or request.validate_merge:
                staging = monitor.build_staging_diff(
                    scan_report,
                    output_dir=None,
                    archive_pdfs=False,
                    validate_merge=request.validate_merge,
                    run_owl_validator=request.run_owl_validator,
                )
                payload["staging"] = staging.to_dict(include_turtle=True)
            return payload
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    @app.post("/api/v1/temporary-import/evaluate")
    def evaluate_temporary_import(request: TemporaryImportRequest) -> dict[str, object]:
        try:
            result = temp_import_advisor.evaluate(request.to_spec())
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        return result.to_dict()

    @app.post("/api/v1/roo/diagnose")
    def diagnose_roo(request: RooDiagnoseRequest) -> dict[str, object]:
        owl = optimizer._owl
        try:
            if request.direction == "export":
                spec_exp = ExportSpec(
                    hs_code=request.hs_code.strip(),
                    origin_country="ID",
                    destination_country=request.partner_country.strip(),
                    as_of_date=request.as_of_date,
                    materials=tuple(m.to_domain() for m in request.materials),
                    rvc_percent=request.rvc_percent,
                    wo_confirmed=request.wo_confirmed,
                    process_kind=request.process_kind.strip() if request.process_kind else None,
                    process_satisfied=request.process_satisfied,
                    de_minimis_fob=request.de_minimis_fob,
                    de_minimis_weight=request.de_minimis_weight,
                    performed_operations=(
                        tuple(request.performed_operations) if request.performed_operations is not None else None
                    ),
                    transshipment_hub=request.transshipment_hub.strip() if request.transshipment_hub else None,
                    requires_back_to_back=request.requires_back_to_back,
                    third_party_invoice_country=(
                        request.third_party_invoice_country.strip() if request.third_party_invoice_country else None
                    ),
                    include_explain=request.include_explain,
                )
                exp_res = export_advisor.evaluate(spec_exp)
                exp_dict = exp_res.to_dict()
                repairs: list[dict[str, object]] = []
                for c in exp_res.cumulation_opportunities:
                    repairs.append({
                        "kind": "CUMULATION_SOURCING",
                        "agreement": c.agreement,
                        "failing_material_hs": c.failing_material_hs,
                        "current_origin": c.current_origin,
                        "recommended_partner_countries": list(c.recommended_partner_countries),
                        "recommendation": c.reason,
                    })
                for s in exp_res.schemes:
                    if s.origin_status != "SATISFIED" and request.rvc_percent is not None and request.rvc_percent < Decimal("40") and "RVC" in (s.concise_rule or "").upper():
                        gap = Decimal("40.00") - request.rvc_percent
                        repairs.append({
                            "kind": "RVC_INCREASE",
                            "agreement": s.agreement,
                            "failing_material_hs": None,
                            "current_origin": None,
                            "recommended_partner_countries": [],
                            "recommendation": (
                                f"Tingkatkan RVC pada {s.agreement_label} minimal +{gap:.2f}% (dari {request.rvc_percent:.2f}% ke ≥ 40.00%) "
                                f"agar memenuhi syarat RVC40 ({s.concise_rule})."
                            ),
                        })
                return {
                    "direction": "export",
                    "kb_version": exp_res.kb_version,
                    "hs_code": exp_res.hs_code,
                    "origin_country": exp_res.origin_country,
                    "destination_country": exp_res.destination_country,
                    "as_of_date": exp_res.as_of_date,
                    "recommended_agreement": exp_res.recommended_agreement,
                    "lartas_status": exp_res.export_lartas.status,
                    "schemes": exp_dict["schemes"],
                    "cumulation_opportunities": exp_dict["cumulation_opportunities"],
                    "repair_suggestions": repairs,
                }

            spec_imp = ShipmentSpec(
                hs_code=request.hs_code.strip(),
                dispatch_country=request.partner_country.strip(),
                destination_country="ID",
                as_of_date=request.as_of_date,
                cif_value=Decimal("100000"),
                currency="USD",
                materials=tuple(m.to_domain() for m in request.materials),
                rvc_percent=request.rvc_percent,
                wo_confirmed=request.wo_confirmed,
                process_kind=request.process_kind.strip() if request.process_kind else None,
                process_satisfied=request.process_satisfied,
                de_minimis_fob=request.de_minimis_fob,
                de_minimis_weight=request.de_minimis_weight,
                performed_operations=(
                    tuple(request.performed_operations) if request.performed_operations is not None else None
                ),
                dv20_percent=request.dv20_percent,
                highest_contributor_country=(
                    request.highest_contributor_country.strip() if request.highest_contributor_country else None
                ),
                include_plan=False,
                include_explain=request.include_explain,
            )
            agreements = (
                tuple(item.strip().lower() for item in request.agreements if item.strip())
                if request.agreements else None
            )
            opt_res = optimizer.optimize(spec_imp, agreements=agreements)
            opt_dict = opt_res.to_dict()
            target_hs4 = opt_res.hs_code.replace(".", "")[:4]
            target_hs6 = opt_res.hs_code.replace(".", "")[:6]
            repairs = []
            for s in opt_res.schemes:
                if s.origin_status != "SATISFIED" and s.minimal_ops_status != "NOT_SATISFIED":
                    agr_iri = TG[s.agreement]
                    partners = sorted({
                        str(p).split("#")[-1]
                        for p in owl.objects(agr_iri, TG.agreementParty)
                        if isinstance(p, URIRef)
                    })
                    for mat in request.materials:
                        m_clean = mat.hs_code.replace(".", "")
                        rule_up = (s.concise_rule or "").upper()
                        same_heading = m_clean[:4] == target_hs4
                        same_sub = m_clean[:6] == target_hs6
                        if mat.non_originating and (
                            ("CTH" in rule_up and same_heading)
                            or ("CTSH" in rule_up and same_sub)
                            or ("CC" in rule_up and m_clean[:2] == target_hs4[:2])
                        ):
                            repairs.append({
                                "kind": "CUMULATION_SOURCING",
                                "agreement": s.agreement,
                                "failing_material_hs": mat.hs_code,
                                "current_origin": mat.origin_country,
                                "recommended_partner_countries": partners,
                                "recommendation": (
                                    f"Bahan {mat.hs_code} menghambat kriteria CTC ({s.concise_rule}) pada {s.agreement_label}. "
                                    f"Ganti pemasok bahan ke negara anggota {s.agreement.upper()} ({', '.join(partners[:6])}) "
                                    f"dengan bukti asal terverifikasi untuk mengaktifkan akumulasi regional."
                                ),
                            })
                    if request.rvc_percent is not None and request.rvc_percent < Decimal("40") and "RVC" in (s.concise_rule or "").upper():
                        gap = Decimal("40.00") - request.rvc_percent
                        repairs.append({
                            "kind": "RVC_INCREASE",
                            "agreement": s.agreement,
                            "failing_material_hs": None,
                            "current_origin": None,
                            "recommended_partner_countries": [],
                            "recommendation": (
                                f"Tingkatkan RVC pada {s.agreement_label} minimal +{gap:.2f}% (dari {request.rvc_percent:.2f}% ke ≥ 40.00%) "
                                f"untuk memenuhi alternatif RVC40 ({s.concise_rule}) dan membuka potensi tarif {s.schedule_duty_percent}%."
                            ),
                        })
            return {
                "direction": "import",
                "kb_version": opt_res.kb_version,
                "hs_code": opt_res.hs_code,
                "origin_country": opt_res.dispatch_country,
                "destination_country": opt_res.destination_country,
                "as_of_date": opt_res.as_of_date,
                "recommended_agreement": opt_res.recommended_agreement,
                "lartas_status": opt_res.lartas.status,
                "schemes": opt_dict["schemes"],
                "cumulation_opportunities": [r for r in repairs if r["kind"] == "CUMULATION_SOURCING"],
                "repair_suggestions": repairs,
            }
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    @app.get("/api/v1/hs-classifier/facets")
    def list_hs_classifier_facets(
        if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    ) -> Response:
        headers = {"ETag": hs_facets_etag, "Cache-Control": "public, max-age=300"}
        if _etag_matches(if_none_match, hs_facets_etag):
            return Response(status_code=304, headers=headers)
        return JSONResponse(content=hs_facets_snapshot, headers=headers)

    @app.post("/api/v1/hs-classifier/classify")
    def classify_hs_code(request: HsClassifyRequest) -> dict[str, object]:
        try:
            return hs_classifier.classify(request.to_spec())
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    @app.post("/api/v1/counterfactual/optimize")
    def optimize_counterfactuals(request: CounterfactualRequest) -> dict[str, object]:
        try:
            return counterfactual_optimizer.optimize_counterfactuals(request.to_spec())
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    @app.post("/api/v1/regimes/recommend")
    def recommend_customs_regimes(request: RegimeConfigureRequest) -> dict[str, object]:
        try:
            return regime_advisor.recommend(request.to_spec())
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    @app.get("/api/v1/documents/templates")
    def list_document_templates(
        if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    ) -> Response:
        headers = {"ETag": doc_templates_etag, "Cache-Control": "public, max-age=300"}
        if _etag_matches(if_none_match, doc_templates_etag):
            return Response(status_code=304, headers=headers)
        return JSONResponse(content=doc_templates_snapshot, headers=headers)

    @app.post("/api/v1/documents/prepare")
    def prepare_documents(
        request: DocumentPrepareRequest,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, object]:
        token = _extract_token(authorization)
        user = auth_service.get_user_by_token(token) if token else None
        user_info = {"name": user.name, "company": user.company} if user is not None else None
        try:
            package = doc_generator.prepare_from_recommendation(
                mode=request.mode,
                recommendation=request.recommendation,
                user_info=user_info,
                extra_inputs=request.extra_inputs,
            )
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        return package.to_dict()

    @app.post("/api/v1/documents/generate")
    def generate_document(request: DocumentGenerateRequest):
        try:
            if request.format == "html":
                html_doc = doc_generator.generate_html_preview(
                    request.template_code,
                    request.fields,
                    custom_title=request.custom_title,
                    requirement_reason=request.requirement_reason,
                )
                return HTMLResponse(content=html_doc)
            docx_bytes, filename = doc_generator.generate_docx(
                request.template_code,
                request.fields,
                custom_title=request.custom_title,
                requirement_reason=request.requirement_reason,
            )
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        return Response(
            content=docx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/v1/documents/generate-bundle")
    def generate_document_bundle(request: DocumentBundleRequest):
        try:
            zip_bytes = doc_generator.generate_bundle_zip([
                {
                    "template_code": item.template_code,
                    "fields": item.fields,
                    "custom_title": item.custom_title,
                    "requirement_reason": item.requirement_reason,
                }
                for item in request.items
            ])
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        safe_hs = re.sub(r"[^a-zA-Z0-9_-]+", "-", request.hs_code.strip()).strip("-").lower() or "shipment"
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="tradegraph-documents-{safe_hs}.zip"'},
        )

    @app.post("/api/v1/auth/register")
    def auth_register(request: RegisterRequest) -> dict[str, object]:
        try:
            return auth_service.register(
                name=request.name,
                email=request.email,
                password=request.password,
                company=request.company,
            )
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    @app.post("/api/v1/auth/login")
    def auth_login(request: LoginRequest) -> dict[str, object]:
        try:
            return auth_service.login(email=request.email, password=request.password)
        except AuthError as err:
            raise HTTPException(status_code=401, detail=str(err)) from err

    @app.post("/api/v1/auth/demo")
    def auth_demo_login(request: DemoLoginRequest) -> dict[str, object]:
        if request.role == "admin":
            return auth_service.login(email=DEMO_ADMIN_EMAIL, password=DEMO_ADMIN_PASSWORD)
        return auth_service.login(email=DEMO_CUSTOMER_EMAIL, password=DEMO_CUSTOMER_PASSWORD)

    @app.post("/api/v1/auth/logout")
    def auth_logout(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, object]:
        auth_service.logout(_extract_token(authorization))
        return {"status": "logged_out"}

    @app.get("/api/v1/auth/me")
    def auth_me(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, object]:
        user = _require_user(authorization)
        return {"user": user.to_dict()}

    @app.get("/api/v1/admin/overview")
    def admin_overview(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, object]:
        admin_user = _require_admin(authorization)
        readiness_info = persistence.readiness(optimizer.kb, optimizer.classpath)
        owl = optimizer.kb.dataset.graph(optimizer.kb.owl_projection_ref)
        freshness_summary = None
        if monitor.manifest_path.is_file():
            try:
                audit = monitor.audit_local_freshness(as_of_date="2026-08-15", check_local_files=False)
                freshness_summary = audit.to_dict()
            except Exception:
                freshness_summary = None
        cases_data = persistence.list_cases(limit=25) if persistence.enabled else {"count": 0, "cases": []}
        return {
            "admin": admin_user.to_dict(),
            "readiness": readiness_info,
            "kb_metrics": {
                "version": optimizer.kb.snapshot.version,
                "sha256": optimizer.kb.snapshot.sha256,
                "owl_triples": len(owl),
                "swrl_rules": len(optimizer.kb.rule_atom_counts),
                "hs_codes_count": len(list(owl.subjects(RDF.type, TG.HSCode))),
                "agreements_count": len(list(owl.subjects(RDF.type, TG.TradeAgreement))),
                "source_documents_count": len(list(owl.subjects(RDF.type, TG.SourceDocument))),
                "legal_relations_count": len(list(owl.subjects(RDF.type, TG.LegalRelationEvent))),
            },
            "users": auth_service.list_users(),
            "cases": cases_data,
            "freshness": freshness_summary,
        }

    @app.post("/api/v1/admin/users/{user_id}/role")
    def admin_set_user_role(
        user_id: str,
        request: RoleUpdateRequest,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, object]:
        _require_admin(authorization)
        try:
            updated = auth_service.set_user_role(user_id, request.role)
            return {"user": updated.to_dict()}
        except KeyError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    return app


def get_app() -> FastAPI:
    """ASGI factory entrypoint for production Uvicorn / Gunicorn deployments."""
    kb_raw = os.environ.get("TRADEGRAPH_KB_PATH", "").strip()
    kb_path = Path(kb_raw) if kb_raw else DEFAULT_KB_PATH
    manifest_raw = os.environ.get("TRADEGRAPH_MANIFEST_PATH", "").strip()
    manifest_path = Path(manifest_raw) if manifest_raw else DEFAULT_MANIFEST_PATH
    engine_profile = os.environ.get("TRADEGRAPH_ENGINE_PROFILE", ENGINE_PROFILE_ID).strip() or ENGINE_PROFILE_ID
    return create_app(kb_path=kb_path, manifest_path=manifest_path, engine_profile=engine_profile)
