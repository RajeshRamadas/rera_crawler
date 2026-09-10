"""
Karnataka Property Legal Due-Diligence Agent
==============================================
AI Property Legal Agent for Karnataka real estate verification.
Investigates, cross-checks, discovers missing evidence, identifies contradictions,
and produces an evidence-backed legal risk assessment report based on the 27-check matrix.
"""

from typing import Dict, Any, List
import json
from legal_risk_engine import (
    KarnatakaLegalRiskEngine,
    LegalAssessmentResult,
    CheckStatus,
    RiskSeverity,
    BuyGateDecision,
    BUY_GATE_CHECK_AREAS
)


class KarnatakaPropertyLegalAgent:
    """
    AI Legal Due-Diligence Agent for Karnataka Properties.
    Examines multi-document inputs (Title Deed, Kaveri 2 EC, Bhoomi RTC, Survey Tippani,
    BDA/BMRDA Master Plan, Rajakaluve Buffer, e-Khata, RERA) to generate legal reports.
    """

    def __init__(self):
        self.engine = KarnatakaLegalRiskEngine()

    def run_due_diligence(self, property_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Runs complete 27-check legal risk assessment and returns structured report dictionary.
        """
        assessment: LegalAssessmentResult = self.engine.evaluate(property_data)
        markdown_report = self.generate_markdown_report(assessment)

        # Convert assessment dataclass to dict
        checks_dict = {}
        for area, check in assessment.checks.items():
            checks_dict[area] = {
                "area": check.area,
                "status": check.status.value,
                "badge": "🟢" if check.status == CheckStatus.PASS else (
                    "🟡" if check.status == CheckStatus.WARNING else (
                        "🔴" if check.status == CheckStatus.FAIL else "⚪"
                    )
                ),
                "severity": check.severity.value,
                "is_mandatory": check.is_mandatory,
                "summary": check.summary,
                "evidence_found": check.evidence_found,
                "missing_evidence": check.missing_evidence,
                "contradictions": check.contradictions,
                "legal_rule": check.legal_rule,
                "mitigation_step": check.mitigation_step
            }

        return {
            "property_name": assessment.property_name,
            "property_type": assessment.property_type,
            "location": assessment.location,
            "survey_no": assessment.survey_no,
            "seller_claimed": assessment.owner_claimed,
            "overall_legal_score": assessment.overall_score,
            "buy_gate_decision": assessment.buy_gate_decision.value,
            "buy_gate_badge": "🟢 APPROVED TO BUY" if assessment.buy_gate_decision == BuyGateDecision.APPROVED_TO_BUY else (
                "🟡 BUY WITH CAUTION" if assessment.buy_gate_decision == BuyGateDecision.BUY_WITH_CAUTION else "🔴 DO NOT BUY"
            ),
            "mandatory_gate_met": assessment.mandatory_gate_met,
            "critical_failures": assessment.critical_failures,
            "warning_count": assessment.warning_count,
            "missing_evidence": assessment.missing_evidence_summary,
            "contradiction_log": assessment.contradiction_summary,
            "matrix_checks": checks_dict,
            "markdown_report": markdown_report
        }

    def generate_markdown_report(self, assessment: LegalAssessmentResult) -> str:
        """Generates comprehensive Github-flavored Markdown legal due-diligence report."""
        gate_badge = "🟢 APPROVED TO BUY" if assessment.buy_gate_decision == BuyGateDecision.APPROVED_TO_BUY else (
            "🟡 BUY WITH CAUTION" if assessment.buy_gate_decision == BuyGateDecision.BUY_WITH_CAUTION else "🔴 DO NOT BUY / HIGH LEGAL RISK"
        )

        lines = [
            f"# AI Property Legal Due-Diligence Report (Karnataka)",
            f"**Property**: {assessment.property_name} | **Type**: {assessment.property_type} | **Survey No**: {assessment.survey_no}",
            f"**Location**: {assessment.location} | **Seller Claimed**: {assessment.owner_claimed}",
            "",
            "---",
            "",
            "## 🛡️ Final BUY Gate Verdict",
            "",
            f"### **Legal Risk Score**: `{assessment.overall_score} / 100`",
            f"### **BUY Gate Decision**: `{gate_badge}`",
            ""
        ]

        if assessment.critical_failures:
            lines.extend([
                "> [!CAUTION]",
                "> **CRITICAL LEGAL DEFECTS DETECTED (DO NOT PURCHASE)**:",
                * [f"> - {cf}" for cf in assessment.critical_failures],
                ""
            ])

        if assessment.contradiction_summary:
            lines.extend([
                "> [!WARNING]",
                "> **CONTRADICTIONS & DISCREPANCIES ACROSS DOCUMENTS**:",
                * [f"> - {c}" for c in assessment.contradiction_summary],
                ""
            ])

        if assessment.missing_evidence_summary:
            lines.extend([
                "> [!NOTE]",
                "> **MISSING LEGAL EVIDENCE / UNVERIFIED DOCUMENTS**:",
                * [f"> - {m}" for m in assessment.missing_evidence_summary],
                ""
            ])

        lines.extend([
            "---",
            "",
            "## 📋 27-Check Mandatory BUY Gate Matrix",
            "",
            "| # | Verification Area | Status | Risk Severity | Mandatory | Summary & Key Legal Findings |",
            "|---|---|:---:|:---:|:---:|---|",
        ])

        idx = 1
        for area in BUY_GATE_CHECK_AREAS:
            c = assessment.checks.get(area)
            if not c:
                continue

            status_icon = "🟢 PASS" if c.status == CheckStatus.PASS else (
                "🟡 WARN" if c.status == CheckStatus.WARNING else (
                    "🔴 FAIL" if c.status == CheckStatus.FAIL else "⚪ N/A"
                )
            )
            mand_str = "Yes" if c.is_mandatory else "No"
            lines.append(
                f"| {idx} | **{c.area}** | {status_icon} | `{c.severity.value}` | {mand_str} | {c.summary} |"
            )
            idx += 1

        lines.extend([
            "",
            "---",
            "",
            "## ⚖️ Deep Dive & Legal Rules Verification",
            ""
        ])

        for area, c in assessment.checks.items():
            if c.status == CheckStatus.NOT_APPLICABLE:
                continue

            icon = "🟢" if c.status == CheckStatus.PASS else ("🟡" if c.status == CheckStatus.WARNING else "🔴")
            lines.append(f"### {icon} {c.area}")
            lines.append(f"- **Status**: `{c.status.value}` | **Severity**: `{c.severity.value}`")
            lines.append(f"- **Legal Act / Provision**: _{c.legal_rule}_")
            lines.append(f"- **Findings**: {c.summary}")

            if c.evidence_found:
                lines.append(f"- **Verified Evidence**: {', '.join(c.evidence_found)}")
            if c.missing_evidence:
                lines.append(f"- ⚠️ **Missing Evidence**: {', '.join(c.missing_evidence)}")
            if c.contradictions:
                lines.append(f"- 🔴 **Contradiction Alert**: {', '.join(c.contradictions)}")
            if c.mitigation_step:
                lines.append(f"- 💡 **Actionable Legal Mitigation**: {c.mitigation_step}")

            lines.append("")

        lines.extend([
            "---",
            "",
            "## 📜 Final Rule & Legal Due-Diligence Mandate",
            "",
            "> Never use a single document as proof that the property is safe.",
            "> **The strongest legal verification is built on 9 pillars**:",
            "> `Title + Revenue + Registration + Planning + Survey + Litigation + Government restrictions + Physical/GIS + Building/RERA`",
            ""
        ])

        return "\n".join(lines)


# Pre-configured sample property assessment scenarios for testing & web API
SAMPLE_PROPERTIES = {
    "sarjapur_plot": {
        "property_name": "Prestige City Villa Plot - Sy No 45/2",
        "property_type": "LAND_PLOT",
        "location": "Sarjapur Road, Bengaluru Urban",
        "survey_no": "45/2",
        "seller_name": "Ramesh Kumar & Sons",
        "claimed_extent_acres": 0.25,
        "seller_identity": {
            "id_proof_matched": True,
            "name_in_title_deed": True,
            "name_in_rtc": True,
            "name_in_khata": True,
            "is_poa_sale": False
        },
        "current_title": {
            "deed_registered": True,
            "kaveri2_matched": True,
            "original_deed_inspected": True
        },
        "title_chain_30yr": {
            "years_covered": 32,
            "unbroken_chain": True,
            "gap_years": []
        },
        "parent_documents": {
            "grant_certificate_available": True,
            "partition_deed_available": True,
            "saguvali_chit_verified": True
        },
        "ec_details": {
            "years_searched": 30,
            "form_type": "Form 16",
            "encumbrances": []
        },
        "rtc_details": {
            "bhoomi_verified": True,
            "extent_acres": 0.25,
            "nature_of_land": "Dry"
        },
        "mutation_details": {
            "mr_extract_present": True,
            "mr_status": "Approved"
        },
        "survey_details": {
            "tippani_present": True,
            "akarband_present": True,
            "sketch_11e_present": True,
            "podi_completed": True
        },
        "conversion_details": {
            "is_converted": True,
            "dc_conversion_order_present": True,
            "conversion_fine_paid": True
        },
        "ptcl_details": {
            "is_sc_st_grant_land": False,
            "govt_permission_obtained": False
        },
        "kharab_details": {
            "b_kharab_acres": 0.0,
            "a_kharab_acres": 0.0
        },
        "acquisition_details": {
            "bda_notified": False,
            "kiadb_notified": False,
            "nhai_notified": False
        },
        "environmental_details": {
            "lake_distance_meters": 350,
            "rajakaluve_distance_meters": 120,
            "rajakaluve_type": "Primary"
        },
        "master_plan_details": {
            "zone": "Residential (Main)"
        },
        "layout_details": {
            "approved_by_authority": True,
            "authority_name": "BDA",
            "nopa_released": True
        },
        "khata_details": {
            "khata_type": "BBMP e-Khata",
            "pid_number_valid": True
        },
        "tax_details": {
            "tax_paid_current_year": True,
            "sas_receipts_available": True
        },
        "litigation_details": {
            "pending_suits": [],
            "court_injunction_active": False
        },
        "physical_survey_details": {
            "total_station_survey_done": True,
            "boundary_matches_deed": True,
            "encroached_acres": 0.0
        },
        "gis_details": {
            "ht_line_clearance": True,
            "road_widening_impact": False
        },
        "mortgage_details": {
            "bank_mortgage_active": False,
            "bank_noc_obtained": True
        },
        "sale_agreement_details": {
            "terms_fair": True,
            "unilateral_forfeiture_clause": False,
            "indemnity_clause_included": True
        },
        "lawyer_opinion_details": {
            "opinion_obtained": True,
            "lawyer_name": "Adv. S. Sundaresan (Karnataka High Court)",
            "title_clear": True
        }
    },

    "whitefield_apartment": {
        "property_name": "Sobha Rosewood - 3BHK Unit 402",
        "property_type": "APARTMENT",
        "location": "ITPL Main Road, Whitefield, Bengaluru",
        "survey_no": "112/4",
        "seller_name": "Sobha Limited / Devadas Shetty",
        "claimed_extent_acres": 4.5,
        "seller_identity": {
            "id_proof_matched": True,
            "name_in_title_deed": True,
            "name_in_rtc": True,
            "name_in_khata": True,
            "is_poa_sale": False
        },
        "current_title": {
            "deed_registered": True,
            "kaveri2_matched": True,
            "original_deed_inspected": True
        },
        "title_chain_30yr": {
            "years_covered": 30,
            "unbroken_chain": True,
            "gap_years": []
        },
        "parent_documents": {
            "grant_certificate_available": True,
            "partition_deed_available": True,
            "saguvali_chit_verified": True
        },
        "ec_details": {
            "years_searched": 30,
            "form_type": "Form 16",
            "encumbrances": []
        },
        "rtc_details": {
            "bhoomi_verified": True,
            "extent_acres": 4.5,
            "nature_of_land": "Converted Residential"
        },
        "mutation_details": {
            "mr_extract_present": True,
            "mr_status": "Approved"
        },
        "survey_details": {
            "tippani_present": True,
            "akarband_present": True,
            "sketch_11e_present": True,
            "podi_completed": True
        },
        "conversion_details": {
            "is_converted": True,
            "dc_conversion_order_present": True,
            "conversion_fine_paid": True
        },
        "ptcl_details": {
            "is_sc_st_grant_land": False,
            "govt_permission_obtained": False
        },
        "kharab_details": {
            "b_kharab_acres": 0.0,
            "a_kharab_acres": 0.0
        },
        "acquisition_details": {
            "bda_notified": False,
            "kiadb_notified": False,
            "nhai_notified": False
        },
        "environmental_details": {
            "lake_distance_meters": 450,
            "rajakaluve_distance_meters": 85,
            "rajakaluve_type": "Primary"
        },
        "master_plan_details": {
            "zone": "Residential (Mixed)"
        },
        "layout_details": {
            "approved_by_authority": True,
            "authority_name": "BDA",
            "nopa_released": True
        },
        "khata_details": {
            "khata_type": "BBMP e-Khata",
            "pid_number_valid": True
        },
        "tax_details": {
            "tax_paid_current_year": True,
            "sas_receipts_available": True
        },
        "litigation_details": {
            "pending_suits": [],
            "court_injunction_active": False
        },
        "physical_survey_details": {
            "total_station_survey_done": True,
            "boundary_matches_deed": True,
            "encroached_acres": 0.0
        },
        "gis_details": {
            "ht_line_clearance": True,
            "road_widening_impact": False
        },
        "rera_details": {
            "rera_registered": True,
            "rera_valid_upto_date": True,
            "complaints_pending": False
        },
        "building_approval_details": {
            "plan_sanctioned": True,
            "far_compliant": True,
            "fire_noc_obtained": True
        },
        "oc_cc_details": {
            "oc_issued": True,
            "cc_issued": True
        },
        "uds_details": {
            "uds_matches_deed": True,
            "uds_sqft": 520,
            "builtup_sqft": 1750
        },
        "mortgage_details": {
            "bank_mortgage_active": True,
            "bank_noc_obtained": True,
            "mortgagee_bank": "HDFC Bank Ltd"
        },
        "sale_agreement_details": {
            "terms_fair": True,
            "unilateral_forfeiture_clause": False,
            "indemnity_clause_included": True
        },
        "lawyer_opinion_details": {
            "opinion_obtained": True,
            "lawyer_name": "Adv. Manjunath Reddy",
            "title_clear": True
        }
    },

    "kengeri_illegal_plot": {
        "property_name": "Sri Sai Enclave - Plot No 24 (High Legal Risk)",
        "property_type": "LAND_PLOT",
        "location": "Kengeri Hobli, Bengaluru South",
        "survey_no": "88/1B",
        "seller_name": "Venkatesh Gowda (GPA Holder)",
        "claimed_extent_acres": 1.5,
        "seller_identity": {
            "id_proof_matched": True,
            "name_in_title_deed": False,
            "name_in_rtc": False,
            "name_in_khata": False,
            "is_poa_sale": True,
            "poa_registered": False
        },
        "current_title": {
            "deed_registered": True,
            "kaveri2_matched": False,
            "original_deed_inspected": False
        },
        "title_chain_30yr": {
            "years_covered": 12,
            "unbroken_chain": False,
            "gap_years": ["1998-2006"]
        },
        "parent_documents": {
            "grant_certificate_available": False,
            "partition_deed_available": False,
            "saguvali_chit_verified": False
        },
        "ec_details": {
            "years_searched": 15,
            "form_type": "Form 15",
            "encumbrances": ["Mortgage to Apex Co-op Bank"]
        },
        "rtc_details": {
            "bhoomi_verified": True,
            "extent_acres": 0.8,
            "nature_of_land": "Agricultural Dry"
        },
        "mutation_details": {
            "mr_extract_present": False,
            "mr_status": "Pending"
        },
        "survey_details": {
            "tippani_present": True,
            "akarband_present": False,
            "sketch_11e_present": False,
            "podi_completed": False
        },
        "conversion_details": {
            "is_converted": False,
            "dc_conversion_order_present": False,
            "conversion_fine_paid": False
        },
        "ptcl_details": {
            "is_sc_st_grant_land": True,
            "govt_permission_obtained": False
        },
        "kharab_details": {
            "b_kharab_acres": 0.25,
            "a_kharab_acres": 0.0
        },
        "acquisition_details": {
            "bda_notified": True,
            "kiadb_notified": False,
            "nhai_notified": False
        },
        "environmental_details": {
            "lake_distance_meters": 45,
            "rajakaluve_distance_meters": 10,
            "rajakaluve_type": "Primary"
        },
        "master_plan_details": {
            "zone": "Green Belt"
        },
        "layout_details": {
            "approved_by_authority": False,
            "authority_name": "None (Revenue Layout)",
            "nopa_released": False
        },
        "khata_details": {
            "khata_type": "B Khata",
            "pid_number_valid": False
        },
        "tax_details": {
            "tax_paid_current_year": False,
            "sas_receipts_available": False
        },
        "litigation_details": {
            "pending_suits": ["OS 452/2021 (Partition Suit in City Civil Court)"],
            "court_injunction_active": True
        },
        "physical_survey_details": {
            "total_station_survey_done": False,
            "boundary_matches_deed": False,
            "encroached_acres": 0.3
        },
        "gis_details": {
            "ht_line_clearance": False,
            "road_widening_impact": True
        },
        "mortgage_details": {
            "bank_mortgage_active": True,
            "bank_noc_obtained": False,
            "mortgagee_bank": "Apex Co-operative Bank"
        },
        "sale_agreement_details": {
            "terms_fair": False,
            "unilateral_forfeiture_clause": True,
            "indemnity_clause_included": False
        },
        "lawyer_opinion_details": {
            "opinion_obtained": False,
            "lawyer_name": "None",
            "title_clear": False
        }
    }
}
