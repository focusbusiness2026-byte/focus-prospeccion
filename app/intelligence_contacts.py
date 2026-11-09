"""Map the real research contact schema without inventing a decision maker."""
import json

def array(value):
    try:parsed=json.loads(value or "[]") if isinstance(value,str) else value
    except (ValueError,TypeError):return []
    return [item for item in parsed if isinstance(item,dict)] if isinstance(parsed,list) else []

def prospect_contact(p):
    people=array(p.get("public_contacts_json"));decisors=array(p.get("decision_makers_json"))
    current=next((d for d in decisors if d.get("role_status")=="current" and d.get("name") and str(d.get("source_url") or "").startswith(("https://","http://"))),{})
    email=next((str(x.get("email") or x.get("value") or "") for x in people if (x.get("type")=="email" or x.get("email")) and "@" in str(x.get("email") or x.get("value") or "")),"")
    phone=next((str(x.get("value") or x.get("phone") or "") for x in people if x.get("type")=="phone" or x.get("phone")),"")
    person_link=str(current.get("public_contact") or "")
    linkedin=person_link if person_link.startswith("https://") and "linkedin.com/in/" in person_link else p.get("linkedin","")
    return {"id":str(p["execution_id"]),"company":p.get("company",""),"website":p.get("website",""),
        "name":current.get("name",""),"role":current.get("role",""),"person_source_url":current.get("source_url",""),
        "name_status":"current_with_source" if current else "not_publicly_verified","email":email,"phone":phone,
        "email_options":[{"email":str(x.get("email") or x.get("value")),"source_url":str(x.get("source_url") or "")} for x in people if (x.get("type")=="email" or x.get("email")) and "@" in str(x.get("email") or x.get("value") or "")],
        "linkedin":linkedin,"sector":p.get("sector",""),"city":p.get("city",""),"summary":p.get("summary",""),
        "research_dossier_json":p.get("research_dossier_json",""),"approved":p.get("lead_status")=="Aprobado para descarga",
        "source":"Copia privada de investigación aprobada de Three Dots Studio; cargos actuales solo con fuente"}
