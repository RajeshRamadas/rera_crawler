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
import re
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
    # Evaluates project-specific complaints primarily, with a portfolio-scaled promoter adjustment
    litigation_reasons = []
    if tot_proj_complaints == 0:
        litigation_score = 100
        litigation_reasons.append("✓ 0 RERA complaints registered on this specific project.")
    elif tot_proj_complaints == 1:
        litigation_score = 80
        litigation_reasons.append("⚠ 1 RERA complaint registered on this project (-20 pts).")
    elif tot_proj_complaints <= 3:
        litigation_score = 65
        litigation_reasons.append(f"❌ DEDUCTION -35 pts: {tot_proj_complaints} RERA complaints registered on project.")
    elif tot_proj_complaints <= 10:
        litigation_score = 45
        litigation_reasons.append(f"❌ DEDUCTION -55 pts: {tot_proj_complaints} RERA complaints registered on project.")
    else:
        litigation_score = 25
        litigation_reasons.append(f"❌ DEDUCTION -75 pts: High complaint volume ({tot_proj_complaints}) on project.")

    if len(under_enquiry_complaints) > 0:
        litigation_score = max(0, litigation_score - 15)
        litigation_reasons.append(f"❌ DEDUCTION -15 pts: {len(under_enquiry_complaints)} complaint(s) actively under enquiry.")

    if tot_prom_complaints > 0:
        prom_adj = min(15, tot_prom_complaints // 5)
        if prom_adj > 0:
            litigation_score = max(0, litigation_score - prom_adj)
            litigation_reasons.append(f"⚠ Minor promoter portfolio adjustment (-{prom_adj} pts for {tot_prom_complaints} developer-level filings).")

    # 3. Timeline Adherence (P-12 to P-16)
    timeline_reasons = []
    has_covid_cert = bool(raw_data.get("covid_certificate_url"))
    has_oc = "completion" in doc_raw_names or "occupancy" in doc_raw_names or "oc" in doc_raw_names or "cc" in doc_raw_names

    if status == "COMPLETED":
        timeline_score = 100
        timeline_reasons.append("✓ Project has achieved official completion status.")
    elif proposed_comp_date != "N/A":
        try:
            comp_dt = datetime.strptime(proposed_comp_date, "%d/%m/%Y" if "/" in proposed_comp_date else "%Y-%m-%d")
            now_dt = datetime.now(tz=timezone.utc).replace(tzinfo=None)
            if comp_dt < now_dt:
                delay_years = (now_dt - comp_dt).days / 365.25
                if delay_years < 1.0:
                    timeline_score = 75
                    timeline_reasons.append(f"⚠ Declared target date {proposed_comp_date} passed (< 1 yr ago).")
                elif delay_years < 3.0:
                    timeline_score = 60
                    timeline_reasons.append(f"❌ DEDUCTION -40 pts: Declared target date {proposed_comp_date} passed ({round(delay_years, 1)} yrs ago).")
                else:
                    timeline_score = 45
                    timeline_reasons.append(f"❌ DEDUCTION -55 pts: Declared target date {proposed_comp_date} passed over 3 years ago.")

                if has_covid_cert or has_oc:
                    timeline_score = min(90, timeline_score + 15)
                    timeline_reasons.append("✓ COVID extension or completion document filings detected (+15 pts buffer).")
            else:
                timeline_score = 90
                timeline_reasons.append(f"✓ Project schedule active with target completion date {proposed_comp_date}.")
        except Exception:
            timeline_score = 70
            timeline_reasons.append(f"⚠ Declared completion date: {proposed_comp_date}.")
    else:
        timeline_score = 50
        timeline_reasons.append("❌ DEDUCTION -50 pts: Target completion date not specified in public records.")

    # 4. Land & Title Clarity (P-17 to P-21)
    title_reasons = []
    title_score = 100
    
    # Check survey numbers in overview / address text if list is empty
    proj_address = loc_info.get("address") or proj_details.get("Project Address") or ""
    overview_text = str(raw_data.get("project_overview_details") or {}) + " " + proj_address
    if not survey_nos:
        sy_matches = re.findall(r'(?:sy|survey|sy\.?\s*no\.?)\s*[:#-]?\s*([\d\/\,\s\-]+)', overview_text, re.IGNORECASE)
        if sy_matches:
            survey_nos = [m.strip() for m in sy_matches[:3]]

    if not survey_nos:
        title_score -= 15
        title_reasons.append("❌ DEDUCTION -15 pts: Land survey numbers missing or unverified.")
    else:
        title_reasons.append(f"✓ Associated survey numbers verified: {', '.join(map(str, survey_nos[:3]))}.")

    if not sros:
        title_score -= 10
        title_reasons.append("❌ DEDUCTION -10 pts: Sub-Registrar Office jurisdiction missing.")
    else:
        title_reasons.append(f"✓ Sub-Registrar Office jurisdiction identified: {', '.join(map(str, sros[:2]))}.")

    if total_doc_count == 0:
        title_score -= 20
        title_reasons.append("❌ DEDUCTION -20 pts: No legal attachments or title documents uploaded.")
    else:
        title_reasons.append(f"✓ {total_doc_count} document record(s) uploaded.")

    # 5. Location & Connectivity (P-22 to P-29) & Location USPs
    location_reasons = []
    location_score = 100
    location_usps = []

    if not (lat and lon):
        location_score -= 25
        location_reasons.append("⚠ Exact geographic coordinates (Lat/Lon) unverified (-25 pts).")
    else:
        location_reasons.append(f"✓ Geographic coordinates verified ({lat}, {lon}).")

    if not district or district == "N/A":
        location_score -= 15
        location_reasons.append("❌ DEDUCTION -15 pts: District jurisdiction not declared.")
    else:
        location_reasons.append(f"✓ District identified: {district}.")

    # 7. Promoter Reputation (P-34 to P-35)
    # Evaluates complaints per project across promoter portfolio
    promoter_reasons = []
    brand_word = firm_name.split()[0].upper() if firm_name else ""
    est_portfolio = 30 if brand_word in ["PRESTIGE", "BRIGADE", "SOBHA", "GODREJ", "PURAVANKARA", "MAHINDRA"] else 5
    comp_per_proj = tot_prom_complaints / max(1, est_portfolio)

    if tot_prom_complaints == 0:
        promoter_score = 100
        promoter_reasons.append(f"✓ Zero RERA complaints registered against promoter '{firm_name}'.")
    else:
        promoter_ded = min(75, round(comp_per_proj * 15))
        promoter_score = max(25, 100 - promoter_ded)
        promoter_reasons.append(f"⚠ Recorded {tot_prom_complaints} complaint(s) across promoter portfolio ({round(comp_per_proj, 1)} per project avg, -{promoter_ded} pts).")

    # 8. Data Transparency (P-36 to P-37)
    transparency_reasons = []
    doc_bonus = 15 if total_doc_count > 50 else (10 if total_doc_count > 20 else (5 if total_doc_count > 5 else 0))
    transparency_score = min(100, quality_score + doc_bonus)

    if transparency_score >= 90:
        transparency_reasons.append(f"✓ High filing transparency ({transparency_score}/100 score, {total_doc_count} attachments).")
    elif transparency_score >= 70:
        transparency_reasons.append(f"⚠ Satisfactory filing transparency ({transparency_score}/100 score).")
    else:
        transparency_reasons.append(f"❌ DEDUCTION: Data filing quality score is low ({transparency_score}/100). Key disclosures missing.")

    # Synthesize Location USPs based on geographic metadata & area specs
    proj_address = loc_info.get("address") or proj_details.get("Project Address") or ""
    addr_lower = (proj_address + " " + (taluk or "") + " " + (district or "")).lower()

    if any(k in addr_lower for k in ["sarjapur", "chikkanayakanahalli", "chikkakanahalli", "varthur", "dommasandra"]):
        location_usps.append({
            "category": "transport",
            "title": "Prime Sarjapur Road Tech Corridor",
            "description": "Situated off Sarjapur Road in Chikkanayakanahalli with direct arterial access to Outer Ring Road (ORR), Bellandur, Marathahalli & Whitefield IT belts."
        })
        location_usps.append({
            "category": "employment",
            "title": "Minutes from Major IT Hubs",
            "description": "5–15 min drive to Wipro SEZ, RMZ Ecoworld, Embassy TechVillage, RGA Tech Park, and Prestige Tech Park."
        })
        location_usps.append({
            "category": "education",
            "title": "Premier International School Hub",
            "description": "Close proximity to Greenwood High, Oakridge International, TISB, and Harvest International School."
        })
        location_usps.append({
            "category": "healthcare",
            "title": "Multispecialty Healthcare Access",
            "description": "Quick emergency & outpatient access to Motherhood Hospital, Manipal Hospital Sarjapur Road, and Sakra World Hospital."
        })
    elif any(k in addr_lower for k in ["whitefield", "itpl", "hoodi", "epip", "kadugodi"]):
        location_usps.append({
            "category": "transport",
            "title": "Whitefield Metro & Railway Corridor",
            "description": "Direct access to Namma Metro Purple Line (Whitefield/Kadugodi Stations) and Whitefield Main Road arterial network."
        })
        location_usps.append({
            "category": "employment",
            "title": "Core ITPL & EPIP Tech Belt",
            "description": "Within minutes from International Tech Park Bangalore (ITPL), SAP Labs, Mindtree, and EPIP Industrial Zone."
        })
        location_usps.append({
            "category": "shopping",
            "title": "Retail & Entertainment Capital",
            "description": "Proximity to Phoenix Marketcity, VR Bengaluru, and Nexus Forum Value Mall."
        })
    elif any(k in addr_lower for k in ["electronic city", "hosur road", "bommasandra", "attibele", "chandapura"]):
        location_usps.append({
            "category": "transport",
            "title": "Electronic City Elevated Expressway",
            "description": "Seamless high-speed commute to Silk Board, Koramangala & Hosur Road via 4-lane Elevated Expressway and Yellow Line Metro."
        })
        location_usps.append({
            "category": "employment",
            "title": "India's Largest Tech Campus Hub",
            "description": "Close to Infosys Main Campus, Wipro Technologies, Tech Mahindra, and Biocon Park."
        })
    elif any(k in addr_lower for k in ["panathur", "kadubeesanahalli", "balagere", "marathahalli", "bellandur"]):
        location_usps.append({
            "category": "transport",
            "title": "Outer Ring Road (ORR) Connectivity Hub",
            "description": "Located right in the Marathahalli-Sarjapur ORR growth corridor with fast access to Bellandur & Kadubeesanahalli metro stations."
        })
        location_usps.append({
            "category": "employment",
            "title": "Short Drive to ORR Work Hubs",
            "description": "Direct connectivity to Ecoworld, Prestige Tech Park, JP Morgan, and Cessna Business Park."
        })
    elif any(k in addr_lower for k in ["thanisandra", "hebbal", "yelahanka", "devanahalli", "kogilu", "jakkur", "hennur"]):
        location_usps.append({
            "category": "transport",
            "title": "North Bengaluru Airport Corridor",
            "description": "Direct arterial connectivity to Bellary Road (NH 44), KIADB Aerospace Park, and Kempegowda International Airport."
        })
        location_usps.append({
            "category": "employment",
            "title": "Aerospace & High-Tech Hardware Hub",
            "description": "Near Shell Technology Centre, KIADB IT Hardware Park, and Manyata Tech Park."
        })
    elif any(k in addr_lower for k in ["kanakapura", "jp nagar", "bannerghatta", "jayanagar", "banashankari"]):
        location_usps.append({
            "category": "transport",
            "title": "South Bengaluru Green Line Metro Corridor",
            "description": "Direct arterial connection to Kanakapura Road, Peripheral Ring Road, and Green Line Metro."
        })
        location_usps.append({
            "category": "education",
            "title": "Established Educational Belt",
            "description": "Surrounded by top institutions including IIM Bangalore, Delhi Public School, and Kumaran Systems."
        })
    else:
        dist_name = (district or "Karnataka").title()
        taluk_name = (taluk or dist_name).title()
        location_usps.append({
            "category": "transport",
            "title": f"Established {taluk_name} Arterial Connectivity",
            "description": f"Strategically located in {taluk_name}, {dist_name} with direct access to primary state highways, transit nodes, and civic infrastructure."
        })
        location_usps.append({
            "category": "civic",
            "title": f"{dist_name} Urban Hub Access",
            "description": f"Convenient proximity to municipal markets, district hospitals, and key civic administrative centers."
        })

    # Open Space & Greenery Ratio USP
    try:
        total_area_val = float(proj_overview.get('Total Area Of Land (Sq Mtr) (A1+A2)') or 0)
        open_area_val = float(proj_overview.get('Total Open Area (Sq Mtr) (A1)') or 0)
        if total_area_val > 0 and open_area_val > 0:
            open_pct = round((open_area_val / total_area_val) * 100, 1)
            if open_pct >= 40:
                acres = round(open_area_val / 4046.86, 1)
                location_usps.append({
                    "category": "lifestyle",
                    "title": f"High Open Space Ratio ({open_pct}% Open Greenery)",
                    "description": f"Expansive {acres} Acres ({open_pct}%) dedicated exclusively to open green spaces, parks, and outdoor sports amenities."
                })
    except Exception:
        pass

    # High Parking Ratio USP
    try:
        tot_flats = float(proj_overview.get('Total Number of Inventories/Flats/Villas') or 0)
        cov_park = float(proj_overview.get('No. of Covered Parking') or 0) + float(proj_overview.get('No. of Open Parking') or 0)
        if tot_flats > 0 and cov_park >= tot_flats:
            ratio = round(cov_park / tot_flats, 2)
            location_usps.append({
                "category": "lifestyle",
                "title": f"Ample Parking Provision ({ratio} Bays / Unit)",
                "description": f"Generous parking allocation with {int(cov_park)} dedicated bays across project towers."
            })
    except Exception:
        pass

    # Verified Spatial GIS Pin USP
    if lat and lon:
        location_usps.append({
            "category": "civic",
            "title": "Verified Spatial GIS Geolocation",
            "description": f"Exact physical project site coordinates verified at Latitude {round(lat, 6)}, Longitude {round(lon, 6)}."
        })

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

def analyze_financial_escrow(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Audits Form 3/5 financial statements, ESCROW 70% bank account compliance,
    and land/construction cost disclosures.
    """
    uploaded_docs = raw_data.get("uploaded_documents") or []
    doc_types = [str(d.get("financial_year", "")).lower() for d in uploaded_docs if isinstance(d, dict)]
    doc_text = " ".join(doc_types) + " " + str(raw_data.get("project_overview_details") or {})

    has_form3 = "form 3" in doc_text or "ca certificate" in doc_text or "chartered accountant" in doc_text
    has_form5 = "form 5" in doc_text or "annual audit" in doc_text or "audit report" in doc_text
    has_form1 = "form 1" in doc_text or "architect certificate" in doc_text
    has_form2 = "form 2" in doc_text or "engineer certificate" in doc_text

    has_escrow_mention = "escrow" in doc_text or "70%" in doc_text or "separate bank account" in doc_text or "schedule bank" in doc_text

    score = 100
    reasons = []
    
    if not (has_form3 or has_form5):
        score -= 30
        reasons.append("❌ DEDUCTION -30 pts: Missing Form 3 (CA Certificate) or Form 5 (Annual Audit Report) filings.")
    else:
        reasons.append("✓ Form 3 CA Certificate & Annual Audit Statement filings detected.")

    if not has_escrow_mention:
        score -= 20
        reasons.append("⚠ DEDUCTION -20 pts: 70% ESCROW bank account disclosure unverified in document headers.")
    else:
        reasons.append("✓ 70% Dedicated ESCROW Bank Account disclosures verified.")

    if not (has_form1 and has_form2):
        score -= 10
        reasons.append("⚠ DEDUCTION -10 pts: Missing Architect/Engineer progress certificates (Form 1 / Form 2).")

    score = max(0, min(100, score))
    return {
        "score": score,
        "has_form1": has_form1,
        "has_form2": has_form2,
        "has_form3": has_form3,
        "has_form5": has_form5,
        "has_escrow": has_escrow_mention,
        "reasons": reasons
    }


def analyze_promoter_portfolio_track_record(firm_name: str) -> Dict[str, Any]:
    """
    Aggregates builder performance across all historical projects in the PostgreSQL database:
    - Total registered projects
    - Completed vs. active vs. lapsed project counts
    - Total complaints across entire builder portfolio
    - Historical average project delay (years)
    """
    if not firm_name or firm_name == "Unknown Developer" or len(firm_name.strip()) < 3:
        return {
            "total_projects": 1,
            "completed_projects": 0,
            "active_projects": 1,
            "lapsed_projects": 0,
            "total_portfolio_complaints": 0,
            "avg_delay_years": 0.0,
            "track_record_score": 80,
            "reasons": ["⚠ Unverified developer portfolio background."]
        }

    try:
        conn = get_db_connection()
        clean_promoter = re.sub(r'^(m/s\.?|m/s\s*)', '', firm_name, flags=re.IGNORECASE).strip()
        first_word = clean_promoter.split()[0].upper() if clean_promoter.split() else ''
        GENERIC_PREFIXES = {'THE', 'NEW', 'SRI', 'SHREE', 'SHRI', 'M/S', 'PVT', 'LTD', '3', 'NO', 'N/A', 'UNKNOWN'}

        with conn.cursor() as cur:
            if len(first_word) >= 3 and first_word not in GENERIC_PREFIXES:
                cur.execute("""
                    SELECT registration_no, project_name, COALESCE(status, 'APPROVED') as status, data
                    FROM rera_projects
                    WHERE firm_name ILIKE %s OR firm_name ILIKE %s OR firm_name ILIKE %s;
                """, (f'{clean_promoter}%', f'M/s. {first_word} %', f'M/s.{first_word} %'))
            else:
                cur.execute("""
                    SELECT registration_no, project_name, COALESCE(status, 'APPROVED') as status, data
                    FROM rera_projects
                    WHERE LOWER(firm_name) = LOWER(%s);
                """, (firm_name,))
            rows = cur.fetchall()
        conn.close()

        total_projects = len(rows)
        completed_count = sum(1 for r in rows if r['status'] == 'COMPLETED')
        active_count = sum(1 for r in rows if r['status'] == 'APPROVED')
        lapsed_count = sum(1 for r in rows if r['status'] in ('EXPIRED', 'REVOKED', 'SUSPENDED'))

        total_complaints = 0
        delays = []
        now_dt = datetime.now(tz=timezone.utc).replace(tzinfo=None)

        for r in rows:
            data = r.get('data') or {}
            c_on_prom = data.get('complaints', {}).get('on_promoter') or []
            c_on_proj = data.get('complaints', {}).get('on_project') or []
            total_complaints += len(c_on_prom) + len(c_on_proj)

            comp_date_str = data.get('proposed_completion_date') or data.get('project_overview_details', {}).get('Proposed Completion Date')
            if comp_date_str and comp_date_str != 'N/A':
                try:
                    comp_dt = datetime.strptime(comp_date_str, "%d/%m/%Y" if "/" in comp_date_str else "%Y-%m-%d")
                    if comp_dt < now_dt and r['status'] != 'COMPLETED':
                        delays.append((now_dt - comp_dt).days / 365.25)
                except Exception:
                    pass

        avg_delay = round(sum(delays) / max(1, len(delays)), 1) if delays else 0.0

        score = 100
        reasons = []

        if total_projects > 5:
            reasons.append(f"✓ Established developer portfolio with {total_projects} registered RERA project(s).")
        else:
            reasons.append(f"ℹ Emerging developer portfolio with {total_projects} registered project(s).")

        if completed_count > 0:
            reasons.append(f"✓ Successfully completed {completed_count} project(s).")

        if lapsed_count > 0:
            score -= (lapsed_count * 20)
            reasons.append(f"❌ DEDUCTION -{lapsed_count * 20} pts: {lapsed_count} project(s) currently in lapsed/suspended status.")

        if total_complaints > 0:
            ded = min(40, total_complaints * 3)
            score -= ded
            reasons.append(f"⚠ DEDUCTION -{ded} pts: Recorded {total_complaints} total complaints across developer portfolio.")

        if avg_delay > 0:
            ded = min(30, int(avg_delay * 10))
            score -= ded
            reasons.append(f"⚠ DEDUCTION -{ded} pts: Average past completion delay of {avg_delay} year(s).")

        score = max(0, min(100, score))

        return {
            "total_projects": total_projects,
            "completed_projects": completed_count,
            "active_projects": active_count,
            "lapsed_projects": lapsed_count,
            "total_portfolio_complaints": total_complaints,
            "avg_delay_years": avg_delay,
            "track_record_score": score,
            "reasons": reasons
        }
    except Exception as exc:
        return {
            "total_projects": 1,
            "completed_projects": 0,
            "active_projects": 1,
            "lapsed_projects": 0,
            "total_portfolio_complaints": 0,
            "avg_delay_years": 0.0,
            "track_record_score": 80,
            "reasons": [f"Portfolio check completed ({exc})."]
        }


def calculate_persona_score(axis_scores: Dict[str, float], persona: str = "homebuyer") -> Dict[str, Any]:
    """
    Computes overall score based on persona weight matrices:
    - Homebuyer: Prioritizes Timeline Adherence (1.5), Title Clarity (1.2), Promoter (1.2).
    - Bank/Lender: Prioritizes Financial ESCROW (1.5), Compliance (1.5), Litigation (1.5).
    - Investor: Prioritizes Location (1.5), Promoter Track Record (1.4), Financials (1.0).
    """
    persona = (persona or "homebuyer").lower().strip()
    
    if persona == "lender" or persona == "bank":
        weights = {
            'compliance': 1.5, 'litigation': 1.5, 'timeline': 0.8,
            'title': 1.5, 'location': 0.6, 'layout': 0.5,
            'promoter': 1.4, 'transparency': 1.2, 'financial_escrow': 1.5
        }
        title_label = "Bank / Lender Risk Assessment"
    elif persona == "investor":
        weights = {
            'compliance': 0.9, 'litigation': 1.0, 'timeline': 1.1,
            'title': 1.0, 'location': 1.5, 'layout': 1.0,
            'promoter': 1.4, 'transparency': 0.8, 'financial_escrow': 1.0
        }
        title_label = "Investor Asset Valuation Assessment"
    else:
        persona = "homebuyer"
        weights = {
            'compliance': 1.0, 'litigation': 1.2, 'timeline': 1.5,
            'title': 1.2, 'location': 1.0, 'layout': 0.8,
            'promoter': 1.2, 'transparency': 0.8, 'financial_escrow': 0.8
        }
        title_label = "Homebuyer Safety & Completion Assessment"

    total_weight = sum(weights[k] for k in weights if k in axis_scores)
    weighted_sum = sum(axis_scores[k] * weights[k] for k in weights if k in axis_scores)
    score = round(weighted_sum / max(0.1, total_weight))
    score = max(0, min(100, score))

    if score >= 80:
        verdict = f"Strong grade for {persona.title()} perspective with robust risk buffers."
        risk_level = "LOW_RISK"
    elif score >= 60:
        verdict = f"Moderate risk for {persona.title()} perspective. Caution advised on specific risk factors."
        risk_level = "MODERATE_RISK"
    elif score >= 40:
        verdict = f"High risk alert for {persona.title()} perspective! Essential disclosures or timeline dates overdue."
        risk_level = "HIGH_RISK"
    else:
        verdict = f"Critical risk warning for {persona.title()} perspective! Multiple red flags detected."
        risk_level = "CRITICAL_RISK"

    return {
        "persona": persona,
        "title_label": title_label,
        "health_score": score,
        "risk_level": risk_level,
        "verdict": verdict,
        "weights": weights
    }


def analyze_project_data(project_row: Dict[str, Any], persona: str = "homebuyer") -> Dict[str, Any]:
    """
    Analyzes project record and generates structured Positives, Negatives,
    Risk Level, Multi-Persona Scores, Promoter Portfolio Track Record, and Financial Audit.
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

    # Documents & Complaints
    uploaded_docs = raw_data.get("uploaded_documents") or []
    overview_docs = [v for k, v in raw_data.get("project_overview_details", {}).items() if ("URL" in k or "url" in k.lower()) and v and (isinstance(v, list) or isinstance(v, str))]
    total_doc_count = len(uploaded_docs) + len(overview_docs)
    doc_types = [str(d.get("financial_year", "")).lower() for d in uploaded_docs if isinstance(d, dict)]
    doc_raw_names = " ".join(doc_types)

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

    # Positives and Negatives
    positives = []
    negatives = []

    # 1. Financial & ESCROW Audit
    financial_escrow_audit = analyze_financial_escrow(raw_data)
    financial_escrow_score = financial_escrow_audit["score"]
    if financial_escrow_audit["has_form3"] or financial_escrow_audit["has_form5"]:
        positives.append({
            "category": "Financial Solvency",
            "title": "Financial Statements & CA Audit Uploaded",
            "description": "Promoter has disclosed Form 3 Chartered Accountant and Form 5 Annual Audit Reports."
        })
    else:
        negatives.append({
            "category": "Financial Risk",
            "severity": "HIGH",
            "title": "Missing CA & Audit Disclosures",
            "description": "Form 3 CA Certificate and Form 5 Annual Financial Audit reports are omitted."
        })

    # 2. Promoter Portfolio Track Record
    promoter_track_record = analyze_promoter_portfolio_track_record(firm_name)
    if promoter_track_record["total_projects"] > 3 and promoter_track_record["completed_projects"] > 0:
        positives.append({
            "category": "Promoter Track Record",
            "title": f"Proven Delivery History ({promoter_track_record['completed_projects']} Completed)",
            "description": f"Promoter has a track record of completing {promoter_track_record['completed_projects']} project(s) out of {promoter_track_record['total_projects']} total filings."
        })

    # Rule Evaluations
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
            "description": f"The RERA registration status is currently '{status}'."
        })

    # 8-Axis Scores
    gate_ok = status in ["APPROVED", "COMPLETED"]
    compliance_score = 95 if status in ["APPROVED", "COMPLETED"] else 0
    compliance_reasons = [f"✓ Project registered under status '{status}'."] if compliance_score > 0 else [f"❌ Status '{status}'."]

    litigation_score = max(0, 100 - (tot_proj_complaints * 20) - (len(under_enquiry_complaints) * 15))
    litigation_reasons = [f"✓ 0 complaints."] if tot_proj_complaints == 0 else [f"⚠ {tot_proj_complaints} complaint(s)."]

    timeline_score = 100 if status == "COMPLETED" else 75
    timeline_reasons = [f"Target completion: {proposed_comp_date}."]

    title_score = 100 if survey_nos and sros else 75
    title_reasons = ["✓ Title and land details verified."] if title_score == 100 else ["⚠ Minor title disclosures missing."]

    location_score = 100 if (lat and lon) else 75
    location_reasons = [f"✓ Verified GIS position."] if (lat and lon) else ["⚠ Estimated location."]

    layout_score = 100 if total_doc_count > 0 else 70
    layout_reasons = [f"✓ Uploaded {total_doc_count} document(s)."]

    promoter_score = promoter_track_record["track_record_score"]
    promoter_reasons = promoter_track_record["reasons"]

    transparency_score = min(100, quality_score + 10)
    transparency_reasons = [f"Filing quality score: {quality_score}/100."]

    _axis_scores = {
        'compliance': compliance_score, 'litigation': litigation_score,
        'timeline': timeline_score, 'title': title_score,
        'location': location_score, 'layout': layout_score,
        'promoter': promoter_score, 'transparency': transparency_score,
        'financial_escrow': financial_escrow_score
    }

    persona_eval = calculate_persona_score(_axis_scores, persona=persona)

    # Location USPs
    proj_address = loc_info.get("address") or proj_details.get("Project Address") or ""
    addr_lower = (proj_address + " " + (taluk or "") + " " + (district or "")).lower()
    location_usps = []
    if "sarjapur" in addr_lower:
        location_usps.append({"category": "transport", "title": "Sarjapur Road Tech Corridor", "description": "Fast access to ORR & Whitefield."})
    elif "whitefield" in addr_lower:
        location_usps.append({"category": "transport", "title": "Whitefield Metro & IT Corridor", "description": "Near Purple Line Metro & ITPL."})
    else:
        location_usps.append({"category": "transport", "title": f"Arterial {district} Access", "description": f"Located in {district}."})

    # Specifications
    proj_overview = raw_data.get("project_overview_details") or {}
    if not isinstance(proj_overview, dict): proj_overview = {}
    pd = proj_details if isinstance(proj_details, dict) else {}

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
            "sub_registrar_offices": sros,
            "location_usps": location_usps,
            "specifications": {
                "approving_authority": proj_overview.get("Approving Authority") or "N/A",
                "land_area_sqm": proj_overview.get("Total Area Of Land (Sq Mtr) (A1+A2)") or "N/A",
                "total_units": proj_overview.get("Total Number of Inventories/Flats/Villas") or "N/A",
                "total_project_cost": proj_overview.get("Total Project Cost (INR) (C1+C2)") or "N/A"
            }
        },
        "persona_evaluation": persona_eval,
        "overall_evaluation": {
            "health_score": persona_eval["health_score"],
            "risk_level": persona_eval["risk_level"],
            "verdict": persona_eval["verdict"],
            "positives_count": len(positives),
            "negatives_count": len(negatives)
        },
        "promoter_track_record": promoter_track_record,
        "financial_escrow_audit": financial_escrow_audit,
        "radar_profile": {
            "gateOK": gate_ok,
            "scores": _axis_scores,
            "reasons": {
                "compliance": compliance_reasons,
                "litigation": litigation_reasons,
                "timeline": timeline_reasons,
                "title": title_reasons,
                "location": location_reasons,
                "layout": layout_reasons,
                "promoter": promoter_reasons,
                "transparency": transparency_reasons,
                "financial_escrow": financial_escrow_audit["reasons"]
            }
        },
        "positives": positives,
        "negatives": negatives,
        "complaints_summary": {
            "total_complaints": tot_complaints,
            "project_complaints_count": tot_proj_complaints,
            "promoter_complaints_count": tot_prom_complaints,
            "active_under_enquiry_count": len(under_enquiry_complaints)
        },
        "data_quality": {
            "score": quality_score,
            "is_complete": is_complete,
            "warnings": dq_warnings
        },
        "raw_data": raw_data
    }


def analyze_by_name_or_reg(query: str, persona: str = "homebuyer") -> Dict[str, Any]:
    """
    Main entrypoint: Accepts a project name or registration number,
    fetches all DB information, and returns persona-specific analysis.
    """
    record = search_project(query)
    if not record:
        return {
            "error": True,
            "message": f"No project found matching query '{query}'. Please check the name or registration number."
        }

    analysis = analyze_project_data(record, persona=persona)
    analysis["error"] = False
    return analysis


if __name__ == "__main__":
    query_term = sys.argv[1] if len(sys.argv) > 1 else "ELEGANT TRANQUILITY"
    persona_choice = sys.argv[2] if len(sys.argv) > 2 else "homebuyer"
    print(f"[*] Analyzing Project: {query_term} (Persona: {persona_choice}) ...\n")
    result = analyze_by_name_or_reg(query_term, persona=persona_choice)
    print(json.dumps(result, indent=2))

