"""Declarative KB-driven Interactive HS Code Classification & Disambiguation Service (BTKI 2022)."""

from __future__ import annotations

from dataclasses import dataclass
import re

from tradegraph.kb.loader import TG
from tradegraph.kb.queries import prepare_local_query
from tradegraph.optimization.export_service import ExportComplianceAdvisor, ExportSpec
from tradegraph.optimization.service import MultiFtaOptimizer, ShipmentSpec


FACET_LABELS: dict[str, dict[str, str]] = {
    "sector": {
        "agro": "Pertanian, Perkebunan & Hasil Hutan",
        "mining": "Pertambangan, Mineral & Logam Dasar",
        "steel": "Besi & Baja (HRC / CRC / Tinplate / Skrap)",
        "machinery": "Mesin Mekanik, Pompa & Peralatan Rumah Tangga",
        "electronics": "Elektronik, Baterai EV & Tenaga Listrik",
        "pharma": "Farmasi & Kesehatan (BPOM)",
        "textile": "Tekstil, Kain & Garmen",
        "consumer": "Barang Konsumen & Set Bepergian",
    },
    "material_group": {
        "plant_crop": "Nabati / Tanaman / Sawit / Kakao / Kopi / Getah",
        "animal_livestock": "Hewani / Ternak / Kulit / Pakan Daging",
        "mineral_ore": "Bijih Mineral / Konsentrat / Batu Bara",
        "metal_alloy": "Logam / Besi / Baja / Timah",
        "mechanical_equipment": "Perangkat Mekanik / Pompa / Mesin",
        "electrical_component": "Komponen Elektrik / Baterai / Motor / Trafo",
        "chemical_pharma": "Senyawa Kimia / Obat Antibiotik",
        "fabric_garment": "Kain / Pakaian / Tekstil",
        "mixed_goods": "Campuran / Set Produk Konsumen",
    },
    "processing_state": {
        "raw_unprocessed": "Mentah / Belum Dimurnikan (Raw / Unprocessed)",
        "refined_processed": "Olahan / Dimurnikan / Sediaan Jadi",
        "hot_rolled": "Canai Panas (Hot-Rolled)",
        "cold_rolled": "Canai Dingin (Cold-Rolled)",
        "plated_coated": "Dilapisi / Dilaminasi / Tinplate",
        "assembled_unit": "Unit Mesin / Produk Jadi Terakit",
        "component_part": "Suku Cadang / Komponen Bagian",
        "used_secondhand": "Barang Bekas Pakai (Secondhand)",
        "scrap_waste": "Sisa / Skrap / Limbah Daur Ulang",
    },
    "intended_application": {
        "industrial_raw_material": "Bahan Baku / Input Industri Manufaktur",
        "automotive_ev": "Kendaraan Bermotor / Mobil Listrik (EV)",
        "household_retail": "Konsumsi Rumah Tangga / Ritel",
        "medical_clinical": "Medis / Klinis / Kesehatan",
        "energy_fuel": "Energi / Bahan Bakar / Kelistrikan",
        "commercial_machinery": "Mesin Komersial / Industri / Pameran",
        "agriculture_breeding": "Pembibitan / Peternakan",
    },
}

FACET_QUESTION_PROMPTS: dict[str, str] = {
    "sector": "Sektor industri utama produk Anda?",
    "material_group": "Kelompok bahan penyusun dominan produk Anda?",
    "processing_state": "Bagaimana tingkat pengolahan atau bentuk fisik barangnya?",
    "intended_application": "Untuk tujuan penggunaan apa barang tersebut dirancang?",
}


@dataclass(frozen=True, slots=True)
class HsProfileEntry:
    hs_code: str
    hs_iri: str
    display_name: str
    btki_label: str
    mfn_duty_percent: str | None
    sector: str
    material_group: str
    processing_state: str
    intended_application: str
    technical_criteria: str
    keywords: tuple[str, ...]

    @property
    def chapter(self) -> str:
        return self.hs_code.replace(".", "")[:2]

    @property
    def heading(self) -> str:
        c = self.hs_code.replace(".", "")
        return f"{c[:2]}.{c[2:4]}"

    @property
    def subheading(self) -> str:
        c = self.hs_code.replace(".", "")
        return f"{c[:4]}.{c[4:6]}"


@dataclass(frozen=True, slots=True)
class HsClassifySpec:
    query_text: str = ""
    sector: str | None = None
    material_group: str | None = None
    processing_state: str | None = None
    intended_application: str | None = None
    as_of_date: str = "2025-09-15"
    direction: str = "both"  # import | export | both
    top_k: int = 8


class HsClassifierService:
    """Interactive attribute-based HS8 classifier backed declaratively by KB CommodityClassificationProfile individuals."""

    def __init__(self, optimizer: MultiFtaOptimizer, export_advisor: ExportComplianceAdvisor):
        self._opt = optimizer
        self._exp = export_advisor
        self.kb = optimizer.kb
        self._owl = optimizer._owl
        self._profiles_cache: tuple[HsProfileEntry, ...] | None = None

    def list_profiles(self) -> tuple[HsProfileEntry, ...]:
        if self._profiles_cache is not None:
            return self._profiles_cache
        qdef = self.kb.query_catalog.get("hs-classification-profiles")
        if qdef is None:
            self._profiles_cache = ()
            return ()
        query = prepare_local_query(qdef.sparql, "SelectQuery")
        out: list[HsProfileEntry] = []
        seen: set[str] = set()
        for row in self._owl.query(query):
            d = row.asdict()
            code = str(d["code"])
            if code in seen:
                continue
            seen.add(code)
            raw_kw = str(d.get("keywords") or "")
            kws = tuple(k.strip().lower() for k in raw_kw.split(",") if k.strip())
            out.append(HsProfileEntry(
                hs_code=code,
                hs_iri=str(d["hs"]),
                display_name=str(d["displayName"]),
                btki_label=str(d.get("hsLabel") or d["displayName"]),
                mfn_duty_percent=str(d["mfn"]) if d.get("mfn") is not None else None,
                sector=str(d["sector"]),
                material_group=str(d["materialGroup"]),
                processing_state=str(d["processingState"]),
                intended_application=str(d["intendedApp"]),
                technical_criteria=str(d["criteria"]),
                keywords=kws,
            ))
        self._profiles_cache = tuple(out)
        return self._profiles_cache

    def get_facets(self) -> dict[str, object]:
        profiles = self.list_profiles()
        facets: dict[str, list[dict[str, object]]] = {}
        for attr in ("sector", "material_group", "processing_state", "intended_application"):
            counts: dict[str, int] = {}
            for p in profiles:
                val = getattr(p, attr)
                counts[val] = counts.get(val, 0) + 1
            facets[attr] = [
                {
                    "value": val,
                    "label": FACET_LABELS.get(attr, {}).get(val, val),
                    "count": counts[val],
                }
                for val in sorted(counts)
            ]
        return {
            "kb_version": self.kb.snapshot.version,
            "total_profiles": len(profiles),
            "facets": facets,
            "prompts": FACET_QUESTION_PROMPTS,
        }

    def classify(self, spec: HsClassifySpec) -> dict[str, object]:
        profiles = self.list_profiles()
        tokens = [
            tok for tok in re.findall(r"[a-z0-9.]+", (spec.query_text or "").lower())
            if len(tok) >= 2
        ]
        selected_facets = {
            "sector": (spec.sector or "").strip() or None,
            "material_group": (spec.material_group or "").strip() or None,
            "processing_state": (spec.processing_state or "").strip() or None,
            "intended_application": (spec.intended_application or "").strip() or None,
        }
        active_facet_count = sum(1 for v in selected_facets.values() if v is not None)

        scored: list[tuple[float, HsProfileEntry, list[str], list[str]]] = []
        for p in profiles:
            matched: list[str] = []
            conflicts: list[str] = []
            score = 0.0
            max_possible = 0.0

            weights = {
                "sector": 30.0,
                "material_group": 25.0,
                "processing_state": 25.0,
                "intended_application": 20.0,
            }
            for fname, fval in selected_facets.items():
                if fval is None:
                    continue
                w = weights[fname]
                max_possible += w
                pval = getattr(p, fname)
                nice_label = FACET_LABELS.get(fname, {}).get(pval, pval)
                if pval == fval:
                    score += w
                    matched.append(f"{fname}: {nice_label}")
                else:
                    conflicts.append(f"{fname}: profil={nice_label} (dipilih={FACET_LABELS.get(fname, {}).get(fval, fval)})")

            if tokens:
                kw_max = 40.0
                max_possible += kw_max
                haystack = " ".join([
                    p.hs_code.lower(),
                    p.hs_code.replace(".", "").lower(),
                    p.display_name.lower(),
                    p.btki_label.lower(),
                    p.technical_criteria.lower(),
                    " ".join(p.keywords),
                ])
                hit_tokens = [tok for tok in tokens if tok in haystack]
                if hit_tokens:
                    frac = len(hit_tokens) / len(tokens)
                    score += kw_max * frac
                    matched.append(f"kata kunci cocok: {', '.join(hit_tokens)}")
                else:
                    conflicts.append(f"tidak memuat kata kunci: {', '.join(tokens[:4])}")

            if active_facet_count == 0 and not tokens:
                pct = 50.0
            elif max_possible > 0:
                pct = round(max(0.0, min(100.0, (score / max_possible) * 100.0)), 1)
            else:
                pct = 0.0

            if active_facet_count > 0 or tokens:
                if score <= 0:
                    continue
            scored.append((pct, p, matched, conflicts))

        scored.sort(key=lambda item: (-item[0], len(item[3]), item[1].hs_code))
        top_items = scored[: max(1, min(spec.top_k, 15))]

        # Compute smart next disambiguation question over top candidates (whose score is within 20 pts of leader)
        disambiguation: list[dict[str, object]] = []
        if len(top_items) > 1:
            leader_score = top_items[0][0]
            contenders = [item[1] for item in top_items if item[0] >= leader_score - 20.0][:6]
            if len(contenders) > 1:
                for fname in ("processing_state", "material_group", "intended_application", "sector"):
                    if selected_facets[fname] is not None:
                        continue
                    distinct_vals = sorted({getattr(c, fname) for c in contenders})
                    if len(distinct_vals) >= 2:
                        disambiguation.append({
                            "facet": fname,
                            "question": FACET_QUESTION_PROMPTS[fname],
                            "options": [
                                {
                                    "value": v,
                                    "label": FACET_LABELS.get(fname, {}).get(v, v),
                                    "matching_hs": [c.hs_code for c in contenders if getattr(c, fname) == v],
                                }
                                for v in distinct_vals
                            ],
                        })

        # Enrich top candidates with live KB Lartas / Export-Lartas / Bea Keluar preview
        candidates_out: list[dict[str, object]] = []
        for pct, p, matched, conflicts in top_items:
            hs_iri = self._opt.resolve_hs_iri(p.hs_code)
            imp_lartas = self._opt._assess_lartas(
                hs_iri,
                TG.jurisdictionIndonesiaCustoms,
                ShipmentSpec(hs_code=p.hs_code, dispatch_country="TH", as_of_date=spec.as_of_date, include_explain=False),
            )
            exp_spec = ExportSpec(hs_code=p.hs_code, destination_country="JP", as_of_date=spec.as_of_date, include_explain=False)
            exp_lartas = self._exp.assess_export_lartas(hs_iri, exp_spec)
            exp_duty = self._exp.assess_export_duty(hs_iri, exp_spec, prohibited=exp_lartas.prohibited)

            candidates_out.append({
                "hs_code": p.hs_code,
                "hs_iri": p.hs_iri,
                "display_name": p.display_name,
                "btki_label": p.btki_label,
                "confidence_percent": pct,
                "mfn_duty_percent": p.mfn_duty_percent,
                "hierarchy": {
                    "chapter": f"Bab {p.chapter}",
                    "heading": f"Pos {p.heading}",
                    "subheading": f"Subpos {p.subheading}",
                    "hs8": p.hs_code,
                },
                "attributes": {
                    "sector": p.sector,
                    "sector_label": FACET_LABELS["sector"].get(p.sector, p.sector),
                    "material_group": p.material_group,
                    "material_group_label": FACET_LABELS["material_group"].get(p.material_group, p.material_group),
                    "processing_state": p.processing_state,
                    "processing_state_label": FACET_LABELS["processing_state"].get(p.processing_state, p.processing_state),
                    "intended_application": p.intended_application,
                    "intended_application_label": FACET_LABELS["intended_application"].get(p.intended_application, p.intended_application),
                },
                "technical_criteria": p.technical_criteria,
                "matched_attributes": matched,
                "conflicting_attributes": conflicts,
                "compliance_preview": {
                    "import_lartas_status": imp_lartas.status,
                    "import_prohibited": imp_lartas.prohibited,
                    "export_lartas_status": exp_lartas.status,
                    "export_prohibited": exp_lartas.prohibited,
                    "subject_to_export_duty": exp_duty.subject_to_export_duty,
                },
            })

        return {
            "kb_version": self.kb.snapshot.version,
            "as_of_date": spec.as_of_date,
            "query_text": spec.query_text,
            "selected_facets": selected_facets,
            "total_matched": len(scored),
            "candidates": candidates_out,
            "disambiguation_questions": disambiguation,
        }
