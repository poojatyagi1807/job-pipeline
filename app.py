import streamlit as st
import requests
import json
import time
import io
from datetime import datetime
import openpyxl
from openpyxl.styles import Font, PatternFill
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

st.set_page_config(page_title="Daily Job Pipeline", page_icon="🚀", layout="wide")

APIFY_BASE = "https://api.apify.com/v2"
HUNTER_BASE = "https://api.hunter.io/v2"
CLAUDE_API = "https://api.anthropic.com/v1/messages"

SEARCH_QUERIES = [
    "Senior Product Manager Authentication Identity Security AI enterprise remote 2026",
    "Senior Product Manager API Platform LLM Generative AI B2B SaaS 2026",
    "Director Product Manager Enterprise AI Security Compliance 2026",
    "Senior PM Fintech AI Products Platform enterprise 2026",
]

# ── Bulletproof ASCII cleaner ──────────────────────────────────────

def clean(val):
    if val is None:
        return ""
    if not isinstance(val, str):
        try:
            val = str(val)
        except Exception:
            return ""
    return "".join(c for c in val if ord(c) < 128).strip()

def deep_clean(obj):
    if isinstance(obj, str):
        return clean(obj)
    if isinstance(obj, dict):
        return {k: deep_clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_clean(i) for i in obj]
    return obj

def s(val, limit=None):
    r = clean(str(val) if val is not None else "")
    return r[:limit] if limit else r

# ── API helpers ────────────────────────────────────────────────────

def run_apify(actor, input_data, apify_key, timeout=300):
    # Fix: replace / with ~ in actor ID for URL
    actor_url_id = actor.replace("/", "~")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + apify_key
    }
    resp = requests.post(
        APIFY_BASE + "/acts/" + actor_url_id + "/runs",
        headers=headers,
        json=input_data,
        timeout=30
    )
    resp.raise_for_status()
    run_id = resp.json()["data"]["id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        sr = requests.get(APIFY_BASE + "/actor-runs/" + run_id, headers=headers, timeout=15)
        data = sr.json()["data"]
        status = data["status"]
        if status == "SUCCEEDED":
            dataset_id = data["defaultDatasetId"]
            items_resp = requests.get(
                APIFY_BASE + "/datasets/" + dataset_id + "/items?limit=100",
                headers=headers, timeout=30
            )
            raw = items_resp.json()
            return [deep_clean(item) for item in raw]
        if status in ("FAILED", "ABORTED"):
            raise RuntimeError("Apify run " + status)
    raise TimeoutError("Apify run timed out")

def call_claude(system, user, api_key):
    # Use requests directly — avoids Anthropic library encoding issues
    clean_system = clean(system)
    clean_user = clean(user)
    resp = requests.post(
        CLAUDE_API,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "system": clean_system,
            "messages": [{"role": "user", "content": clean_user}]
        },
        timeout=60
    )
    resp.raise_for_status()
    result = resp.json()["content"][0]["text"]
    return clean(result)

def parse_json_safe(text):
    try:
        return json.loads(text.replace("```json","").replace("```","").strip())
    except Exception:
        return None

def hunter_find_email(first, last, company, hunter_key):
    if not hunter_key or not first or not last:
        return None
    try:
        resp = requests.get(
            HUNTER_BASE + "/email-finder",
            params={"company": company, "first_name": first, "last_name": last, "api_key": hunter_key},
            timeout=10
        )
        data = resp.json().get("data", {})
        if data.get("email"):
            score = data.get("score", 0)
            return {"email": clean(data["email"]), "score": score, "verified": score > 70}
    except Exception:
        pass
    return None

def parse_name(full_name):
    parts = clean(full_name or "").split()
    return (parts[0] if parts else ""), (" ".join(parts[1:]) if len(parts) > 1 else "")

def get_secret(key, env_key):
    if st.session_state.get(key):
        return st.session_state[key]
    try:
        return st.secrets[env_key]
    except Exception:
        return os.environ.get(env_key, "")

# ── Pipeline stages ────────────────────────────────────────────────

def stage_discover(apify_key, claude_key, msg):
    msg.info("Stage 1/7: Searching job boards...")
    all_results = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(run_apify, "apify/rag-web-browser", {"query": q, "maxResults": 5}, apify_key, 180): i
            for i, q in enumerate(SEARCH_QUERIES)
        }
        for i, future in enumerate(as_completed(futures)):
            msg.info("Stage 1/7: Scraped " + str(i+1) + " of " + str(len(SEARCH_QUERIES)) + " queries...")
            try:
                all_results.extend(future.result() or [])
            except Exception as e:
                st.warning("Query " + str(i+1) + " failed: " + s(e))

    parts = []
    for r in all_results:
        parts.append("SOURCE: " + s(r.get("url",""), 200) + "\nTITLE: " + s(r.get("title",""), 100) + "\nCONTENT: " + s(r.get("text",""), 500))
    content = "\n---\n".join(parts)

    msg.info("Stage 1/7: AI extracting job listings...")
    raw = call_claude(
        "Extract job listings from web search results. Return ONLY valid JSON array, nothing else.",
        'Extract all PM job listings. Return array: [{"title":"","company":"","url":"","description":"","location":"","postedDate":""}]\n\nContent:\n' + content[:8000],
        claude_key
    )
    jobs = parse_json_safe(raw)
    return [deep_clean(j) for j in (jobs or []) if j.get("title") and j.get("company")]


def stage_score(jobs, resume, claude_key, msg):
    msg.info("Stage 2/7: Scoring " + str(len(jobs)) + " jobs...")
    clean_resume = s(resume, 1200)

    def score_job(j):
        raw = call_claude(
            "Score job match. Return ONLY valid JSON, nothing else.",
            "Resume: " + clean_resume + "\n\nJob: " + s(j.get("title","")) + " at " + s(j.get("company","")) + "\nDesc: " + s(j.get("description",""), 700) +
            '\n\nReturn: {"score":8.5,"domain":9,"seniority":8,"technical":8,"ai_relevance":9,"match_reason":"2 sentences","gap":"1 sentence or none","competition":"low/medium/high","sponsorship":"confirmed/likely/unknown/no","angle":"what to emphasize"}',
            claude_key
        )
        parsed = parse_json_safe(raw)
        return deep_clean({**j, **(parsed or {"score": 0})})

    with ThreadPoolExecutor(max_workers=4) as ex:
        scored = list(ex.map(score_job, jobs[:20]))

    return sorted([j for j in scored if j.get("score", 0) >= 7.5], key=lambda x: x.get("score", 0), reverse=True)[:8]


def stage_contacts(jobs, apify_key, claude_key, msg):
    msg.info("Stage 3/7: Finding decision makers...")
    result_jobs = []
    for i, job in enumerate(jobs):
        company = s(job.get("company",""))
        msg.info("Stage 3/7: Contacts at " + company + " (" + str(i+1) + "/" + str(len(jobs)) + ")...")
        try:
            results = run_apify("apify/rag-web-browser",
                {"query": '"' + company + '" "VP Product" OR "Director Product" OR "Technical Recruiter" site:linkedin.com', "maxResults": 5},
                apify_key, 120)
            content = "\n".join([s(r.get("title",""), 100) + " | " + s(r.get("url",""), 200) for r in (results or [])])
            raw = call_claude(
                "Extract LinkedIn contacts. Return ONLY valid JSON array.",
                'Find people at ' + company + '. Return: [{"name":"Full Name","title":"","linkedInUrl":"","priority":"high/medium/low"}]\nPriority: VP/Director/Head=high, Recruiter=medium, PM=low\n\nResults:\n' + content,
                claude_key
            )
            contacts = parse_json_safe(raw)
            result_jobs.append({**job, "contacts": [deep_clean(c) for c in (contacts or [])[:5]]})
        except Exception as e:
            result_jobs.append({**job, "contacts": []})
    return result_jobs


def stage_enrich_emails(jobs, hunter_key, msg):
    if not hunter_key:
        msg.warning("Stage 4/7: No Hunter.io key — skipping")
        time.sleep(1)
        return jobs
    msg.info("Stage 4/7: Finding emails via Hunter.io...")
    found = total = 0
    enriched = []
    for job in jobs:
        company = s(job.get("company",""))
        contacts = []
        for c in job.get("contacts", []):
            total += 1
            first, last = parse_name(c.get("name",""))
            result = hunter_find_email(first, last, company, hunter_key)
            if result:
                found += 1
            msg.info("Stage 4/7: Hunter.io — " + str(found) + " of " + str(total) + " found...")
            contacts.append({**c, "email": result["email"] if result else None,
                "email_score": result["score"] if result else None,
                "email_verified": result["verified"] if result else False})
        enriched.append({**job, "contacts": contacts})
    msg.success("Stage 4/7: Done — " + str(found) + " emails found")
    return enriched


def stage_drafts(jobs, resume, claude_key, msg):
    msg.info("Stage 5/7: Writing outreach emails...")
    clean_resume = s(resume, 350)
    result = []
    for job in jobs:
        title = s(job.get("title",""))
        company = s(job.get("company",""))
        contacts = []
        for c in job.get("contacts", []):
            try:
                draft = call_claude(
                    "Write a short cold outreach email. No hyphens. No bullet points. Max 4 sentences. Human and specific. Return email body only.",
                    "Sender: " + clean_resume + "\nTo: " + s(c.get("name","")) + ", " + s(c.get("title","")) + " at " + company + "\nApplying for: " + title + "\n\nWrite email body:",
                    claude_key
                )
                contacts.append({**c, "email_draft": draft})
            except Exception:
                contacts.append({**c, "email_draft": ""})
        result.append({**job, "contacts": contacts})
    return result


def stage_resumes(jobs, msg):
    msg.info("Stage 6/7: Generating Resume Tailor prompts...")
    result = []
    for job in jobs:
        prompt = ("Tailor my resume for this role.\n\n"
                 "Company: " + s(job.get("company","")) + "\n"
                 "Role: " + s(job.get("title","")) + "\n"
                 "Apply Link: " + s(job.get("url","")) + "\n\n"
                 "Job Description:\n" + s(job.get("description",""), 2000) + "\n\n"
                 "Angle to emphasize: " + s(job.get("angle","")) + "\n"
                 "Gap to address: " + s(job.get("gap","")) + "\n\n"
                 "Instructions:\n"
                 "- Match keywords until score is above 90%\n"
                 "- Rewrite bullets using their exact language\n"
                 "- Flag any ATS keyword gaps\n"
                 "- Tell me what to add if new gap found\n"
                 "- Update memory with new instructions")
        result.append({**job, "resume_tailor_prompt": prompt})
    return result


def generate_excel(jobs):
    wb = openpyxl.Workbook()
    today = datetime.now().strftime("%Y-%m-%d")
    hf = Font(bold=True, color="FFFFFF")
    hfill = PatternFill("solid", fgColor="1a73e8")

    ws1 = wb.active
    ws1.title = "Jobs"
    ws1.append(["Date","Rank","Company","Role","Score","Competition","Sponsorship","Why Match","Gap","Angle","Apply Link","Status","Notes"])
    for cell in ws1[1]:
        cell.font = hf
        cell.fill = hfill
    for i, j in enumerate(jobs):
        ws1.append([today, i+1, s(j.get("company","")), s(j.get("title","")),
            j.get("score",""), s(j.get("competition","")), s(j.get("sponsorship","")),
            s(j.get("match_reason","")), s(j.get("gap","")), s(j.get("angle","")),
            s(j.get("url","")), "Not Applied", ""])

    ws2 = wb.create_sheet("Contacts and Emails")
    ws2.append(["Date","Company","Role","Name","Title","Email","Confidence","Verified","LinkedIn","Priority","Draft","Sent?"])
    for cell in ws2[1]:
        cell.font = hf
        cell.fill = hfill
    for j in jobs:
        for c in j.get("contacts", []):
            score = c.get("email_score")
            ws2.append([today, s(j.get("company","")), s(j.get("title","")),
                s(c.get("name","")), s(c.get("title","")),
                s(c.get("email","Not found")),
                (str(score) + "%") if score else "",
                "Yes" if c.get("email_verified") else "",
                s(c.get("linkedInUrl","")), s(c.get("priority","")),
                s(c.get("email_draft","")), "No"])

    ws3 = wb.create_sheet("Resume Tailor Prompts")
    ws3.append(["Company","Role","Paste Into Resume Tailor Project"])
    for cell in ws3[1]:
        cell.font = hf
        cell.fill = hfill
    for j in jobs:
        ws3.append([s(j.get("company","")), s(j.get("title","")), s(j.get("resume_tailor_prompt",""))])

    for ws in [ws1, ws2, ws3]:
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = 28

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


# ── Main UI ────────────────────────────────────────────────────────

def main():
    st.title("Daily Job Pipeline")
    st.caption(datetime.now().strftime("%A, %B %d %Y") + " · Goal: 10 interviews in 30 days")

    with st.sidebar:
        st.header("Configuration")
        apify_key = st.text_input("Apify API Key", type="password", value=get_secret("apify_key","APIFY_KEY"))
        claude_key = st.text_input("Claude API Key", type="password", value=get_secret("claude_key","ANTHROPIC_API_KEY"), help="console.anthropic.com")
        hunter_key = st.text_input("Hunter.io Key (optional)", type="password", value=get_secret("hunter_key","HUNTER_KEY"))
        master_resume = st.text_area("Master Resume", value=st.session_state.get("master_resume",""), height=200)

        if st.button("Save Configuration", use_container_width=True):
            st.session_state["apify_key"] = apify_key
            st.session_state["claude_key"] = claude_key
            st.session_state["hunter_key"] = hunter_key
            st.session_state["master_resume"] = master_resume
            st.success("Saved!")

        if st.session_state.get("excel_bytes"):
            st.divider()
            st.success("Pipeline complete!")
            st.download_button("Download Excel Now",
                data=st.session_state["excel_bytes"],
                file_name="JobPipeline_" + datetime.now().strftime("%Y-%m-%d") + ".xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True, key="sidebar_dl")

        st.divider()
        st.caption("Claude API key: console.anthropic.com")
        st.caption("Hunter.io free: 25 emails/month")
        st.caption("Pipeline takes 10 to 15 minutes")

    ak = st.session_state.get("apify_key","")
    ck = st.session_state.get("claude_key","")
    hk = st.session_state.get("hunter_key","")
    resume = st.session_state.get("master_resume","")

    if not ak or not ck or not resume:
        st.warning("Add your Apify key, Claude API key, and resume in the sidebar. Then click Save Configuration.")
        return

    col1, col2 = st.columns([3,1])
    with col1:
        st.subheader("Pipeline Stages")
    with col2:
        run_clicked = st.button("Run Pipeline", type="primary", use_container_width=True)

    stage_ids   = ["discover","score","contacts","emails","drafts","resume","export"]
    stage_names = ["Discover jobs","Score matches","Find decision makers","Enrich emails","Write outreach","Resume Tailor prompts","Generate spreadsheet"]

    done   = st.session_state.get("done_stages",[])
    active = st.session_state.get("active_stage",None)
    cols = st.columns(4)
    for i,(sid,name) in enumerate(zip(stage_ids,stage_names)):
        with cols[i%4]:
            if sid in done:   st.success(name)
            elif sid==active: st.info(name + "...")
            else:             st.markdown("<span style='color:#999;font-size:13px'>" + name + "</span>", unsafe_allow_html=True)

    msg = st.empty()

    if run_clicked:
        st.session_state.update({"done_stages":[],"active_stage":None,"pipeline_results":None,"excel_bytes":None})

        def mark(sid):
            st.session_state["done_stages"] = st.session_state.get("done_stages",[]) + [sid]

        try:
            st.session_state["active_stage"] = "discover"
            j1 = stage_discover(ak, ck, msg)
            mark("discover")
            msg.success("Found " + str(len(j1)) + " job listings")

            st.session_state["active_stage"] = "score"
            j2 = stage_score(j1, resume, ck, msg)
            mark("score")
            msg.success(str(len(j2)) + " strong matches (score 7.5+)")

            st.session_state["active_stage"] = "contacts"
            j3 = stage_contacts(j2, ak, ck, msg)
            mark("contacts")

            st.session_state["active_stage"] = "emails"
            j4 = stage_enrich_emails(j3, hk, msg)
            mark("emails")

            st.session_state["active_stage"] = "drafts"
            j5 = stage_drafts(j4, resume, ck, msg)
            mark("drafts")

            st.session_state["active_stage"] = "resume"
            j6 = stage_resumes(j5, msg)
            mark("resume")

            st.session_state["active_stage"] = "export"
            msg.info("Stage 7/7: Building spreadsheet...")
            j6 = [deep_clean(j) for j in j6]
            excel = generate_excel(j6)
            mark("export")

            st.session_state.update({"active_stage":None,"pipeline_results":j6,"excel_bytes":excel})
            email_n = sum(1 for j in j6 for c in j.get("contacts",[]) if c.get("email"))
            msg.success("Done! " + str(len(j6)) + " jobs. " + str(email_n) + " emails. Download in sidebar.")
            st.balloons()

        except Exception as e:
            stage = st.session_state.get("active_stage","unknown")
            msg.error("Error at stage [" + str(stage) + "]: " + str(e))
            st.session_state["active_stage"] = None

    results = st.session_state.get("pipeline_results")
    if results:
        st.divider()
        email_n = sum(1 for j in results for c in j.get("contacts",[]) if c.get("email"))
        st.subheader(str(len(results)) + " Matched Jobs - " + str(email_n) + " Direct Emails Found")

        tabs = st.tabs([s(j.get("company","?")) + " (" + str(round(j.get("score",0),1)) + ")" for j in results])
        for tab, job in zip(tabs, results):
            with tab:
                c1,c2,c3 = st.columns(3)
                c1.metric("Match Score", str(round(job.get("score",0),1)) + "/10")
                c2.metric("Competition", s(job.get("competition","")).capitalize())
                c3.metric("Sponsorship", s(job.get("sponsorship","")).capitalize())
                if job.get("url"): st.markdown("[Apply Now](" + s(job["url"]) + ")")
                if job.get("match_reason"): st.success("Why you match: " + s(job["match_reason"]))
                if job.get("gap") and s(job["gap"]).lower() not in ("none",""): st.warning("Address in cover note: " + s(job["gap"]))
                if job.get("resume_tailor_prompt"):
                    with st.expander("Resume Tailor Prompt - copy into your Resume Tailor project"):
                        st.text_area("", value=s(job["resume_tailor_prompt"]), height=200,
                            key="rtp_" + str(results.index(job)), label_visibility="collapsed")

                st.subheader("Decision Makers (" + str(len(job.get("contacts",[]))) + ")")
                for ci, c in enumerate(job.get("contacts",[])):
                    priority = s(c.get("priority",""))
                    icon = "High" if priority=="high" else "Med" if priority=="medium" else "Low"
                    with st.expander("[" + icon + "] " + s(c.get("name","")) + " - " + s(c.get("title",""))):
                        if c.get("email"):
                            st.markdown("**Email:** `" + s(c["email"]) + "` (" + str(c.get("email_score","")) + "% confidence" + (" Verified" if c.get("email_verified") else "") + ")")
                        else:
                            st.caption("Email not found - use LinkedIn")
                        if c.get("linkedInUrl"): st.markdown("[LinkedIn](" + s(c["linkedInUrl"]) + ")")
                        if c.get("email_draft"):
                            st.text_area("Outreach draft:", value=s(c["email_draft"]), height=120,
                                key="d_" + str(results.index(job)) + "_" + str(ci))

if __name__ == "__main__":
    main()
