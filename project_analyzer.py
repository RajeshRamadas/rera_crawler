"""
RERA Karnataka Project Analyzer & Intelligence Engine
=====================================================
Analyzes RERA projects by name or registration number.
Extracts complete project metadata, documents, location data, timeline progress,
complaint history, and computes structured Positive (+) and Negative (-) evaluation reports.
"""

import sys
import os
import json
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import psycopg2
import psycopg2.extras

DB_URI = os.getenv("DATABASE_URL", "postgresql://rera_user:rera_password@localhost:5432/rera_db")


def get_db_connection():
    return psycopg2.connect(DB_URI, cursor_factory=psycopg2.extras.DictCursor)


def search_project(query: str, conn=None) -> Optional[Dict[str, Any]]:
    """
    Search for a project by registration_no or project_name (fuzzy / ILIKE).
    Returns the full database record.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    try:
        with conn.cursor() as cur:
            clean_q = query.strip()
            # 1. Direct match on registration number
            cur.execute(
                "SELECT registration_no, acknowledgement_no, project_name, firm_name, crawled_at, data FROM rera_projects WHERE registration_no = %s;",
                (clean_q,)
            )
            row = cur.fetchone()
            if row:
                return dict(row)

            # 2. Match ILIKE on project name
            cur.execute(
                "SELECT registration_no, acknowledgement_no, project_name, firm_name, crawled_at, data FROM rera_projects WHERE project_name ILIKE %s ORDER BY crawled_at DESC LIMIT 1;",
                (f"%{clean_q}%",)
            )
            row = cur.fetchone()
            if row:
                return dict(row)

            # 3. Match ILIKE on firm name / promoter
            cur.execute(
                "SELECT registration_no, acknowledgement_no, project_name, firm_name, crawled_at, data FROM rera_projects WHERE firm_name ILIKE %s ORDER BY crawled_at DESC LIMIT 1;",
                (f"%{clean_q}%",)
            )
            row = cur.fetchone()
            if row:
                return dict(row)

            return None
    finally:
        if close_conn:
            conn.close()


def analyze_project_data(project_row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analyzes project record and generates structured Positives, Negatives,
    Risk Level, and Executive Summary.
    """
    reg_no = project_row.get("registration_no") or ""
    proj_name = project_row.get("project_name") or "Unnamed Project"
    firm_name = project_row.get("firm_name") or "Unknown Developer"
    raw_data = project_row.get("data") or {}

    # Core attributes
    proj_details = raw_data.get("project_details", {}).get("Project Details", {})
    if not proj_details and isinstance(raw_data.get("project_details"), dict):
        proj_details = raw_data.get("project_details", {})
        
    status = (raw_data.get("status") or proj_details.get("Project Status") or "UNKNOWN").upper()
    district = raw_data.get("district") or raw_data.get("project_location", {}).get("district") or proj_details.get("District") or "N/A"
    taluk = raw_data.get("taluk") or raw_data.get("project_location", {}).get("taluk") or proj_details.get("Taluk") or "N/A"
    project_type = raw_data.get("project_type") or proj_details.get("Project Type") or "N/A"
    approved_on = raw_data.get("approved_on") or "N/A"
    proposed_comp_date = raw_data.get("proposed_completion_date") or proj_details.get("Proposed Project Completion Date") or proj_details.get("Proposed Completion Date") or "N/A"
    orig_comp_date = raw_data.get("proposed_completion_date_at_registration") or proposed_comp_date
    
    # Location
    loc_info = raw_data.get("project_location") or {}
    lat = loc_info.get("latitude")
    lon = loc_info.get("longitude")
    coord_source = loc_info.get("coordinate_source")
    survey_nos = loc_info.get("survey_numbers") or []
    sros = loc_info.get("sub_registrar_offices") or []

    # Quality score
    data_quality = raw_data.get("data_quality") or {}
    quality_score = data_quality.get("quality_score", 50)
    is_complete = data_quality.get("is_complete", False)
    dq_warnings = data_quality.get("warnings") or []
    dq_checks = data_quality.get("checks") or {}

    # Documents
    uploaded_docs = raw_data.get("uploaded_documents") or []
    overview_docs = [v for k, v in raw_data.get("project_overview_details", {}).items() if ("URL" in k or "url" in k.lower()) and v and (isinstance(v, list) or isinstance(v, str))]
    enquired_docs = raw_data.get("enquired_documents") or []
    total_doc_count = len(uploaded_docs) + len(overview_docs)
    doc_types = [str(d.get("financial_year", "")).lower() for d in uploaded_docs if isinstance(d, dict)]
    doc_raw_names = " ".join(doc_types)

    # Complaints
    complaints = raw_data.get("complaints") or {}
    on_proj_complaints = complaints.get("on_project") or []
    on_prom_complaints = complaints.get("on_promoter") or []
    if not on_proj_complaints and isinstance(raw_data.get("complaints_on_project"), list):
        on_proj_complaints = raw_data.get("complaints_on_project")
    if not on_prom_complaints and isinstance(raw_data.get("complaints_on_promoter"), list):
        on_prom_complaints = raw_data.get("complaints_on_promoter")

    tot_proj_complaints = len(on_proj_complaints)
    tot_prom_complaints = len(on_prom_complaints)
    tot_complaints = tot_proj_complaints + tot_prom_complaints

    under_enquiry_complaints = []
    for c in on_proj_complaints + on_prom_complaints:
        if isinstance(c, dict):
            c_status = str(c.get("STATUS", "")).upper()
            if "UNDER ENQUIRY" in c_status or "PENDING" in c_status:
                under_enquiry_complaints.append(c)

    # --- RULE EVALUATION ENGINE ---
    positives = []
    negatives = []
    risk_score_deductions = 0

    # 1. RERA Approval & Status Analysis
    if "APPROVED" in status:
        positives.append({
            "category": "Regulatory Approval",
            "title": "RERA Approved Project",
            "description": f"Project is officially registered and approved under Karnataka RERA (Reg No: {reg_no})."
        })
    elif "COMPLETED" in status:
        positives.append({
            "category": "Regulatory Status",
            "title": "Completed Project",
            "description": "Project has achieved completion status under RERA Karnataka guidelines."
        })
    elif "EXPIRED" in status or "REVOKED" in status or "SUSPENDED" in status:
        negatives.append({
            "category": "Regulatory Risk",
            "severity": "HIGH",
            "title": f"Project Status Alert ({status})",
            "description": f"The RERA registration status is currently '{status}', indicating regulatory compliance or timeline issues."
        })
        risk_score_deductions += 30

    # 2. Data Quality & Filing Completeness
    if quality_score >= 80:
        positives.append({
            "category": "Data & Transparency",
            "title": "High Filing Quality & Transparency",
            "description": f"RERA submission quality score is {quality_score}/100 with comprehensive documentation."
        })
    elif quality_score < 60:
        negatives.append({
            "category": "Data & Transparency",
            "severity": "MEDIUM",
            "title": "Incomplete RERA Filings",
            "description": f"RERA filing quality score is low ({quality_score}/100). Key details or attachments may be omitted."
        })
        risk_score_deductions += 15

    # 3. Geo-Coordinates & Location Verification
    if lat and lon:
        positives.append({
            "category": "Location Intelligence",
            "title": "Verified Spatial Coordinates",
            "description": f"Geographic position verified at ({lat}, {lon}) via {coord_source or 'RERA GIS'}."
        })
    else:
        negatives.append({
            "category": "Location Intelligence",
            "severity": "LOW",
            "title": "Unverified Exact Map Location",
            "description": "Spatial coordinates are not present or incomplete in RERA records."
        })
        risk_score_deductions += 5

    # 4. Land & Legal Details
    if survey_nos:
        positives.append({
            "category": "Land Details",
            "title": "Survey Numbers Verified",
            "description": f"Associated with survey number(s): {', '.join(map(str, survey_nos[:5]))}."
        })
    if sros:
        positives.append({
            "category": "Legal Jurisdiction",
            "title": "Sub-Registrar Office Identified",
            "description": f"Jurisdiction registered under SRO: {', '.join(map(str, sros[:3]))}."
        })

    # 5. Complaints & Legal Disputes
    if tot_complaints == 0:
        positives.append({
            "category": "Legal & Disputes",
            "title": "Zero RERA Complaints",
            "description": "No buyer complaints have been registered against this project or promoter on RERA Karnataka portal."
        })
    else:
        severity = "HIGH" if len(under_enquiry_complaints) > 0 or tot_complaints >= 5 else "MEDIUM"
        negatives.append({
            "category": "Legal & Disputes",
            "severity": severity,
            "title": f"RERA Complaints Recorded ({tot_complaints} total)",
            "description": f"Found {tot_proj_complaints} complaint(s) on project and {tot_prom_complaints} on promoter. ({len(under_enquiry_complaints)} active under enquiry)."
        })
        deduct = 25 if severity == "HIGH" else 10
        risk_score_deductions += deduct

    # 6. Timeline & Delay Analysis
    if proposed_comp_date != "N/A":
        try:
            # Parse DD/MM/YYYY or YYYY-MM-DD
            if "/" in proposed_comp_date:
                comp_dt = datetime.strptime(proposed_comp_date, "%d/%m/%Y")
            else:
                comp_dt = datetime.strptime(proposed_comp_date, "%Y-%m-%d")
            
            now_dt = datetime.now(tz=timezone.utc).replace(tzinfo=None)
            if comp_dt < now_dt and "COMPLETED" not in status:
                negatives.append({
                    "category": "Timeline & Delay",
                    "severity": "HIGH",
                    "title": "Proposed Completion Date Overdue",
                    "description": f"Project target date was {proposed_comp_date}, which has passed without official completion status."
                })
                risk_score_deductions += 20
            else:
                positives.append({
                    "category": "Timeline",
                    "title": "Defined Completion Schedule",
                    "description": f"Target completion date declared as {proposed_comp_date}."
                })
        except Exception:
            pass

    # 7. Document Audit Check
    has_encumbrance = "encumbrance" in doc_raw_names or "ec" in doc_raw_names
    has_title = "title" in doc_raw_names or "ownership" in doc_raw_names
    has_conversion = "conversion" in doc_raw_names or "alienation" in doc_raw_names
    has_plan = "plan" in doc_raw_names or "layout" in doc_raw_names or "sanction" in doc_raw_names

    found_doc_highlights = []
    if total_doc_count > 0:
        positives.append({
            "category": "Documentation",
            "title": f"{total_doc_count} Document Record(s) Uploaded",
            "description": "Promoter has uploaded property layout, inventory details, or legal filings."
        })
    else:
        negatives.append({
            "category": "Documentation",
            "severity": "MEDIUM",
            "title": "No Document Attachments Found",
            "description": "No legal certificates or site plans are publicly attached under uploaded documents."
        })
        risk_score_deductions += 10

    # --- 8 ROLLED-UP AXES (P-01 to P-37) WITH EXPLICIT SCORE JUSTIFICATIONS ---
    gate_ok = status in ["APPROVED", "COMPLETED"]

    # 1. Regulatory Compliance (P-01 to P-04)
    # Fix #8: If gateOK=False, force compliance_score = 0 so a suspended/expired
    # project cannot contribute positive weight to the overall health score.
    compliance_reasons = []
    if status in ["APPROVED", "COMPLETED"]:
        compliance_score = 95
        compliance_reasons.append(f"✓ Project actively registered under status '{status}' (Reg No: {reg_no}).")
    elif status == "NEW LAUNCH":
        compliance_score = 80
        compliance_reasons.append("⚠ Project is in initial launch phase.")
    else:
        compliance_score = 0  # Hard gate: suspended/expired/unknown cannot score on compliance
        compliance_reasons.append(f"❌ HARD GATE FAIL: Project status '{status}' — registration is not active. Score forced to 0.")

    # 2. Litigation Risk (P-05 to P-11) — INVERTED (100 = safe, 0 = heavy litigation)
    litigation_reasons = []
    if tot_complaints == 0:
        litigation_score = 100
        litigation_reasons.append("✓ 0 RERA complaints registered against project or promoter.")
    else:
        if tot_complaints <= 2:
            base_lit_ded = 20
        elif tot_complaints <= 5:
            base_lit_ded = 35
        elif tot_complaints <= 15:
            base_lit_ded = 55
        elif tot_complaints <= 30:
            base_lit_ded = 70
        else:
            base_lit_ded = 85

        litigation_score = max(0, 100 - base_lit_ded)
        litigation_reasons.append(f"❌ DEDUCTION -{base_lit_ded} pts: Recorded {tot_complaints} total RERA complaints ({tot_proj_complaints} on project, {tot_prom_complaints} on promoter).")

        if len(under_enquiry_complaints) > 0:
            litigation_score -= 15
            litigation_reasons.append(f"❌ DEDUCTION -15 pts: {len(under_enquiry_complaints)} complaint(s) actively under enquiry.")

        # Fix #7: Single final clamp — safe against any future additional deductions
        litigation_score = max(0, litigation_score)

    # 3. Timeline Adherence (P-12 to P-16)
    timeline_reasons = []
    if status == "COMPLETED":
        timeline_score = 100
        timeline_reasons.append("✓ Project has achieved official completion status.")
    elif proposed_comp_date != "N/A":
        timeline_score = 90
        try:
            comp_dt = datetime.strptime(proposed_comp_date, "%d/%m/%Y" if "/" in proposed_comp_date else "%Y-%m-%d")
            if comp_dt < datetime.now(tz=timezone.utc).replace(tzinfo=None) and "COMPLETED" not in status:
                timeline_score = 35
                timeline_reasons.append(f"❌ DEDUCTION -55 pts: Declared target date {proposed_comp_date} has expired without completion certificate.")
            else:
                timeline_reasons.append(f"✓ Project schedule declared with target completion date {proposed_comp_date}.")
        except Exception:
            timeline_reasons.append(f"⚠ Declared completion date: {proposed_comp_date}.")
    else:
        timeline_score = 50
        timeline_reasons.append("❌ DEDUCTION -50 pts: Target completion date not specified in public records.")

    # 4. Land & Title Clarity (P-17 to P-21)
    title_reasons = []
    title_score = 100
    if not survey_nos:
        title_score -= 20
        title_reasons.append("❌ DEDUCTION -20 pts: Land survey numbers missing or unverified.")
    else:
        title_reasons.append(f"✓ Associated survey numbers verified: {', '.join(map(str, survey_nos[:3]))}.")

    if not sros:
        title_score -= 20
        title_reasons.append("❌ DEDUCTION -20 pts: Sub-Registrar Office jurisdiction missing.")
    else:
        title_reasons.append(f"✓ Sub-Registrar Office jurisdiction identified: {', '.join(map(str, sros[:2]))}.")

    if total_doc_count == 0:
        title_score -= 20
        title_reasons.append("❌ DEDUCTION -20 pts: No legal attachments or title documents uploaded.")
    else:
        title_reasons.append(f"✓ {total_doc_count} document record(s) uploaded.")

    # 5. Location & Connectivity (P-22 to P-29)
    location_reasons = []
    location_score = 100
    if not (lat and lon):
        location_score -= 35
        location_reasons.append("❌ DEDUCTION -35 pts: Exact geographic coordinates (Lat/Lon) unverified.")
    else:
        location_reasons.append(f"✓ Geographic coordinates verified ({lat}, {lon}).")

    if not district or district == "N/A":
        location_score -= 25
        location_reasons.append("❌ DEDUCTION -25 pts: District jurisdiction not declared.")
    else:
        location_reasons.append(f"✓ District identified: {district}.")

    # 6. Layout & Amenities (P-30 to P-33)
    layout_reasons = []
    layout_score = 100
    if total_doc_count == 0:
        layout_score -= 30
        layout_reasons.append("❌ DEDUCTION -30 pts: Missing layout plan and unit inventory filings.")
    else:
        layout_reasons.append(f"✓ Layout filings and inventory records uploaded ({total_doc_count} attachments).")

    if project_type == "N/A":
        layout_score -= 20
        layout_reasons.append("❌ DEDUCTION -20 pts: Unspecified project layout category.")
    else:
        layout_reasons.append(f"✓ Project layout type: {project_type}.")

    # 7. Promoter Reputation (P-34 to P-35)
    # Fix #5: Cap raised to 100 (was 60) so builders with 20+ complaints can reach score 0.
    # Previously a builder with 100 complaints scored the same as one with 12.
    promoter_reasons = []
    promoter_score = 100
    if tot_prom_complaints > 0:
        prom_ded = min(100, tot_prom_complaints * 5)
        promoter_score = max(0, 100 - prom_ded)
        promoter_reasons.append(f"❌ DEDUCTION -{prom_ded} pts: Recorded {tot_prom_complaints} complaint(s) on builder/promoter.")
    else:
        promoter_reasons.append(f"✓ Zero RERA complaints registered against promoter '{firm_name}'.")

    # 8. Data Transparency (P-36 to P-37)
    # Fix #6: Only flag ❌ DEDUCTION if quality_score < 80.
    # Scores in 80–99 range get a ⚠ note (minor gap, not a penalty).
    transparency_reasons = []
    transparency_score = quality_score
    if quality_score >= 100:
        transparency_reasons.append("✓ 100/100 Perfect Filing Transparency score.")
    elif quality_score >= 80:
        transparency_reasons.append(f"⚠ Filing quality is {quality_score}/100 — minor gaps detected but within acceptable range.")
        if dq_warnings:
            transparency_reasons.append(f"⚠ Filing warnings: {', '.join(dq_warnings[:2])}.")
    else:
        gap = 100 - quality_score
        transparency_reasons.append(f"❌ DEDUCTION -{gap} pts: Data filing quality score is low ({quality_score}/100). Key disclosures missing.")
        if dq_warnings:
            transparency_reasons.append(f"⚠ Filing warnings: {', '.join(dq_warnings[:2])}.")

    # Risk Score & Verdict Calculation
    # Derived from weighted average of 8 axis scores (same weights as the radar chart)
    # so overall_health_score and the radar axes are always consistent.
    _axis_weights = {
        'compliance': 1.0, 'litigation': 1.2, 'timeline': 1.1,
        'title': 1.1, 'location': 0.9, 'layout': 0.7,
        'promoter': 1.0, 'transparency': 0.8
    }
    _axis_scores = {
        'compliance': compliance_score, 'litigation': litigation_score,
        'timeline': timeline_score, 'title': title_score,
        'location': location_score, 'layout': layout_score,
        'promoter': promoter_score, 'transparency': transparency_score
    }
    _total_weight = sum(_axis_weights.values())
    overall_health_score = round(
        sum(_axis_scores[k] * _axis_weights[k] for k in _axis_weights) / _total_weight
    )
    overall_health_score = max(0, min(100, overall_health_score))

    if overall_health_score >= 80:
        risk_level = "LOW_RISK"
        verdict = "Strong RERA project with verified credentials, solid filings, and clean legal standing."
    elif overall_health_score >= 60:
        risk_level = "MODERATE_RISK"
        verdict = "Moderate project risk. Verify pending timeline dates and check complaint history."
    elif overall_health_score >= 40:
        risk_level = "HIGH_RISK"
        verdict = "High risk detected due to complaints, missing filings, or past target completion dates."
    else:
        risk_level = "CRITICAL_RISK"
        verdict = "Critical warning! Multiple red flags including status alerts, heavy complaints, and unverified data."

    return {
        "project_summary": {
            "registration_no": reg_no,
            "project_name": proj_name,
            "developer_name": firm_name,
            "status": status,
            "project_type": project_type,
            "district": district,
            "taluk": taluk,
            "approved_on": approved_on,
            "proposed_completion_date": proposed_comp_date,
            "coordinates": {"latitude": lat, "longitude": lon} if lat and lon else None,
            "survey_numbers": survey_nos,
            "sub_registrar_offices": sros
        },
        "overall_evaluation": {
            "health_score": overall_health_score,
            "risk_level": risk_level,
            "verdict": verdict,
            "positives_count": len(positives),
            "negatives_count": len(negatives)
        },
        "radar_profile": {
            "gateOK": gate_ok,
            "scores": {
                "compliance": compliance_score,
                "litigation": litigation_score,
                "timeline": timeline_score,
                "title": title_score,
                "location": location_score,
                "layout": layout_score,
                "promoter": promoter_score,
                "transparency": transparency_score
            },
            "reasons": {
                "compliance": compliance_reasons,
                "litigation": litigation_reasons,
                "timeline": timeline_reasons,
                "title": title_reasons,
                "location": location_reasons,
                "layout": layout_reasons,
                "promoter": promoter_reasons,
                "transparency": transparency_reasons
            }
        },
        "positives": positives,
        "negatives": negatives,
        "complaints_summary": {
            "total_complaints": tot_complaints,
            "project_complaints_count": tot_proj_complaints,
            "promoter_complaints_count": tot_prom_complaints,
            "active_under_enquiry_count": len(under_enquiry_complaints),
            "recent_complaints": (on_proj_complaints + on_prom_complaints)[:5]
        },
        "data_quality": {
            "score": quality_score,
            "is_complete": is_complete,
            "warnings": dq_warnings
        },
        "raw_data": raw_data
    }


def analyze_by_name_or_reg(query: str) -> Dict[str, Any]:
    """
    Main entrypoint: Accepts a project name or registration number,
    fetches all DB information, and returns the analysis.
    """
    record = search_project(query)
    if not record:
        return {
            "error": True,
            "message": f"No project found matching query '{query}'. Please check the name or registration number."
        }

    analysis = analyze_project_data(record)
    analysis["error"] = False
    return analysis


if __name__ == "__main__":
    query_term = sys.argv[1] if len(sys.argv) > 1 else "ELEGANT TRANQUILITY"
    print(f"[*] Analyzing Project: {query_term} ...\n")
    result = analyze_by_name_or_reg(query_term)
    print(json.dumps(result, indent=2))
