import os
import uvicorn
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

# --- CONFIGURATION ---
SUPABASE_URL = "https://esmhvcfemenpmpciiucz.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImVzbWh2Y2ZlbWVucG1wY2lpdWN6Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2NTc4OTcwOCwiZXhwIjoyMDgxMzY1NzA4fQ.5X3wzLn44aSsJvauwDHFJF2SuucnQaxTYxGeItj8ICA"

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    print(f"✅ Supabase Connected: {SUPABASE_URL}")
except Exception as e:
    print(f"❌ Connection Failed: {e}")

app = FastAPI(title="HIS Multi-Payer Module")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- MODELS ---
class CoverageRule(BaseModel):
    coverage_percentage: float
    plafon_limit: float
    deductible: float

class EligibilityResponse(BaseModel):
    status: str
    patient_name: str
    nik: Optional[str]
    gender: Optional[str]
    card_number: str
    class_level: int
    sep_no: Optional[str]
    insurance_name: str
    insurance_type: str
    coverage_rules: Optional[CoverageRule] = None

class SEPRequest(BaseModel):
    card_number: str
    diagnosis_code: str
    visit_type: str = "INPATIENT"
    insurance_type: str

class SEPResponse(BaseModel):
    doc_number: str
    doc_type: str
    date: str
    visit_id: str

class GrouperRequest(BaseModel):
    doc_number: str
    icd10_code: str
    icd9_code: Optional[str] = None
    secondary_icd10: List[str] = []
    discharge_status: str = "Pulang Sehat"
    birth_weight: int = 0
    class_level: int = 1

class BillItem(BaseModel):
    name: str
    category: str
    amount: float

class SimulationResponse(BaseModel):
    simulation_type: str
    real_bill: float
    bill_items: List[BillItem]
    inacbg_code: Optional[str] = None
    severity: Optional[str] = None
    tariff: Optional[float] = None
    hospital_margin: Optional[float] = None
    covered_amount: Optional[float] = None
    patient_excess: Optional[float] = None
    plafon_limit: Optional[float] = None
    deductible: Optional[float] = None
    description: str
    description_suffix: Optional[str] = None
    warning_flag: bool = False
    jasa_sarana: Optional[float] = None
    jasa_pelayanan: Optional[float] = None

class AutoFillResponse(BaseModel):
    found: bool
    icd10: Optional[str] = None
    icd9: Optional[str] = None
    invoice_id: Optional[str] = None

# --- ENDPOINTS ---

@app.get("/api/eligibility/{card_number}", response_model=EligibilityResponse)
def check_eligibility(card_number: str):
    print(f"\n🔍 [LOOKUP] Checking Card: {card_number}")
    try:
        # Check if input is MR Number or Name first to get card number
        card_to_search = card_number
        
        # Try MR Number
        p_res = supabase.table("patients").select("id").eq("mr_no", card_number).execute()
        patient_id = p_res.data[0]['id'] if p_res.data else None
        
        # Try Name (if MR not found)
        if not patient_id:
            p_res = supabase.table("patients").select("id").ilike("full_name", f"%{card_number}%").execute()
            if p_res.data: patient_id = p_res.data[0]['id']
            
        # If found via MR/Name, get the active insurance card
        if patient_id:
            ins_res = supabase.table("patient_insurances").select("card_number").eq("patient_id", patient_id).order("created_at", desc=True).limit(1).execute()
            if ins_res.data:
                card_to_search = ins_res.data[0]['card_number']
                print(f"   ℹ️ Resolved '{card_number}' to Card: {card_to_search}")

        # Perform Main Eligibility Check
        response = supabase.table("patient_insurances")\
            .select("*, patients(full_name, nik, gender), insurances(*)")\
            .eq("card_number", card_to_search)\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()

        if not response.data:
            print("   ❌ Card not found")
            raise HTTPException(status_code=404, detail=f"Data tidak ditemukan untuk: {card_number}")

        data = response.data[0]
        
        if data.get('status') is False:
             raise HTTPException(status_code=400, detail="Status Kepesertaan TIDAK AKTIF.")
        
        patient_info = data.get("patients")
        ins_info = data.get("insurances") or {"name": "Unknown", "type": "PRIVATE", "id": "unknown"}

        raw_type = ins_info.get('type')
        db_type = raw_type.upper() if raw_type else 'PRIVATE'
        ins_name = ins_info.get('name', '').upper()
        
        normalized_type = 'PRIVATE'
        if db_type in ['GOVERNMENT', 'BPJS', 'JKN']: normalized_type = 'GOVERNMENT'
        elif db_type == 'COMPANY': normalized_type = 'COMPANY'
        if 'BPJS' in ins_name: normalized_type = 'GOVERNMENT'
            
        cov_rules = None
        insurance_id = ins_info.get('id')
        
        if normalized_type != 'GOVERNMENT':
            cov_res = supabase.table("insurance_coverages").select("*").eq("insurance_id", insurance_id).limit(1).execute()
            if cov_res.data:
                rule = cov_res.data[0]
                cov_rules = CoverageRule(
                    coverage_percentage=float(rule.get('coverage_percentage', 100)),
                    plafon_limit=float(rule.get('plafon_limit', 0)),
                    deductible=float(rule.get('deductible', 0))
                )
            else:
                cov_rules = CoverageRule(coverage_percentage=100, plafon_limit=0, deductible=0)

        sep_val = data.get("sep_no")
        print(f"   ✅ FOUND: {patient_info.get('full_name')} ({normalized_type})")
        
        return EligibilityResponse(
            status="AKTIF",
            patient_name=patient_info.get('full_name', 'Unknown'),
            nik=patient_info.get('nik'),
            gender=patient_info.get("gender"),
            card_number=data.get("card_number"),
            class_level=data.get("class_id", 3),
            sep_no=sep_val,
            insurance_name=ins_info.get("name", "Unknown"),
            insurance_type=normalized_type,
            coverage_rules=cov_rules
        )
    except HTTPException as he: raise he
    except Exception as e:
        print(f"   🔥 ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/sep", response_model=SEPResponse)
def generate_document(payload: SEPRequest):
    print(f"\n📝 [DOC GEN] Generating for: {payload.card_number}")
    try:
        pat_query = supabase.table("patient_insurances").select("patient_id").eq("card_number", payload.card_number).limit(1).execute()
        if not pat_query.data: raise HTTPException(status_code=404, detail="Pasien not found")
        patient_id = pat_query.data[0]['patient_id']
        
        doc_number = ""
        doc_type = ""
        if payload.insurance_type == 'GOVERNMENT':
            doc_type = "SEP"
            doc_number = f"001R001{datetime.now().strftime('%m%d')}{str(int(datetime.now().timestamp()))[-4:]}"
        else:
            doc_type = "GL"
            timestamp_code = str(int(datetime.now().timestamp()))[-6:]
            doc_number = f"GL-{datetime.now().strftime('%Y')}-{timestamp_code}"

        doc_ref = supabase.table("doctors").select("id").eq("is_active", True).limit(1).execute()
        doctor_id = doc_ref.data[0]['id'] if doc_ref.data else None

        visit_data = {
            "patient_id": patient_id,
            "doctor_id": doctor_id,
            "visit_type": payload.visit_type,
            "status": "ADMITTED",
            "queue_number": doc_number[-4:],
            "payment_method": payload.insurance_type
        }
        
        try:
            visit_res = supabase.table("visits").insert(visit_data).execute()
            visit_id = visit_res.data[0]['id']
        except Exception:
            visit_id = "temp_visit_id"

        supabase.table("patient_insurances").update({"sep_no": doc_number}).eq("patient_id", patient_id).execute()
        print(f"   ✅ {doc_type} Created: {doc_number}")
        return SEPResponse(doc_number=doc_number, doc_type=doc_type, date=datetime.now().strftime("%Y-%m-%d"), visit_id=str(visit_id))
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/grouper", response_model=SimulationResponse)
def calculate_benefits(payload: GrouperRequest):
    print(f"\n🧮 [CALC] Processing Doc: {payload.doc_number}")
    try:
        pat_res = supabase.table("patient_insurances").select("patient_id, class_id, insurances(*)").eq("sep_no", payload.doc_number).limit(1).execute()
        
        if not pat_res.data:
            raise HTTPException(status_code=404, detail="Dokumen aktif tidak ditemukan")
            
        insurance_data = pat_res.data[0]['insurances']
        patient_id = pat_res.data[0]['patient_id']
        
        raw_type = insurance_data.get('type')
        normalized_type = 'PRIVATE'
        if raw_type and raw_type.upper() in ['GOVERNMENT', 'BPJS', 'JKN', 'GOOVERNMENT']:
            normalized_type = 'GOVERNMENT'
        if 'BPJS' in insurance_data.get('name', '').upper():
            normalized_type = 'GOVERNMENT'

        # Bill Logic
        total_bill = 0.0
        bill_items = []
        inv_res = supabase.table("invoices").select("id, total_amount").eq("patient_id", patient_id).order("created_at", desc=True).limit(1).execute()
        
        real_invoice_found = False
        
        if inv_res.data:
            invoice = inv_res.data[0]
            details = supabase.table("invoice_details").select("*").eq("invoice_id", invoice['id']).execute()
            
            if details.data:
                real_invoice_found = True
                for item in details.data:
                    cost = float(item['subtotal'])
                    total_bill += cost
                    bill_items.append(BillItem(name=item['item_name'], category=item['item_type'], amount=cost))
            elif invoice.get('total_amount') and float(invoice['total_amount']) > 0:
                real_invoice_found = True
                total_bill = float(invoice['total_amount'])
                bill_items.append(BillItem(name="Total Invoice (Header)", category="system", amount=total_bill))

        # Simulation Prices
        sim_diag_price = 0.0
        sim_proc_price = 0.0
        diag_name = "Unknown"
        
        t10 = supabase.table("tariff_icd10").select("price, name").eq("code", payload.icd10_code).limit(1).execute()
        if t10.data: 
            sim_diag_price = float(t10.data[0]['price'])
            diag_name = t10.data[0]['name']
        
        if payload.icd9_code:
            t9 = supabase.table("tariff_icd9").select("price").eq("code", payload.icd9_code).limit(1).execute()
            if t9.data: sim_proc_price = float(t9.data[0]['price'])

        if normalized_type == 'GOVERNMENT':
            print("   🏥 Mode: INA-CBG (Government)")
            group_code = "UNSPECIFIED"
            
            try:
                map_res = supabase.table("ref_medical_codes").select("target_inacbg_code").eq("code", payload.icd10_code).limit(1).execute()
                if map_res.data: group_code = map_res.data[0]['target_inacbg_code']
            except Exception: pass

            severity = "I"
            desc = diag_name

            if payload.icd9_code: severity = "II"
            if len(payload.secondary_icd10) > 0:
                severity = "III" if severity == "II" else "II"
                sim_diag_price += (sim_diag_price * 0.2 * len(payload.secondary_icd10))

            if payload.birth_weight > 0 and payload.birth_weight < 2500:
                group_code = "P-8-XX"
                desc = f"Neonatal <2500g ({desc})"
                sim_diag_price *= 1.5

            raw_tariff = sim_diag_price + sim_proc_price
            
            class_multiplier = 1.0
            if payload.class_level == 2: class_multiplier = 1.2
            elif payload.class_level == 1: class_multiplier = 1.4
            
            final_tariff = raw_tariff * class_multiplier
            
            is_aps = payload.discharge_status == "APS"
            covered = final_tariff
            excess = 0.0
            warning = False
            desc_suffix = ""
            
            j_sarana = final_tariff * 0.56
            j_pelayanan = final_tariff * 0.44

            if not real_invoice_found or total_bill == 0:
                total_bill = final_tariff * 0.85
                bill_items.append(BillItem(name="Estimasi Biaya RS (Simulasi)", category="system", amount=total_bill))

            if is_aps:
                warning = True
                desc_suffix = " (GUGUR KLAIM - APS)"
                covered = 0.0
                excess = total_bill 
                
            return SimulationResponse(
                simulation_type="INA-CBG",
                real_bill=total_bill,
                bill_items=bill_items,
                inacbg_code=f"{group_code}-{severity}",
                description=desc + desc_suffix,
                severity=severity,
                tariff=final_tariff,
                hospital_margin=(covered - total_bill) if not is_aps else 0,
                covered_amount=covered,
                patient_excess=excess,
                jasa_sarana=j_sarana,
                jasa_pelayanan=j_pelayanan,
                warning_flag=warning,
                plafon_limit=0, deductible=0
            )
        else:
            print(f"   🛡️ Mode: Private Coverage")
            cov_res = supabase.table("insurance_coverages").select("*").eq("insurance_id", insurance_data['id']).limit(1).execute()
            coverage_pct = 100.0
            plafon = 0.0
            deductible = 0.0
            if cov_res.data:
                rule = cov_res.data[0]
                coverage_pct = float(rule.get('coverage_percentage', 100))
                plafon = float(rule.get('plafon_limit', 0))
                deductible = float(rule.get('deductible', 0))

            if not real_invoice_found or total_bill == 0:
                total_bill = sim_diag_price + sim_proc_price
                if len(payload.secondary_icd10) > 0:
                    total_bill += (sim_diag_price * 0.3 * len(payload.secondary_icd10))
                if total_bill < 500000: total_bill += 500000
                if payload.class_level <= 3 and total_bill < 2000000:
                     total_bill = 2000000 + total_bill 
                bill_items = [BillItem(name="Simulasi Biaya Medis", category="system", amount=total_bill)]

            bill_after_deductible = max(0, total_bill - deductible)
            initial_covered = bill_after_deductible * (coverage_pct / 100.0)
            final_covered = initial_covered
            if plafon > 0: final_covered = min(initial_covered, plafon)
            patient_pay = total_bill - final_covered

            return SimulationResponse(
                simulation_type="PRIVATE_COVERAGE",
                real_bill=total_bill,
                bill_items=bill_items,
                description=f"Coverage: {coverage_pct}% | Limit: {plafon:,.0f}",
                covered_amount=final_covered,
                patient_excess=patient_pay,
                plafon_limit=plafon,
                deductible=deductible,
                description_suffix=""
            )
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/bill-details/{card_number}", response_model=AutoFillResponse)
def get_bill_details(card_number: str):
    print(f"\n🔍 [AUTOFILL] Checking invoice for: {card_number}")
    try:
        # First try to resolve card_number if it's actually an MR or Name
        search_card = card_number
        p_res = supabase.table("patients").select("id").eq("mr_no", card_number).execute()
        if p_res.data:
            patient_id = p_res.data[0]['id']
        else:
            p_res = supabase.table("patients").select("id").ilike("full_name", f"%{card_number}%").execute()
            if p_res.data: patient_id = p_res.data[0]['id']
            else:
                 # Assume it's a card number
                pat_query = supabase.table("patient_insurances").select("patient_id").eq("card_number", card_number).limit(1).execute()
                if not pat_query.data: return AutoFillResponse(found=False)
                patient_id = pat_query.data[0]['patient_id']

        inv_query = supabase.table("invoices").select("id").eq("patient_id", patient_id).order("created_at", desc=True).limit(1).execute()
        if not inv_query.data: return AutoFillResponse(found=False)
        invoice_id = inv_query.data[0]['id']

        details = supabase.table("invoice_details").select("item_type, item_code, item_name").eq("invoice_id", invoice_id).execute()
        
        icd10 = None
        icd9 = None
        
        for item in details.data:
            t = item.get("item_type")
            c = item.get("item_code")
            if t == 'icd10' and not icd10: icd10 = c
            if t == 'icd9' and not icd9: icd9 = c
        
        print(f"   ✅ Invoice Found: {invoice_id} | ICD10: {icd10} | ICD9: {icd9}")
        return AutoFillResponse(found=True, icd10=icd10, icd9=icd9, invoice_id=invoice_id)
    except Exception as e:
        print(f"Error: {e}")
        return AutoFillResponse(found=False)

@app.get("/api/references")
def get_references():
    try:
        icd10 = supabase.table("tariff_icd10").select("code, name").execute()
        icd9 = supabase.table("tariff_icd9").select("code, name").execute()
        return {"icd10": icd10.data or [], "icd9": icd9.data or []}
    except Exception as e:
        return {"icd10": [], "icd9": []}

# --- NEW: Patient Search Endpoint ---
@app.get("/api/patients/search")
def search_patients(query: str):
    try:
        # Search by Name or MR No in 'patients' table
        pat_res = supabase.table("patients").select("id, full_name, mr_no").or_(f"full_name.ilike.%{query}%,mr_no.ilike.%{query}%").limit(3).execute()
        
        results = []
        if pat_res.data:
            for p in pat_res.data:
                card_res = supabase.table("patient_insurances").select("card_number, insurances(name)").eq("patient_id", p['id']).limit(1).execute()
                
                card_no = "-"
                ins_name = "-"
                if card_res.data:
                    card_no = card_res.data[0]['card_number']
                    ins_name = card_res.data[0]['insurances']['name'] if card_res.data[0]['insurances'] else "Unknown"
                
                results.append({
                    "name": p['full_name'],
                    "mr_no": p['mr_no'],
                    "card_number": card_no,
                    "insurance": ins_name
                })
        
        if len(results) < 3:
            card_search = supabase.table("patient_insurances").select("card_number, patients(full_name, mr_no), insurances(name)").ilike("card_number", f"%{query}%").limit(3 - len(results)).execute()
            if card_search.data:
                for c in card_search.data:
                    if any(r['card_number'] == c['card_number'] for r in results): continue
                    p = c.get('patients') or {}
                    i = c.get('insurances') or {}
                    results.append({
                        "name": p.get('full_name', 'Unknown'),
                        "mr_no": p.get('mr_no', '-'),
                        "card_number": c['card_number'],
                        "insurance": i.get('name', 'Unknown')
                    })

        return results
    except Exception as e:
        print(f"Search Error: {e}")
        return []

if __name__ == "__main__":
    print("🚀 Starting Multi-Payer Backend on Port 8000...")
    uvicorn.run(app, host="0.0.0.0", port=8000)