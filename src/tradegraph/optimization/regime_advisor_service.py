"""Declarative KB-driven Multi-Regime Customs Configuration Advisor (PIB+FTA vs Temporary Import vs KITE vs Kawasan Berikat vs PEB)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tradegraph.kb.loader import TG
from tradegraph.kb.queries import prepare_local_query
from tradegraph.optimization.export_service import ExportComplianceAdvisor, ExportSpec
from tradegraph.optimization.service import MultiFtaOptimizer, ShipmentSpec


def _q2(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class CustomsRegimeSpecData:
    code: str
    title: str
    subtitle: str
    direction: str
    declaration_form: str
    duty_benefit: str
    vat_benefit: str
    lartas_exemption: str
    target_uses: tuple[str, ...]
    reexport_requirement: str
    required_licenses: tuple[str, ...]
    key_obligations: str
    legal_basis_summary: str
    target_task_mode: str
    duty_relief_percent: Decimal
    vat_relief_percent: Decimal
    requires_physical_intact: bool
    source_clauses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegimeAdvisorSpec:
    hs_code: str = "7209.26.10"
    as_of_date: str = "2025-09-15"
    direction: str = "import"  # import | export
    trade_purpose: str = "manufacturing_for_export"  # local_consumption | commercial_trading | manufacturing_local_sale | exhibition_event | temporary_project_testing | manufacturing_for_export | industrial_bonded_zone | outbound_export
    reexport_plan: str = "PROCESSED_EXPORT"  # NO_REEXPORT | SAME_CONDITION_REEXPORT | PROCESSED_EXPORT | OUTBOUND_EXPORT
    company_license: str = "API-P"  # API-P | API-U | KITE_HOLDER | KB_OPERATOR | NONE
    identifiable_for_reexport: bool = True
    consumed_in_use: bool = False
    essential_change_in_use: bool = False
    customs_value_usd: Decimal = Decimal("100000")
    has_fta_partner: bool = True


class CustomsRegimeAdvisor:
    """Constraint-based configurator evaluating 5 declarative KB Indonesian customs regimes against operational & legal constraints."""

    def __init__(self, optimizer: MultiFtaOptimizer, export_advisor: ExportComplianceAdvisor):
        self._opt = optimizer
        self._exp = export_advisor
        self.kb = optimizer.kb
        self._owl = optimizer._owl
        self._regimes_cache: tuple[CustomsRegimeSpecData, ...] | None = None

    def list_regimes(self) -> tuple[CustomsRegimeSpecData, ...]:
        if self._regimes_cache is not None:
            return self._regimes_cache
        qdef = self.kb.query_catalog.get("customs-regime-specs")
        if qdef is None:
            self._regimes_cache = ()
            return ()
        query = prepare_local_query(qdef.sparql, "SelectQuery")
        grouped: dict[str, dict[str, object]] = {}
        for row in self._owl.query(query):
            d = row.asdict()
            code = str(d["code"])
            entry = grouped.setdefault(code, {
                "title": str(d["title"]),
                "subtitle": str(d["subtitle"]),
                "direction": str(d["direction"]),
                "form": str(d["form"]),
                "duty_benefit": str(d["dutyBenefit"]),
                "vat_benefit": str(d["vatBenefit"]),
                "lartas_exemption": str(d["lartasExemption"]),
                "target_uses": set(),
                "reexport_req": str(d["reexportReq"]),
                "req_licenses": set(),
                "obligations": str(d["obligations"]),
                "legal_basis": str(d["legalBasis"]),
                "target_task_mode": str(d["targetTaskMode"]),
                "duty_relief": Decimal(str(d["dutyRelief"])),
                "vat_relief": Decimal(str(d["vatRelief"])),
                "physical_intact": str(d["physicalIntact"]).lower() == "true",
                "clauses": set(),
            })
            entry["target_uses"].add(str(d["targetUse"]))
            entry["req_licenses"].add(str(d["reqLicense"]))
            if d.get("clause"):
                entry["clauses"].add(str(d["clause"]).split("#")[-1])

        out: list[CustomsRegimeSpecData] = []
        for code in sorted(grouped):
            g = grouped[code]
            out.append(CustomsRegimeSpecData(
                code=code,
                title=str(g["title"]),
                subtitle=str(g["subtitle"]),
                direction=str(g["direction"]),
                declaration_form=str(g["form"]),
                duty_benefit=str(g["duty_benefit"]),
                vat_benefit=str(g["vat_benefit"]),
                lartas_exemption=str(g["lartas_exemption"]),
                target_uses=tuple(sorted(g["target_uses"])),
                reexport_requirement=str(g["reexport_req"]),
                required_licenses=tuple(sorted(g["req_licenses"])),
                key_obligations=str(g["obligations"]),
                legal_basis_summary=str(g["legal_basis"]),
                target_task_mode=str(g["target_task_mode"]),
                duty_relief_percent=g["duty_relief"],
                vat_relief_percent=g["vat_relief"],
                requires_physical_intact=bool(g["physical_intact"]),
                source_clauses=tuple(sorted(g["clauses"])),
            ))
        self._regimes_cache = tuple(out)
        return self._regimes_cache

    def recommend(self, spec: RegimeAdvisorSpec) -> dict[str, object]:
        hs_iri = self._opt.resolve_hs_iri(spec.hs_code)
        hs_code_val = str(self._owl.value(hs_iri, TG.codeValue) or spec.hs_code)
        norm_dir = spec.direction.strip().upper()
        if norm_dir not in {"IMPORT", "EXPORT"}:
            raise ValueError("direction must be 'import' or 'export'.")

        # Live Lartas / Prohibition check
        if norm_dir == "IMPORT":
            lartas_imp = self._opt._assess_lartas(
                hs_iri,
                TG.jurisdictionIndonesiaCustoms,
                ShipmentSpec(
                    hs_code=hs_code_val,
                    dispatch_country="TH",
                    as_of_date=spec.as_of_date,
                    api_type=spec.company_license if spec.company_license in {"API-P", "API-U"} else None,
                    include_explain=True,
                ),
            )
            prohibited = lartas_imp.prohibited
            lartas_status = lartas_imp.status
            lartas_diags = list(lartas_imp.diagnostics)
            lartas_cits = [c.to_dict() for c in lartas_imp.citations]
        else:
            exp_spec = ExportSpec(hs_code=hs_code_val, destination_country="JP", as_of_date=spec.as_of_date, include_explain=True)
            lartas_exp = self._exp.assess_export_lartas(hs_iri, exp_spec)
            prohibited = lartas_exp.prohibited
            lartas_status = lartas_exp.status
            lartas_diags = list(lartas_exp.diagnostics)
            lartas_cits = [c.to_dict() for c in lartas_exp.citations]

        # Baseline MFN tax benchmark for fiscal comparison on import
        mfn_raw = self._owl.value(hs_iri, TG.mfnDutyPercent)
        mfn_duty = Decimal(str(mfn_raw)) if mfn_raw is not None else Decimal("5.00")
        tax_res = self._opt._query_tax_profile(TG.jurisdictionIndonesiaCustoms)
        has_api = spec.company_license != "NONE"
        mfn_tax = (
            self._opt._compute_tax(spec.customs_value_usd, "USD", mfn_duty, tax_res[0], has_api)
            if norm_dir == "IMPORT" and tax_res is not None else None
        )
        mfn_total = mfn_tax.total_import_taxes if mfn_tax is not None else Decimal("0.00")

        regimes = self.list_regimes()
        evaluated: list[dict[str, object]] = []

        for r in regimes:
            satisfied: list[str] = []
            violated: list[str] = []
            warnings: list[str] = []
            score = 0

            if prohibited:
                violated.append(f"Barang berstatus {lartas_status} (dilarang mutlak) sehingga seluruh fasilitas diblokir.")
            elif r.direction != norm_dir:
                violated.append(f"Arah transaksi rezim adalah {r.direction}, tidak sesuai dengan transaksi {norm_dir}.")
            else:
                satisfied.append(f"Arah transaksi sesuai ({r.direction}).")
                score += 20

                # Constraint 1: Trade Purpose
                if spec.trade_purpose in r.target_uses:
                    satisfied.append(f"Tujuan penggunaan ({spec.trade_purpose}) cocok dengan cakupan rezim.")
                    score += 35
                else:
                    violated.append(f"Tujuan penggunaan ({spec.trade_purpose}) di luar tujuan utama rezim ({', '.join(r.target_uses)}).")

                # Constraint 2: Re-export requirement
                if spec.reexport_plan == r.reexport_requirement:
                    satisfied.append(f"Rencana penyelesaian/ekspor kembali ({spec.reexport_plan}) memenuhi syarat rezim.")
                    score += 30
                elif r.code == "general-fta-import" and spec.reexport_plan in {"PROCESSED_EXPORT", "NO_REEXPORT"}:
                    satisfied.append("Impor Umum PIB tetap dapat digunakan sebagai alternatif berbayar (tanpa pembebasan KITE/KB).")
                    score += 10
                else:
                    violated.append(f"Rezim mensyaratkan skema penyelesaian {r.reexport_requirement} (rencana Anda: {spec.reexport_plan}).")

                # Constraint 3: Physical intactness (Temporary Import PMK 178/2017 Pasal 2)
                if r.requires_physical_intact:
                    if spec.identifiable_for_reexport and not spec.consumed_in_use and not spec.essential_change_in_use:
                        satisfied.append("Syarat fisik barang terpenuhi (dapat diidentifikasi, tidak habis pakai, tidak berubah hakiki).")
                        score += 20
                    else:
                        violated.append("Gagal syarat fisik Impor Sementara (PMK 178/2017 Pasal 2): barang harus dapat diidentifikasi, tidak habis pakai, dan tidak berubah bentuk hakiki.")

                # Constraint 4: License / Facility status
                if spec.company_license in r.required_licenses:
                    satisfied.append(f"Status perizinan perusahaan ({spec.company_license}) memenuhi syarat rezim.")
                    score += 15
                elif r.code in {"kite-pembebasan", "kawasan-berikat"} and spec.company_license == "API-P":
                    satisfied.append("API-P memenuhi prasyarat dasar industri; perlu penetapan fasilitas KITE/PKB dari DJBC.")
                    warnings.append(f"Perlu mengajukan izin/penetapan khusus ({', '.join(r.required_licenses)}) sebelum menggunakan fasilitas ini.")
                    score += 10
                else:
                    warnings.append(f"Status izin saat ini ({spec.company_license}) belum termasuk izin standar rezim ({', '.join(r.required_licenses)}).")

                # Bonus for Lartas relief on Temporary Import / Kawasan Berikat when goods are Lartas-restricted
                if norm_dir == "IMPORT" and lartas_status in {"BORDER_BLOCKED", "POST_BORDER_PENDING"} and r.code in {"temporary-import", "kawasan-berikat"}:
                    satisfied.append("Memberikan keringanan/penundaan beban izin Lartas impor umum sesuai regulasi fasilitas.")
                    score += 10

            # Fiscal savings estimate
            if norm_dir == "IMPORT" and mfn_tax is not None:
                if r.duty_relief_percent >= Decimal("100") and r.vat_relief_percent >= Decimal("100"):
                    est_tax = Decimal("0.00")
                    est_savings = mfn_total
                elif r.code == "general-fta-import" and spec.has_fta_partner:
                    # Estimate preferential FTA at 0% BM (best-case ceiling shown clearly as estimate)
                    fta0 = self._opt._compute_tax(spec.customs_value_usd, "USD", Decimal("0.00"), tax_res[0], has_api)
                    est_tax = fta0.total_import_taxes
                    est_savings = _q2(mfn_total - est_tax)
                else:
                    est_tax = mfn_total
                    est_savings = Decimal("0.00")
            else:
                est_tax = Decimal("0.00")
                est_savings = Decimal("0.00")

            if prohibited or violated:
                suitability = "INELIGIBLE"
            elif warnings:
                suitability = "CONDITIONAL"
            else:
                suitability = "ELIGIBLE"

            cits = [c.to_dict() for c in self._opt.resolve_citations(r.source_clauses)]
            evaluated.append({
                "regime_code": r.code,
                "title": r.title,
                "subtitle": r.subtitle,
                "direction": r.direction,
                "declaration_form": r.declaration_form,
                "suitability": suitability,
                "score": score if suitability != "INELIGIBLE" else 0,
                "duty_benefit": r.duty_benefit,
                "vat_benefit": r.vat_benefit,
                "lartas_exemption": r.lartas_exemption,
                "key_obligations": r.key_obligations,
                "legal_basis_summary": r.legal_basis_summary,
                "target_task_mode": r.target_task_mode,
                "estimated_baseline_mfn_taxes_usd": f"{mfn_total:.2f}",
                "estimated_regime_taxes_usd": f"{est_tax:.2f}",
                "estimated_fiscal_relief_usd": f"{est_savings:.2f}",
                "satisfied_constraints": satisfied,
                "violated_constraints": violated,
                "warnings": warnings,
                "citations": cits,
            })

        evaluated.sort(
            key=lambda x: (
                0 if x["suitability"] == "ELIGIBLE" else (1 if x["suitability"] == "CONDITIONAL" else 2),
                -int(x["score"]),
                -Decimal(str(x["estimated_fiscal_relief_usd"])),
                str(x["regime_code"]),
            )
        )

        recommended_code = None
        if not prohibited and evaluated and evaluated[0]["suitability"] in {"ELIGIBLE", "CONDITIONAL"}:
            evaluated[0]["suitability"] = "RECOMMENDED"
            recommended_code = str(evaluated[0]["regime_code"])

        return {
            "kb_version": self.kb.snapshot.version,
            "hs_code": hs_code_val,
            "direction": spec.direction.lower(),
            "as_of_date": spec.as_of_date,
            "prohibited": prohibited,
            "lartas_status": lartas_status,
            "lartas_diagnostics": lartas_diags,
            "lartas_citations": lartas_cits,
            "recommended_regime": recommended_code or ("PROHIBITED" if prohibited else "NONE"),
            "regimes": evaluated,
        }
