from pathlib import Path

from tradegraph.sources import parsers


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "sources" / "manifest.json"


def test_parse_atiga_psr_sample_pages_single_and_multiline():
    pdf_path, meta = parsers.resolve_manifest_source(MANIFEST, "ATIGA-PSR22")

    # Page 1: single-line WO rows
    page1 = parsers.parse_atiga_psr_pdf(pdf_path, source_meta=meta, start_page=1, end_page=1, max_records=3)
    assert len(page1) == 3
    assert page1[0].row_no == 1
    assert page1[0].hs6 == "0101.21"
    assert page1[0].hs6_digits == "010121"
    assert page1[0].concise_rule == "WO"
    assert "Wholly obtained" in page1[0].full_rule
    assert page1[0].criteria_alternatives == (({"kind": "WO", "raw": "WO"},),)

    # Page 55: multiline concise rule ('RVC40 or CC or textile process rule') + vertically centered full rule
    page55 = parsers.parse_atiga_psr_pdf(pdf_path, source_meta=meta, start_page=55, end_page=55, max_records=2)
    assert page55[0].row_no == 1287
    assert page55[0].hs6 == "5801.36"
    assert page55[0].concise_rule == "RVC40 or CC or textile process rule"
    assert "subheading 5801.36" in page55[0].full_rule
    assert [alt[0]["kind"] for alt in page55[0].criteria_alternatives] == ["RVC", "CC", "TEXTILE_PROCESS"]

    # Page 93: reference pump row 2042 (8413.70)
    page93 = parsers.parse_atiga_psr_pdf(pdf_path, source_meta=meta, start_page=93, end_page=93)
    pump = next(row for row in page93 if row.row_no == 2042)
    assert pump.hs6 == "8413.70"
    assert pump.concise_rule == "RVC40 or CTSH"
    assert pump.locator == "Annex 3 HS 2022, baris 2042 (8413.70)"
    assert pump.criteria_alternatives == (
        ({"kind": "RVC", "threshold_percent": "40", "raw": "RVC40"},),
        ({"kind": "CTSH", "raw": "CTSH"},),
    )


def test_parse_pmk_tariff_sample_page_matches_reference_pump_part():
    pdf_path, meta = parsers.resolve_manifest_source(MANIFEST, "ID-TARIF-ATIGA")
    rows = parsers.parse_pmk_tariff_pdf(pdf_path, source_meta=meta, start_page=291, end_page=291)
    assert len(rows) >= 10

    part = next(row for row in rows if row.row_no == 7590)
    assert part.hs8 == "8413.91.30"
    assert part.hs8_digits == "84139130"
    assert part.hs6 == "8413.91"
    assert part.tariff_rate_percent == "0.00"
    assert "8413.70.11" in part.description_id
    assert "8413.70.11" in part.description_en
    assert part.locator == "Lampiran, baris 7590 (8413.91.30)"


def test_parse_indonesian_statutes_clean_and_ocr_samples():
    # Clean digital PDF: Permendag 16/2025 pages 7-8
    permendag_path, permendag_meta = parsers.resolve_manifest_source(MANIFEST, "ID-IMPOR-GENERAL")
    clauses = parsers.parse_indonesian_statute_pdf(
        permendag_path, source_meta=permendag_meta, start_page=7, end_page=8, max_records=3,
    )
    assert clauses
    pasal2 = next(c for c in clauses if c.pasal == "Pasal 2")
    assert pasal2.bab == "BAB II"
    assert len(pasal2.ayat) >= 2
    assert pasal2.ayat[0]["ayat"] == "1"
    assert "kegiatan usaha" in pasal2.text.lower()

    # OCR'd PDF: PMK 178/2017 page 5 ('Pasa l 3' normalized to 'Pasal 3')
    pmk_path, pmk_meta = parsers.resolve_manifest_source(MANIFEST, "ID-IMPOR-SEMENTARA")
    ocr_clauses = parsers.parse_indonesian_statute_pdf(
        pmk_path, source_meta=pmk_meta, start_page=5, end_page=5, max_records=2,
    )
    assert any(c.pasal == "Pasal 3" for c in ocr_clauses)
    pasal3 = next(c for c in ocr_clauses if c.pasal == "Pasal 3")
    assert len(pasal3.ayat) >= 1
    assert any(ref.lower().startswith("pasal 2") for ref in pasal3.cross_references)


def test_parse_archived_guidance_html_and_jsonl_roundtrip(tmp_path):
    html_path, meta = parsers.resolve_manifest_source(MANIFEST, "SG-SFA-PROCESSED")
    sections = parsers.parse_guidance_html(html_path, source_meta=meta, max_records=5)
    assert len(sections) == 5
    assert all(sec.heading and sec.text for sec in sections)

    out = tmp_path / "sections.jsonl"
    written = parsers.write_jsonl(out, sections)
    assert written == 5
    reloaded = parsers.read_jsonl(out)
    assert len(reloaded) == 5
    assert reloaded[0]["record_kind"] == "guidance_section"
    assert reloaded[0]["source_key"] == "SG-SFA-PROCESSED"


def test_automated_cross_check_accepts_consistent_and_flags_mismatched_records():
    good_psr = {
        "record_kind": "psr",
        "row_no": 2042,
        "hs6": "8413.70",
        "hs6_digits": "841370",
        "concise_rule": "RVC40 or CTSH",
        "full_rule": "A regional value content of not less than 40 percent; or A change to subheading 8413.70 from any other subheading",
        "criteria_alternatives": [[{"kind": "RVC", "threshold_percent": "40"}], [{"kind": "CTSH"}]],
    }
    bad_psr = {
        **good_psr,
        "hs6": "8413.81",
        "hs6_digits": "841381",
        "concise_rule": "WO",
        "criteria_alternatives": [[{"kind": "WO"}]],
    }
    bad_tariff = {
        "record_kind": "tariff",
        "row_no": 1,
        "hs8": "8413.70.11",
        "hs8_digits": "84137011",
        "hs6": "8413.70",
        "description_id": "Pompa",
        "description_en": "Pump",
        "tariff_rate_percent": None,
    }
    checked = parsers.verify_records([good_psr, bad_psr, bad_tariff])
    assert len(checked.verified) == 1
    assert len(checked.flagged) == 2
    assert any("WO" in r for r in checked.flagged[0].reasons)
    assert any("8413.70" in r for r in checked.flagged[0].reasons)
    assert any("tariff_rate_percent" in r for r in checked.flagged[1].reasons)


def test_parse_rcep_psr_chapter_heading_and_subheading_levels():
    pdf_path, meta = parsers.resolve_manifest_source(MANIFEST, "RCEP-PSR22")

    # Page 1: Chapter-level rules (01 -> WO, 02 -> CC except from Chapter 01)
    p1 = parsers.parse_rcep_psr_pdf(pdf_path, source_meta=meta, start_page=1, end_page=1)
    ch1 = next(r for r in p1 if r.hs_code == "01")
    ch2 = next(r for r in p1 if r.hs_code == "02")
    assert ch1.agreement == "RCEP" and ch1.hs_level == "CHAPTER"
    assert ch1.criteria_alternatives == (({"kind": "WO", "raw": "WO"},),)
    assert ch2.hs_level == "CHAPTER"
    assert ch2.criteria_alternatives == (({"kind": "CC", "except_from": "01", "raw": "CC except from Chapter 01"},),)

    # Page 82: Heading-level rules with comma-or and range exclusions (72.09, 72.10, 72.12)
    p82 = parsers.parse_rcep_psr_pdf(pdf_path, source_meta=meta, start_page=82, end_page=82)
    h7209 = next(r for r in p82 if r.hs_code == "72.09")
    h7212 = next(r for r in p82 if r.hs_code == "72.12")
    assert h7209.hs_level == "HEADING" and h7209.hs_digits == "7209"
    assert h7209.criteria_alternatives == (
        ({"kind": "CTH", "except_from": "7208", "raw": "CTH except from heading 72.08"},),
        ({"kind": "RVC", "threshold_percent": "40", "raw": "RVC40"},),
    )
    assert h7212.criteria_alternatives[0][0]["except_from"] == "7208, 7209, 7210, 7211"

    # Page 13 & 97: Subheading-level rules (0901.21 -> RVC40, 0902.10 -> WO, 8422.11 -> CTSH or RVC40)
    p13 = parsers.parse_rcep_psr_pdf(pdf_path, source_meta=meta, start_page=13, end_page=13)
    p97 = parsers.parse_rcep_psr_pdf(pdf_path, source_meta=meta, start_page=97, end_page=97)
    tea = next(r for r in p13 if r.hs_code == "0902.10")
    dw = next(r for r in p97 if r.hs_code == "8422.11")
    assert tea.hs_level == "SUBHEADING" and tea.concise_rule == "WO"
    assert dw.hs_level == "SUBHEADING" and [a[0]["kind"] for a in dw.criteria_alternatives] == ["CTSH", "RVC"]

    checked = parsers.verify_records(p1 + p13 + p82 + p97)
    assert len(checked.flagged) == 0


def test_parse_multi_fta_tariffs_rcep_staging_and_appendix_dv20():
    # MFN (PMK 26/2022) & ACFTA (PMK 46/2022) & IJEPA (PMK 50/2022 MFN exclusion) on 7209.26.10
    mfn_pdf, mfn_meta = parsers.resolve_manifest_source(MANIFEST, "ID-TARIF-MFN")
    mfn_rows = parsers.parse_pmk_tariff_pdf(mfn_pdf, source_meta=mfn_meta, start_page=466, end_page=466, agreement="MFN")
    mfn_steel = next(r for r in mfn_rows if r.hs8 == "7209.26.10")
    assert mfn_steel.tariff_rate_percent == "15.00" and not mfn_steel.mfn_excluded

    acfta_pdf, acfta_meta = parsers.resolve_manifest_source(MANIFEST, "ID-TARIF-ACFTA")
    acfta_rows = parsers.parse_pmk_tariff_pdf(acfta_pdf, source_meta=acfta_meta, start_page=244, end_page=244, agreement="ACFTA")
    acfta_steel = next(r for r in acfta_rows if r.hs8 == "7209.26.10")
    assert acfta_steel.tariff_rate_percent == "5.00" and not acfta_steel.mfn_excluded

    ijepa_pdf, ijepa_meta = parsers.resolve_manifest_source(MANIFEST, "ID-TARIF-IJEPA")
    ijepa_rows = parsers.parse_pmk_tariff_pdf(
        ijepa_pdf, source_meta=ijepa_meta, start_page=250, end_page=250, agreement="IJEPA", partner_countries=("japan",),
    )
    ijepa_steel = next(r for r in ijepa_rows if r.hs8 == "7209.26.10")
    assert ijepa_steel.mfn_excluded is True and ijepa_steel.tariff_rate_percent is None

    # RCEP ASEAN (PMK 221/2022) staged 2023-2027 schedule vs RCEP China (PMK 224/2022) MFN exclusion on 7209.26.10
    rcep_asean_pdf, rcep_asean_meta = parsers.resolve_manifest_source(MANIFEST, "ID-TARIF-RCEP-ASEAN")
    asean_steel = parsers.parse_rcep_staged_tariff_pdf(
        rcep_asean_pdf, source_meta=rcep_asean_meta, partner_countries=("thailand",),
        start_page=311, end_page=311, hs8_filter={"7209.26.10"},
    )
    by_period = {r.effective_from: r.tariff_rate_percent for r in asean_steel}
    assert by_period["2025-04-01"] == "6.60"
    assert by_period["2026-04-01"] == "5.90"

    rcep_cn_pdf, rcep_cn_meta = parsers.resolve_manifest_source(MANIFEST, "ID-TARIF-RCEP-CN")
    cn_steel = parsers.parse_rcep_staged_tariff_pdf(
        rcep_cn_pdf, source_meta=rcep_cn_meta, partner_countries=("china",),
        start_page=301, end_page=301, hs8_filter={"7209.26.10"},
    )
    assert len(cn_steel) == 5 and all(r.mfn_excluded for r in cn_steel)

    cn_pump = parsers.parse_rcep_staged_tariff_pdf(
        rcep_cn_pdf, source_meta=rcep_cn_meta, partner_countries=("china",),
        start_page=360, end_page=360, hs8_filter={"8413.70.11"},
    )
    pump_by_period = {r.effective_from: r.tariff_rate_percent for r in cn_pump}
    assert pump_by_period["2025-04-01"] == "2.50"
    assert pump_by_period["2026-04-01"] == "2.00"

    # RCEP Lampiran II (Appendix to Annex I) DV20 list on p.604 includes 7209.26.10 (row 673)
    appendix = parsers.parse_rcep_appendix_pdf(
        rcep_asean_pdf, source_meta=rcep_asean_meta, start_page=604, end_page=604,
    )
    steel_app = next(r for r in appendix if r["hs8"] == "7209.26.10")
    assert steel_app["row_no"] == 673 and steel_app["record_kind"] == "rcep_appendix"

    checked = parsers.verify_records([mfn_steel, acfta_steel, ijepa_steel] + asean_steel + cn_steel + cn_pump + appendix)
    assert len(checked.flagged) == 0


def test_parse_permendag_lartas_and_prohibited_pdfs():
    # Permendag 17/2025 (Tekstil): PI + LS at BORDER on 5801.36.10 (p.42) and 6101.20.00 (p.80)
    t_pdf, t_meta = parsers.resolve_manifest_source(MANIFEST, "ID-LARTAS-TEKSTIL")
    velvet = parsers.parse_permendag_lartas_pdf(
        t_pdf, source_meta=t_meta, commodity_group="Tekstil dan Produk Tekstil",
        start_page=42, end_page=42, effective_from="2025-08-29", hs8_filter={"5801.36.10"},
    )
    coat = parsers.parse_permendag_lartas_pdf(
        t_pdf, source_meta=t_meta, commodity_group="Pakaian Jadi dan Aksesori Pakaian Jadi",
        start_page=80, end_page=80, effective_from="2025-08-29", hs8_filter={"6101.20.00"},
    )
    assert len(velvet) == 1 and velvet[0].requires_pi and velvet[0].requires_ls
    assert velvet[0].supervision_mode == "BORDER" and velvet[0].required_doc_types == ("persetujuanImpor", "laporanSurveyor")
    assert len(coat) == 1 and coat[0].requires_pi and coat[0].requires_ls and coat[0].supervision_mode == "BORDER"

    # Permendag 18/2025 (Pertanian): PI ONLY + POST_BORDER on 0101.21.00 (p.18) — X-coordinate disambiguates '√ √' from PI+LS!
    p_pdf, p_meta = parsers.resolve_manifest_source(MANIFEST, "ID-LARTAS-PERTANIAN")
    horse = parsers.parse_permendag_lartas_pdf(
        p_pdf, source_meta=p_meta, commodity_group="Hewan dan Produk Hewan",
        start_page=18, end_page=18, effective_from="2025-08-29", hs8_filter={"0101.21.00"},
    )
    assert len(horse) == 1 and horse[0].requires_pi and not horse[0].requires_ls
    assert horse[0].supervision_mode == "POST_BORDER" and horse[0].required_doc_types == ("persetujuanImpor",)

    # Permendag 21/2025 (Elektronik): LS ONLY at BORDER on 8413.70.11 (p.43)
    e_pdf, e_meta = parsers.resolve_manifest_source(MANIFEST, "ID-LARTAS-ELEKTRONIK")
    pump = parsers.parse_permendag_lartas_pdf(
        e_pdf, source_meta=e_meta, commodity_group="Barang Elektronik",
        start_page=43, end_page=43, effective_from="2025-08-29", hs8_filter={"8413.70.11"},
    )
    assert len(pump) == 1 and not pump[0].requires_pi and pump[0].requires_ls
    assert pump[0].supervision_mode == "BORDER" and pump[0].required_doc_types == ("laporanSurveyor",)

    # Permendag 22/2025 (Industri Tertentu): PI + LS at BORDER on 7209.26.10 (p.20)
    i_pdf, i_meta = parsers.resolve_manifest_source(MANIFEST, "ID-LARTAS-INDUSTRI")
    steel = parsers.parse_permendag_lartas_pdf(
        i_pdf, source_meta=i_meta, commodity_group="Besi atau Baja, Baja Paduan, dan Produk Turunannya",
        start_page=20, end_page=20, effective_from="2025-08-29", hs8_filter={"7209.26.10"},
    )
    assert len(steel) == 1 and steel[0].requires_pi and steel[0].requires_ls and steel[0].supervision_mode == "BORDER"

    # Permendag 47/2025 (Barang Dilarang Impor): PROHIBITED on 6309.00.00 (Lampiran IV p.11)
    b_pdf, b_meta = parsers.resolve_manifest_source(MANIFEST, "ID-LARTAS-LARANGAN")
    worn = parsers.parse_permendag_prohibited_pdf(
        b_pdf, source_meta=b_meta, commodity_group="Kantong Bekas, Karung Bekas, dan Pakaian Bekas",
        start_page=11, end_page=11, effective_from="2026-01-01", hs8_filter={"6309.00.00"},
    )
    assert len(worn) == 1 and worn[0].prohibited is True and worn[0].supervision_mode == "PROHIBITED"

    checked = parsers.verify_records(velvet + coat + horse + pump + steel + worn)
    assert len(checked.verified) == 6 and len(checked.flagged) == 0

    bad = parsers.verify_records([{"record_kind": "lartas", "hs8": "6309.00.00", "hs8_digits": "63090000", "supervision_mode": "BORDER", "prohibited": True}])
    assert len(bad.flagged) == 1


def test_parse_ikcepa_iacepa_rcep_au_kr_and_binary_page_locator():
    # IK-CEPA (PMK 227/2022): binary search + split-span OCR merging on 7210.12.10 (0.00%) & 8507.60.33 (0.00%)
    ik_pdf, ik_meta = parsers.resolve_manifest_source(MANIFEST, "ID-TARIF-IKCEPA")
    ik_loc = parsers.locate_hs8_pages(ik_pdf, ["7210.12.10", "8507.60.33"], start_page=7)
    assert set(ik_loc) == {"7210.12.10", "8507.60.33"}
    ik_rows = []
    for pg in sorted(set(ik_loc.values())):
        ik_rows.extend(parsers.parse_ikcepa_staged_tariff_pdf(
            ik_pdf, source_meta=ik_meta, start_page=pg, end_page=pg, hs8_filter={"7210.12.10", "8507.60.33"},
        ))
    assert any(r.hs8 == "7210.12.10" and r.effective_from == "2025-01-01" and r.tariff_rate_percent == "0.00" for r in ik_rows)
    assert any(r.hs8 == "8507.60.33" and r.effective_from == "2025-01-01" and r.tariff_rate_percent == "0.00" for r in ik_rows)

    # IA-CEPA (PMK 54/2022): 7209.26.10 drops from 11.25% (2024) to 4.50% (2025+)
    ia_pdf, ia_meta = parsers.resolve_manifest_source(MANIFEST, "ID-TARIF-IACEPA")
    ia_loc = parsers.locate_hs8_pages(ia_pdf, ["7209.26.10"], start_page=10)
    ia_rows = parsers.parse_iacepa_staged_tariff_pdf(
        ia_pdf, source_meta=ia_meta, start_page=ia_loc["7209.26.10"], end_page=ia_loc["7209.26.10"], hs8_filter={"7209.26.10"},
    )
    ia_by_year = {r.effective_from: r.tariff_rate_percent for r in ia_rows}
    assert ia_by_year["2024-01-01"] == "11.25"
    assert ia_by_year["2025-01-01"] == "4.50"

    # RCEP AU (PMK 222/2022) & RCEP KR (PMK 223/2022): 7209.26.10 is 6.60% (2025) for AU and MFN for KR
    au_pdf, au_meta = parsers.resolve_manifest_source(MANIFEST, "ID-TARIF-RCEP-AU")
    au_loc = parsers.locate_hs8_pages(au_pdf, ["7209.26.10"], start_page=7)
    au_rows = parsers.parse_rcep_staged_tariff_pdf(
        au_pdf, source_meta=au_meta, partner_countries=("australia",),
        start_page=au_loc["7209.26.10"], end_page=au_loc["7209.26.10"], hs8_filter={"7209.26.10"},
    )
    assert any(r.effective_from == "2025-04-01" and r.tariff_rate_percent == "6.60" for r in au_rows)

    kr_pdf, kr_meta = parsers.resolve_manifest_source(MANIFEST, "ID-TARIF-RCEP-KR")
    kr_loc = parsers.locate_hs8_pages(kr_pdf, ["7209.26.10"], start_page=7)
    kr_rows = parsers.parse_rcep_staged_tariff_pdf(
        kr_pdf, source_meta=kr_meta, partner_countries=("southKorea",),
        start_page=kr_loc["7209.26.10"], end_page=kr_loc["7209.26.10"], hs8_filter={"7209.26.10"},
    )
    assert len(kr_rows) >= 5 and all(r.mfn_excluded for r in kr_rows)

    # Permendag 47/2025 default include_ex=False skips conditional HCFC ban 'ex 8415.10.20' on p.15
    ban_pdf, ban_meta = parsers.resolve_manifest_source(MANIFEST, "ID-LARTAS-LARANGAN")
    assert parsers.parse_permendag_prohibited_pdf(
        ban_pdf, source_meta=ban_meta, commodity_group="Pendingin", start_page=15, end_page=15, hs8_filter={"8415.10.20"},
    ) == []

    checked = parsers.verify_records(ik_rows + ia_rows + au_rows + kr_rows)
    assert len(checked.flagged) == 0


def test_parse_permendag_export_lartas_and_prohibited_export_pdfs():
    # Permendag 9/2025 (Kebijakan dan Pengaturan Ekspor):
    # - ex 8001.10.00 (Refined Tin, p.322): ET + PE + LS
    # - 2701.11.00 (Anthracite Coal, p.441): ET + LS (no PE)
    # - 7204.21.00 (Stainless Steel Scrap, p.353): PE only
    exp_pdf, exp_meta = parsers.resolve_manifest_source(MANIFEST, "ID-LARTAS-EKSPOR-AMEND25")
    tin = parsers.parse_permendag_export_lartas_pdf(
        exp_pdf, source_meta=exp_meta, commodity_group="Timah Murni Batangan",
        start_page=322, end_page=322, effective_from="2025-03-13", hs8_filter={"8001.10.00"},
    )
    coal = parsers.parse_permendag_export_lartas_pdf(
        exp_pdf, source_meta=exp_meta, commodity_group="Batubara",
        start_page=441, end_page=441, effective_from="2025-03-13", hs8_filter={"2701.11.00"},
    )
    scrap = parsers.parse_permendag_export_lartas_pdf(
        exp_pdf, source_meta=exp_meta, commodity_group="Sisa dan Skrap Logam",
        start_page=353, end_page=353, effective_from="2025-03-13", hs8_filter={"7204.21.00"},
    )
    assert len(tin) == 1 and tin[0].requires_et and tin[0].requires_pe and tin[0].requires_ls
    assert tin[0].required_doc_types == ("eksportirTerdaftar", "persetujuanEkspor", "laporanSurveyorEkspor")
    assert len(coal) == 1 and coal[0].requires_et and not coal[0].requires_pe and coal[0].requires_ls
    assert coal[0].required_doc_types == ("eksportirTerdaftar", "laporanSurveyorEkspor")
    assert len(scrap) == 1 and not scrap[0].requires_et and scrap[0].requires_pe and not scrap[0].requires_ls
    assert scrap[0].required_doc_types == ("persetujuanEkspor",)

    # Permendag 8/2025 (Barang Dilarang Ekspor):
    # - 1401.20.10 (Whole Raw Rattan, p.5) & 2604.00.00 (Nickel Ores, p.23): PROHIBITED_EXPORT
    ban_pdf, ban_meta = parsers.resolve_manifest_source(MANIFEST, "ID-LARTAS-EKSPOR-LARANGAN-AMEND25")
    rattan = parsers.parse_permendag_prohibited_export_pdf(
        ban_pdf, source_meta=ban_meta, commodity_group="Produk Kehutanan (Rotan)",
        start_page=5, end_page=5, effective_from="2025-03-13", hs8_filter={"1401.20.10"},
    )
    nickel = parsers.parse_permendag_prohibited_export_pdf(
        ban_pdf, source_meta=ban_meta, commodity_group="Produk Pertambangan (Bijih Nikel)",
        start_page=23, end_page=23, effective_from="2025-03-13", hs8_filter={"2604.00.00"},
    )
    assert len(rattan) == 1 and rattan[0].prohibited is True and rattan[0].supervision_mode == "PROHIBITED_EXPORT"
    assert len(nickel) == 1 and nickel[0].prohibited is True and nickel[0].supervision_mode == "PROHIBITED_EXPORT"

    checked = parsers.verify_records(tin + coal + scrap + rattan + nickel)
    assert len(checked.verified) == 5 and len(checked.flagged) == 0

    bad = parsers.verify_records([{
        "record_kind": "export_lartas",
        "hs8": "2604.00.00",
        "hs8_digits": "26040000",
        "supervision_mode": "RESTRICTED_EXPORT",
        "prohibited": True,
    }])
    assert len(bad.flagged) == 1


def test_parse_pmk_export_duty_pdfs():
    # PMK 38/2024 vs PMK 68/2025:
    # - 1511.10.00 (CPO): 17 specific USD/MT tiers (0..288; Col 7 = 124, Col 8 = 148)
    # - 1511.90.36 (RBD Palm Olein bulk vs branded <=25kg ex): Col 7 = 71 (bulk) vs 31 (ex)
    # - 1801.00.10 (Cocoa Beans): 4 ad-valorem tiers cut in half from 0/5/10/15 (PMK 38/2024) to 0/2.5/5/7.5 (PMK 68/2025)
    # - 2603.00.00 (Copper Concentrate): flat 7.50% (Lampiran E)
    # - 4104.11.10 (Wet Blue Leather): flat 15.00% (Lampiran A)
    # - 1301.90.90 (Getah Pinus): newly added flat 25.00% in PMK 68/2025 (Lampiran G)
    hs_set = {"1511.10.00", "1511.90.36", "1801.00.10", "2603.00.00", "4104.11.10", "1301.90.90"}

    p38, m38 = parsers.resolve_manifest_source(MANIFEST, "ID-BEA-KELUAR")
    r38 = parsers.parse_pmk_export_duty_pdf(p38, source_meta=m38, effective_from="2024-06-03", hs8_filter=hs_set)
    cpo38 = {r.tier_column: r.rate_value for r in r38 if r.hs8 == "1511.10.00"}
    assert len(cpo38) == 17 and cpo38[1] == "0.00" and cpo38[7] == "124.00" and cpo38[17] == "288.00"
    cocoa38 = [r.rate_value for r in r38 if r.hs8 == "1801.00.10"]
    assert cocoa38 == ["0.00", "5.00", "10.00", "15.00"]
    assert any(r.hs8 == "2603.00.00" and r.rate_value == "7.50" and r.duty_basis == "AD_VALOREM" for r in r38)
    assert any(r.hs8 == "4104.11.10" and r.rate_value == "15.00" for r in r38)
    assert not any(r.hs8 == "1301.90.90" for r in r38)

    p68, m68 = parsers.resolve_manifest_source(MANIFEST, "ID-BEA-KELUAR-AMEND25")
    r68 = parsers.parse_pmk_export_duty_pdf(p68, source_meta=m68, effective_from="2025-10-22", hs8_filter=hs_set)
    cocoa68 = [r.rate_value for r in r68 if r.hs8 == "1801.00.10"]
    assert cocoa68 == ["0.00", "2.50", "5.00", "7.50"]
    rbd_bulk = {r.tier_column: r.rate_value for r in r68 if r.hs8 == "1511.90.36" and not r.ex_code}
    rbd_pack = {r.tier_column: r.rate_value for r in r68 if r.hs8 == "1511.90.36" and r.ex_code}
    assert rbd_bulk[7] == "71.00" and rbd_pack[7] == "31.00"
    assert any(r.hs8 == "1301.90.90" and r.lampiran_section == "G" and r.rate_value == "25.00" for r in r68)

    checked = parsers.verify_records(r38 + r68)
    assert len(checked.verified) == len(r38) + len(r68) and len(checked.flagged) == 0

    bad = parsers.verify_records([{
        "record_kind": "export_duty",
        "hs8": "1801.00.10",
        "hs8_digits": "18010010",
        "duty_basis": "AD_VALOREM",
        "rate_value": "150.00",
    }])
    assert len(bad.flagged) == 1
