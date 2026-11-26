"""Automated tests for KB-driven Document Preparation & Draft Generator (.docx, HTML, ZIP, FastAPI)."""

from __future__ import annotations

from decimal import Decimal
import io
from pathlib import Path
import zipfile

from fastapi.testclient import TestClient
from lxml import etree
import pytest

from tradegraph.api.app import create_app
from tradegraph.documents.service import DocumentGeneratorService
from tradegraph.kb.loader import ENGINE_PROFILE_ID, compile_kb
from tradegraph.kb.validation import resolve_runtime_classpath
from tradegraph.optimization.export_service import ExportComplianceAdvisor, ExportSpec
from tradegraph.optimization.service import MaterialInput, MultiFtaOptimizer, ShipmentSpec

KB_PATH = Path(__file__).resolve().parents[2] / "knowledge" / "tradegraph.ttl"


@pytest.fixture(scope="module")
def optimizer() -> MultiFtaOptimizer:
    compiled = compile_kb(KB_PATH, ENGINE_PROFILE_ID)
    return MultiFtaOptimizer(compiled, resolve_runtime_classpath())


@pytest.fixture(scope="module")
def doc_service(optimizer: MultiFtaOptimizer) -> DocumentGeneratorService:
    return DocumentGeneratorService(optimizer)


@pytest.fixture(scope="module")
def export_advisor(optimizer: MultiFtaOptimizer) -> ExportComplianceAdvisor:
    return ExportComplianceAdvisor(optimizer)


@pytest.fixture(scope="module")
def client(optimizer: MultiFtaOptimizer, tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    db_path = tmp_path_factory.mktemp("docs_api") / "cases.db"
    app = create_app(optimizer=optimizer, case_db_path=db_path)
    return TestClient(app)


def test_kb_catalog_loads_all_15_document_draft_templates(doc_service: DocumentGeneratorService) -> None:
    templates = doc_service.list_templates()
    codes = {t.template_code for t in templates}
    assert len(templates) == 15
    assert {
        "atiga-form-d",
        "rcep-form-rcep",
        "acfta-form-e",
        "ijepa-form-jiepa",
        "ikcepa-form-kicepa",
        "iacepa-form-iacepa",
        "rcep-dv20-statement",
        "lartas-persetujuan-impor",
        "lartas-laporan-surveyor",
        "bpom-ski",
        "bpom-izin-edar",
        "pib-bc20-draft",
        "export-lartas-permit",
        "peb-bc30-draft",
        "back-to-back-co",
    } == codes
    for tpl in templates:
        assert tpl.title
        assert tpl.issuing_portal
        assert len(tpl.fields) >= 6
        assert any(f.key == "hs_code" for f in tpl.fields)


def test_prepare_import_steel_rcep_dv20_lartas_and_autofill(
    optimizer: MultiFtaOptimizer,
    doc_service: DocumentGeneratorService,
) -> None:
    spec = ShipmentSpec(
        hs_code="7209.26.10",
        dispatch_country="TH",
        destination_country="ID",
        as_of_date="2025-09-15",
        cif_value=Decimal("100000"),
        currency="USD",
        has_api=True,
        api_type="API-P",
        materials=(
            MaterialInput(
                hs_code="7208.54.10",
                non_originating=True,
                origin_country="JP",
                origin_proof_verified=True,
            ),
        ),
        rvc_percent=Decimal("30.00"),
        highest_contributor_country="japan",
        include_plan=True,
        include_explain=True,
    )
    opt_res = optimizer.optimize(spec)
    pkg = doc_service.prepare_from_recommendation(
        mode="import",
        recommendation=opt_res.to_dict(),
        user_info={"name": "Budi Santoso", "company": "PT Baja Nusantara Tbk"},
        extra_inputs={"rvc_percent": "30.00%", "highest_contributor_country": "JP"},
    )
    assert not pkg.blocked
    assert pkg.hs_code == "7209.26.10"
    assert pkg.recommended_agreement == "rcep"

    codes = [d.template_code for d in pkg.documents]
    assert codes == [
        "rcep-form-rcep",
        "rcep-dv20-statement",
        "lartas-laporan-surveyor",
        "lartas-persetujuan-impor",
        "pib-bc20-draft",
    ]
    rcep_doc = pkg.documents[0]
    assert rcep_doc.prefilled_values["hs_code"] == "7209.26.10"
    assert rcep_doc.prefilled_values["company_name"] == "PT Baja Nusantara Tbk"
    assert rcep_doc.prefilled_values["signatory_name"] == "Budi Santoso"
    assert "69" in rcep_doc.prefilled_values["agreement_scheme"]
    assert "USD 100000.00" in rcep_doc.prefilled_values["customs_value"]
    assert "company_npwp" in rcep_doc.missing_required_fields
    assert len(rcep_doc.citations) >= 1


def test_prepare_prohibited_import_and_export_are_hard_blocked(
    optimizer: MultiFtaOptimizer,
    export_advisor: ExportComplianceAdvisor,
    doc_service: DocumentGeneratorService,
) -> None:
    imp_res = optimizer.optimize(
        ShipmentSpec(
            hs_code="6309.00.00",
            dispatch_country="TH",
            destination_country="ID",
            as_of_date="2026-06-15",
            cif_value=Decimal("20000"),
        )
    )
    imp_pkg = doc_service.prepare_from_recommendation(mode="import", recommendation=imp_res.to_dict())
    assert imp_pkg.blocked is True
    assert imp_pkg.documents == ()
    assert imp_pkg.block_reason

    exp_res = export_advisor.evaluate(
        ExportSpec(
            hs_code="2604.00.00",
            origin_country="ID",
            destination_country="CN",
            as_of_date="2025-09-15",
            fob_value=Decimal("500000"),
            wo_confirmed=True,
        )
    )
    exp_pkg = doc_service.prepare_from_recommendation(mode="export", recommendation=exp_res.to_dict())
    assert exp_pkg.blocked is True
    assert exp_pkg.documents == ()


def test_prepare_export_refined_tin_back_to_back_and_permits(
    export_advisor: ExportComplianceAdvisor,
    doc_service: DocumentGeneratorService,
) -> None:
    exp_res = export_advisor.evaluate(
        ExportSpec(
            hs_code="8001.10.00",
            origin_country="ID",
            destination_country="JP",
            as_of_date="2025-09-15",
            fob_value=Decimal("250000"),
            rvc_percent=Decimal("65.00"),
            transshipment_hub="SG",
            requires_back_to_back=True,
            third_party_invoice_country="HK",
            include_explain=True,
        )
    )
    pkg = doc_service.prepare_from_recommendation(
        mode="export",
        recommendation=exp_res.to_dict(),
        extra_inputs={"customs_value": "USD 250000 (FOB)"},
    )
    assert not pkg.blocked
    assert pkg.recommended_agreement == "rcep"
    codes = [d.template_code for d in pkg.documents]
    # ET, PE, LS share export-lartas-permit with distinct target_doc_type, plus PEB, Form RCEP, and Back-to-Back CO
    assert codes.count("export-lartas-permit") == 3
    assert "peb-bc30-draft" in codes
    assert "rcep-form-rcep" in codes
    assert "back-to-back-co" in codes

    b2b = next(d for d in pkg.documents if d.template_code == "back-to-back-co")
    assert b2b.prefilled_values["transshipment_hub"] == "SINGAPORE"
    rcep = next(d for d in pkg.documents if d.template_code == "rcep-form-rcep")
    assert "HK" in rcep.prefilled_values["third_party_invoice_info"]


def test_generate_valid_docx_html_and_zip_bundle(doc_service: DocumentGeneratorService) -> None:
    fields = {
        "company_name": "PT Baja Nusantara Tbk",
        "company_npwp": "01.234.567.8-011.000",
        "company_nib": "9120001234567",
        "hs_code": "7209.26.10",
        "invoice_number": "INV-2025-999",
        "signatory_name": "Budi Santoso",
    }
    docx_bytes, filename = doc_service.generate_docx(
        "rcep-form-rcep",
        fields,
        requirement_reason="Klaim tarif RCEP kode fasilitas 69",
    )
    assert filename == "draft-rcep-form-rcep-7209-26-10.docx"
    with zipfile.ZipFile(io.BytesIO(docx_bytes), "r") as zin:
        assert zin.testzip() is None
        names = set(zin.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= names
        doc_xml = zin.read("word/document.xml")
        root = etree.fromstring(doc_xml)
        all_text = " ".join(root.itertext())
        assert "PT Baja Nusantara Tbk" in all_text
        assert "01.234.567.8-011.000" in all_text
        assert "INV-2025-999" in all_text
        assert "7209.26.10" in all_text

    html_doc = doc_service.generate_html_preview("rcep-form-rcep", fields)
    assert "<!DOCTYPE html>" in html_doc
    assert "PT Baja Nusantara Tbk" in html_doc
    assert "INV-2025-999" in html_doc

    zip_bytes = doc_service.generate_bundle_zip([
        {"template_code": "rcep-form-rcep", "fields": fields},
        {"template_code": "pib-bc20-draft", "fields": fields},
        {"template_code": "export-lartas-permit", "fields": fields, "custom_title": "Draf Permohonan ET"},
        {"template_code": "export-lartas-permit", "fields": fields, "custom_title": "Draf Permohonan PE"},
    ])
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zbundle:
        assert zbundle.testzip() is None
        assert len(zbundle.namelist()) == 4
        assert len(set(zbundle.namelist())) == 4


def test_document_api_endpoints_end_to_end(client: TestClient) -> None:
    # 1. List templates
    r_tpl = client.get("/api/v1/documents/templates")
    assert r_tpl.status_code == 200
    assert r_tpl.json()["count"] == 15

    # 2. Demo customer login to verify user auto-fill on /prepare
    login = client.post("/api/v1/auth/demo", json={"role": "user"})
    assert login.status_code == 200
    token = login.json()["token"]
    user = login.json()["user"]

    # 3. Optimize a shipment and prepare its documents
    opt = client.post(
        "/api/v1/optimize",
        json={
            "hs_code": "8422.11.00",
            "dispatch_country": "CN",
            "destination_country": "ID",
            "as_of_date": "2025-09-15",
            "cif_value": "100000",
            "api_type": "API-U",
            "materials": [{"hs_code": "8422.90.10", "non_originating": True}],
            "rvc_percent": "30.00",
            "include_plan": True,
            "include_explain": True,
        },
    )
    assert opt.status_code == 200

    prep = client.post(
        "/api/v1/documents/prepare",
        headers={"Authorization": f"Bearer {token}"},
        json={"mode": "import", "recommendation": opt.json()},
    )
    assert prep.status_code == 200
    pkg = prep.json()
    assert pkg["blocked"] is False
    assert [d["template_code"] for d in pkg["documents"]] == ["rcep-form-rcep", "pib-bc20-draft"]
    assert pkg["documents"][0]["prefilled_values"]["company_name"] == user["company"]
    assert pkg["documents"][0]["prefilled_values"]["signatory_name"] == user["name"]

    # 4. Generate single .docx and HTML preview
    gen_docx = client.post(
        "/api/v1/documents/generate",
        json={
            "template_code": "rcep-form-rcep",
            "fields": {**pkg["documents"][0]["prefilled_values"], "invoice_number": "INV-API-77"},
            "format": "docx",
        },
    )
    assert gen_docx.status_code == 200
    assert "wordprocessingml" in gen_docx.headers["content-type"]
    with zipfile.ZipFile(io.BytesIO(gen_docx.content), "r") as zf:
        assert zf.testzip() is None

    gen_html = client.post(
        "/api/v1/documents/generate",
        json={"template_code": "rcep-form-rcep", "fields": {"hs_code": "8422.11.00"}, "format": "html"},
    )
    assert gen_html.status_code == 200
    assert "text/html" in gen_html.headers["content-type"]
    assert "8422.11.00" in gen_html.text

    # 5. Generate ZIP bundle
    gen_zip = client.post(
        "/api/v1/documents/generate-bundle",
        json={
            "hs_code": "8422.11.00",
            "items": [
                {"template_code": d["template_code"], "fields": d["prefilled_values"]}
                for d in pkg["documents"]
            ],
        },
    )
    assert gen_zip.status_code == 200
    assert gen_zip.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(gen_zip.content), "r") as zf:
        assert zf.testzip() is None
        assert len(zf.namelist()) == 2
