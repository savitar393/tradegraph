"""Counterfactual BOM, Sourcing & Route Repair Optimizer backed by live OWL 2 DL + HermiT + SWRL re-evaluation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from rdflib import URIRef

from tradegraph.kb.loader import TG
from tradegraph.optimization.export_service import ExportComplianceAdvisor, ExportSpec
from tradegraph.optimization.service import MaterialInput, MultiFtaOptimizer, ShipmentSpec


def _q2(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class CounterfactualSpec:
    direction: str = "import"  # import | export
    hs_code: str = "8507.60.33"
    partner_country: str = "KR"
    as_of_date: str = "2025-09-15"
    cif_or_fob_value: Decimal = Decimal("100000")
    currency: str = "USD"
    has_api: bool = True
    api_type: str | None = "API-P"
    materials: tuple[MaterialInput, ...] = ()
    rvc_percent: Decimal | None = None
    wo_confirmed: bool | None = None
    process_kind: str | None = None
    process_satisfied: bool | None = None
    dv20_percent: Decimal | None = None
    highest_contributor_country: str | None = None
    transshipment_hub: str | None = None
    requires_back_to_back: bool = False
    third_party_invoice_country: str | None = None
    max_scenarios: int = 4


class CounterfactualOptimizerService:
    """Generates minimal counterfactual interventions and verifies every repair with the real HermiT + SWRL reasoner."""

    def __init__(self, optimizer: MultiFtaOptimizer, export_advisor: ExportComplianceAdvisor):
        self._opt = optimizer
        self._exp = export_advisor
        self.kb = optimizer.kb
        self._owl = optimizer._owl

    def _party_countries(self, agreement_slug: str) -> list[str]:
        agr_iri = TG[agreement_slug]
        return sorted({
            str(p).split("#")[-1]
            for p in self._owl.objects(agr_iri, TG.agreementParty)
            if isinstance(p, URIRef)
        })

    def optimize_counterfactuals(self, spec: CounterfactualSpec) -> dict[str, object]:
        direction = spec.direction.strip().lower()
        if direction not in {"import", "export"}:
            raise ValueError("direction must be 'import' or 'export'.")
        if direction == "export":
            return self._optimize_export(spec)
        return self._optimize_import(spec)

    def _optimize_import(self, spec: CounterfactualSpec) -> dict[str, object]:
        base_spec = ShipmentSpec(
            hs_code=spec.hs_code,
            dispatch_country=spec.partner_country,
            destination_country="ID",
            as_of_date=spec.as_of_date,
            cif_value=spec.cif_or_fob_value,
            currency=spec.currency,
            has_api=spec.has_api,
            api_type=spec.api_type,
            materials=spec.materials,
            rvc_percent=spec.rvc_percent,
            wo_confirmed=spec.wo_confirmed,
            process_kind=spec.process_kind,
            process_satisfied=spec.process_satisfied,
            dv20_percent=spec.dv20_percent,
            highest_contributor_country=spec.highest_contributor_country,
            include_plan=False,
            include_explain=False,
        )
        baseline = self._opt.optimize(base_spec)
        if baseline.recommended_agreement == "PROHIBITED":
            return {
                "direction": "import",
                "kb_version": self.kb.snapshot.version,
                "hs_code": baseline.hs_code,
                "route": f"{baseline.dispatch_country.upper()} -> ID",
                "as_of_date": spec.as_of_date,
                "blocked": True,
                "block_reason": "Barang berstatus DILARANG IMPOR — perbaikan BOM/RVC tidak dapat membuka izin atau tarif preferensi.",
                "baseline": self._summarize_import_baseline(baseline),
                "counterfactuals": [],
            }

        base_winner = next((s for s in baseline.schemes if s.agreement == baseline.recommended_agreement), None)
        base_tax_total = (
            base_winner.tax_breakdown.total_import_taxes
            if base_winner and base_winner.tax_breakdown
            else (baseline.mfn_tax_breakdown.total_import_taxes if baseline.mfn_tax_breakdown else Decimal("0"))
        )
        base_duty = (
            base_winner.applied_duty_percent
            if base_winner and base_winner.applied_duty_percent is not None
            else (baseline.mfn_duty_percent or Decimal("0"))
        )

        # Build bounded minimal candidate interventions targeting schemes with lower schedule tariff or DV20 unlock
        candidates: list[dict[str, object]] = []
        seen_sigs: set[str] = set()

        def enqueue(sig: str, item: dict[str, object]) -> None:
            if sig not in seen_sigs and len(candidates) < max(1, min(spec.max_scenarios, 6)):
                seen_sigs.add(sig)
                candidates.append(item)

        for s in sorted(
            baseline.schemes,
            key=lambda ev: ev.schedule_duty_percent if ev.schedule_duty_percent is not None else Decimal("999"),
        ):
            sched = s.schedule_duty_percent
            if s.excluded_to_mfn or sched is None:
                continue

            # Case A: Scheme is INELIGIBLE due to RoO, yet its schedule tariff beats current applied duty!
            if s.origin_status != "SATISFIED" and s.minimal_ops_status != "NOT_SATISFIED" and sched < base_duty:
                parties = self._party_countries(s.agreement)
                # Intervention 1: Regional Cumulation Switch on non-originating BOM materials
                if spec.materials and parties:
                    # Prefer dispatch partner or first partner country in agreement
                    preferred_partner = baseline.dispatch_country if baseline.dispatch_country in parties else parties[0]
                    patched_mats = tuple(
                        replace(m, origin_country=preferred_partner, origin_proof_verified=True)
                        if m.non_originating else m
                        for m in spec.materials
                    )
                    mat_list = ", ".join(m.hs_code for m in spec.materials if m.non_originating)
                    enqueue(
                        f"cumul:{s.agreement}:{preferred_partner}",
                        {
                            "intervention_id": f"cumulation-{s.agreement}",
                            "kind": "CUMULATION_SWITCH",
                            "target_agreement": s.agreement,
                            "target_agreement_label": s.agreement_label,
                            "title": f"Alihkan Pemasok Bahan ({mat_list}) ke Negara Mitra {s.agreement.upper()} ({preferred_partner.upper()})",
                            "change_summary": (
                                f"Ganti asal bahan baku non-originating ({mat_list}) ke negara mitra {s.agreement.upper()} "
                                f"({', '.join(p.upper() for p in parties[:5])}) dengan bukti asal terverifikasi (akumulasi regional)."
                            ),
                            "spec": replace(base_spec, materials=patched_mats),
                            "agreements": (s.agreement,),
                        },
                    )

                # Intervention 2: RVC Threshold Upgrade to 40%
                rule_up = (s.concise_rule or "").upper()
                if "RVC" in rule_up and (spec.rvc_percent is None or spec.rvc_percent < Decimal("40")):
                    cur_rvc = spec.rvc_percent or Decimal("0")
                    delta_rvc = _q2(Decimal("40.00") - cur_rvc)
                    enqueue(
                        f"rvc40:{s.agreement}",
                        {
                            "intervention_id": f"rvc-upgrade-{s.agreement}",
                            "kind": "RVC_THRESHOLD_UPGRADE",
                            "target_agreement": s.agreement,
                            "target_agreement_label": s.agreement_label,
                            "title": f"Tingkatkan RVC +{delta_rvc}% (dari {cur_rvc:.2f}% → 40.00%) untuk Membuka {s.agreement_label}",
                            "change_summary": (
                                f"Naikkan kandungan nilai regional (RVC) minimal +{delta_rvc}% menjadi 40.00% "
                                f"agar memenuhi syarat {s.concise_rule} pada {s.agreement_label}."
                            ),
                            "spec": replace(base_spec, rvc_percent=Decimal("40.00")),
                            "agreements": (s.agreement,),
                        },
                    )

            # Case B: RCEP DV20 Tariff Differential repair (DV20 < 20% or missing)
            if s.agreement == "rcep" and s.requires_rcep_dv20 and s.dv20_status != "SATISFIED" and s.origin_status == "SATISFIED":
                enqueue(
                    "dv20:rcep",
                    {
                        "intervention_id": "dv20-rcep-statement",
                        "kind": "DV20_COMPLIANCE",
                        "target_agreement": "rcep",
                        "target_agreement_label": s.agreement_label,
                        "title": "Lampirkan Pernyataan Nilai Tambah Domestik RCEP (DV20 ≥ 20% FOB)",
                        "change_summary": (
                            f"Sertakan bukti DV20 ≥ 20% FOB di {baseline.dispatch_country.upper()} (PMK 209/2022 Pasal 14) "
                            f"untuk mengamankan tarif jadwal RCEP ({sched}%) dan mencegah pengalihan tarif ke negara kontributor tertinggi."
                        ),
                        "spec": replace(base_spec, dv20_percent=Decimal("25.00")),
                        "agreements": ("rcep",),
                    },
                )

        verified_results: list[dict[str, object]] = []
        for cand in candidates:
            cf_res = self._opt.optimize(cand["spec"], agreements=cand["agreements"])
            cf_scheme = cf_res.schemes[0] if cf_res.schemes else None
            if cf_scheme is None:
                continue
            cf_passed = cf_scheme.origin_status == "SATISFIED" and cf_scheme.eligibility in {"ELIGIBLE", "ELIGIBLE_PENDING_DOCS"}
            cf_tax = cf_scheme.tax_breakdown.total_import_taxes if cf_scheme.tax_breakdown else base_tax_total
            cf_duty = cf_scheme.applied_duty_percent if cf_scheme.applied_duty_percent is not None else base_duty
            additional_savings = _q2(base_tax_total - cf_tax)
            total_vs_mfn = cf_scheme.total_tax_savings or Decimal("0.00")

            verified_results.append({
                "intervention_id": cand["intervention_id"],
                "kind": cand["kind"],
                "target_agreement": cand["target_agreement"],
                "target_agreement_label": cand["target_agreement_label"],
                "title": cand["title"],
                "change_summary": cand["change_summary"],
                "verified_by_reasoner": True,
                "unlock_succeeded": cf_passed,
                "new_origin_status": cf_scheme.origin_status,
                "new_eligibility": cf_scheme.eligibility,
                "new_dv20_status": cf_scheme.dv20_status,
                "baseline_duty_percent": f"{base_duty:.2f}",
                "new_duty_percent": f"{cf_duty:.2f}",
                "duty_reduction_points": f"{_q2(base_duty - cf_duty):.2f}",
                "baseline_total_taxes": f"{base_tax_total:.2f}",
                "new_total_taxes": f"{cf_tax:.2f}",
                "net_additional_savings": f"{additional_savings:.2f}",
                "total_savings_vs_mfn": f"{total_vs_mfn:.2f}",
                "currency": spec.currency,
                "facility_code": cf_scheme.facility_code,
                "concise_rule": cf_scheme.concise_rule,
                "patched_payload": {
                    "hs_code": cand["spec"].hs_code,
                    "dispatch_country": spec.partner_country,
                    "as_of_date": spec.as_of_date,
                    "cif_value": str(spec.cif_or_fob_value),
                    "rvc_percent": str(cand["spec"].rvc_percent) if cand["spec"].rvc_percent is not None else None,
                    "dv20_percent": str(cand["spec"].dv20_percent) if cand["spec"].dv20_percent is not None else None,
                    "materials": [
                        {
                            "hs_code": m.hs_code,
                            "non_originating": m.non_originating,
                            "origin_country": m.origin_country,
                            "origin_proof_verified": m.origin_proof_verified,
                        }
                        for m in cand["spec"].materials
                    ],
                },
            })

        verified_results.sort(
            key=lambda r: (
                0 if r["unlock_succeeded"] else 1,
                -Decimal(str(r["net_additional_savings"])),
                Decimal(str(r["new_duty_percent"])),
            )
        )

        return {
            "direction": "import",
            "kb_version": self.kb.snapshot.version,
            "hs_code": baseline.hs_code,
            "route": f"{baseline.dispatch_country.upper()} -> ID",
            "as_of_date": spec.as_of_date,
            "blocked": False,
            "block_reason": None,
            "baseline": self._summarize_import_baseline(baseline),
            "counterfactuals": verified_results,
        }

    def _summarize_import_baseline(self, baseline) -> dict[str, object]:
        winner = next((s for s in baseline.schemes if s.agreement == baseline.recommended_agreement), None)
        tax = winner.tax_breakdown if winner else baseline.mfn_tax_breakdown
        return {
            "recommended_agreement": baseline.recommended_agreement,
            "recommended_label": winner.agreement_label if winner else baseline.recommended_agreement,
            "applied_duty_percent": (
                f"{winner.applied_duty_percent:.2f}"
                if winner and winner.applied_duty_percent is not None
                else (f"{baseline.mfn_duty_percent:.2f}" if baseline.mfn_duty_percent is not None else None)
            ),
            "mfn_duty_percent": f"{baseline.mfn_duty_percent:.2f}" if baseline.mfn_duty_percent is not None else None,
            "total_import_taxes": f"{tax.total_import_taxes:.2f}" if tax else None,
            "total_savings_vs_mfn": f"{winner.total_tax_savings:.2f}" if winner and winner.total_tax_savings is not None else "0.00",
            "currency": tax.currency if tax else "USD",
            "lartas_status": baseline.lartas.status,
            "schemes_summary": [
                {
                    "agreement": s.agreement,
                    "agreement_label": s.agreement_label,
                    "schedule_duty_percent": f"{s.schedule_duty_percent:.2f}" if s.schedule_duty_percent is not None else None,
                    "origin_status": s.origin_status,
                    "eligibility": s.eligibility,
                    "concise_rule": s.concise_rule,
                }
                for s in baseline.schemes
            ],
        }

    def _optimize_export(self, spec: CounterfactualSpec) -> dict[str, object]:
        base_spec = ExportSpec(
            hs_code=spec.hs_code,
            origin_country="ID",
            destination_country=spec.partner_country,
            as_of_date=spec.as_of_date,
            fob_value=spec.cif_or_fob_value,
            currency=spec.currency,
            materials=spec.materials,
            rvc_percent=spec.rvc_percent,
            wo_confirmed=spec.wo_confirmed,
            process_kind=spec.process_kind,
            process_satisfied=spec.process_satisfied,
            transshipment_hub=spec.transshipment_hub,
            requires_back_to_back=spec.requires_back_to_back,
            third_party_invoice_country=spec.third_party_invoice_country,
            include_explain=False,
        )
        baseline = self._exp.evaluate(base_spec)
        if baseline.recommended_agreement == "PROHIBITED_EXPORT":
            return {
                "direction": "export",
                "kb_version": self.kb.snapshot.version,
                "hs_code": baseline.hs_code,
                "route": f"ID -> {baseline.destination_country.upper()}",
                "as_of_date": spec.as_of_date,
                "blocked": True,
                "block_reason": "Barang berstatus DILARANG EKSPOR (Permendag 8/2025) — simulasi perbaikan SKA tidak dapat dilakukan.",
                "baseline": self._summarize_export_baseline(baseline),
                "counterfactuals": [],
            }

        candidates: list[dict[str, object]] = []
        seen_sigs: set[str] = set()

        def enqueue(sig: str, item: dict[str, object]) -> None:
            if sig not in seen_sigs and len(candidates) < max(1, min(spec.max_scenarios, 5)):
                seen_sigs.add(sig)
                candidates.append(item)

        for s in baseline.schemes:
            if s.origin_status != "SATISFIED":
                parties = self._party_countries(s.agreement)
                if spec.materials and parties:
                    preferred = baseline.destination_country if baseline.destination_country in parties else parties[0]
                    patched_mats = tuple(
                        replace(m, origin_country=preferred, origin_proof_verified=True)
                        if m.non_originating else m
                        for m in spec.materials
                    )
                    mat_list = ", ".join(m.hs_code for m in spec.materials if m.non_originating)
                    enqueue(
                        f"exp-cumul:{s.agreement}",
                        {
                            "intervention_id": f"export-cumulation-{s.agreement}",
                            "kind": "CUMULATION_SWITCH",
                            "target_agreement": s.agreement,
                            "target_agreement_label": s.agreement_label,
                            "title": f"Alihkan Bahan ({mat_list}) ke Mitra {s.agreement.upper()} ({preferred.upper()}) untuk Membuka SKA {s.origin_proof_label or s.agreement.upper()}",
                            "change_summary": (
                                f"Ganti sumber bahan {mat_list} ke negara anggota {s.agreement.upper()} ({', '.join(p.upper() for p in parties[:5])}) "
                                f"dengan bukti asal terverifikasi agar pembeli dapat mengklaim tarif preferensi {s.agreement_label}."
                            ),
                            "spec": replace(base_spec, materials=patched_mats),
                        },
                    )
                if "RVC" in (s.concise_rule or "").upper() and (spec.rvc_percent is None or spec.rvc_percent < Decimal("40")):
                    cur_rvc = spec.rvc_percent or Decimal("0")
                    delta_rvc = _q2(Decimal("40.00") - cur_rvc)
                    enqueue(
                        f"exp-rvc40:{s.agreement}",
                        {
                            "intervention_id": f"export-rvc-upgrade-{s.agreement}",
                            "kind": "RVC_THRESHOLD_UPGRADE",
                            "target_agreement": s.agreement,
                            "target_agreement_label": s.agreement_label,
                            "title": f"Tingkatkan RVC +{delta_rvc}% (→ 40.00%) untuk Membuka SKA {s.agreement_label}",
                            "change_summary": f"Peningkatan RVC menjadi 40.00% memenuhi syarat {s.concise_rule} untuk penerbitan {s.origin_proof_label or 'SKA'}.",
                            "spec": replace(base_spec, rvc_percent=Decimal("40.00")),
                        },
                    )
            if s.back_to_back_status == "DISALLOWED_BILATERAL" and spec.requires_back_to_back:
                enqueue(
                    f"exp-direct:{s.agreement}",
                    {
                        "intervention_id": f"export-direct-route-{s.agreement}",
                        "kind": "DIRECT_CONSIGNMENT_SWITCH",
                        "target_agreement": s.agreement,
                        "target_agreement_label": s.agreement_label,
                        "title": f"Ubah ke Pengiriman Langsung (Tanpa Back-to-Back Split) untuk Membuka {s.agreement_label}",
                        "change_summary": (
                            f"Menghilangkan pemecahan Back-to-Back di hub {spec.transshipment_hub or 'transit'} membuka kembali "
                            f"kelayakan skema bilateral {s.agreement_label} ({s.origin_proof_label or 'SKA'})."
                        ),
                        "spec": replace(base_spec, transshipment_hub=None, requires_back_to_back=False),
                    },
                )

        verified_results: list[dict[str, object]] = []
        for cand in candidates:
            cf_res = self._exp.evaluate(cand["spec"])
            target = next((x for x in cf_res.schemes if x.agreement == cand["target_agreement"]), None)
            if target is None:
                continue
            unlocked = target.eligibility == "ELIGIBLE"
            verified_results.append({
                "intervention_id": cand["intervention_id"],
                "kind": cand["kind"],
                "target_agreement": cand["target_agreement"],
                "target_agreement_label": cand["target_agreement_label"],
                "title": cand["title"],
                "change_summary": cand["change_summary"],
                "verified_by_reasoner": True,
                "unlock_succeeded": unlocked,
                "new_origin_status": target.origin_status,
                "new_eligibility": target.eligibility,
                "new_recommended_agreement": cf_res.recommended_agreement,
                "origin_proof_doc": target.origin_proof_doc,
                "origin_proof_label": target.origin_proof_label,
                "concise_rule": target.concise_rule,
                "patched_payload": {
                    "hs_code": cand["spec"].hs_code,
                    "destination_country": spec.partner_country,
                    "as_of_date": spec.as_of_date,
                    "fob_value": str(spec.cif_or_fob_value),
                    "rvc_percent": str(cand["spec"].rvc_percent) if cand["spec"].rvc_percent is not None else None,
                    "transshipment_hub": cand["spec"].transshipment_hub,
                    "requires_back_to_back": cand["spec"].requires_back_to_back,
                    "materials": [
                        {
                            "hs_code": m.hs_code,
                            "non_originating": m.non_originating,
                            "origin_country": m.origin_country,
                            "origin_proof_verified": m.origin_proof_verified,
                        }
                        for m in cand["spec"].materials
                    ],
                },
            })

        verified_results.sort(key=lambda r: (0 if r["unlock_succeeded"] else 1, r["target_agreement"]))
        return {
            "direction": "export",
            "kb_version": self.kb.snapshot.version,
            "hs_code": baseline.hs_code,
            "route": f"ID -> {baseline.destination_country.upper()}",
            "as_of_date": spec.as_of_date,
            "blocked": False,
            "block_reason": None,
            "baseline": self._summarize_export_baseline(baseline),
            "counterfactuals": verified_results,
        }

    def _summarize_export_baseline(self, baseline) -> dict[str, object]:
        winner = next((s for s in baseline.schemes if s.agreement == baseline.recommended_agreement), None)
        return {
            "recommended_agreement": baseline.recommended_agreement,
            "recommended_label": winner.agreement_label if winner else baseline.recommended_agreement,
            "origin_proof_label": winner.origin_proof_label if winner else None,
            "export_lartas_status": baseline.export_lartas.status,
            "export_duty_status": baseline.export_duty.status,
            "schemes_summary": [
                {
                    "agreement": s.agreement,
                    "agreement_label": s.agreement_label,
                    "origin_status": s.origin_status,
                    "back_to_back_status": s.back_to_back_status,
                    "eligibility": s.eligibility,
                    "concise_rule": s.concise_rule,
                }
                for s in baseline.schemes
            ],
        }
