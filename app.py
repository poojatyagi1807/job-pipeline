import streamlit as st
import anthropic
import requests
import json
import time
import io
import re
import unicodedata
from datetime import datetime
import openpyxl
from openpyxl.styles import Font, PatternFill
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

st.set_page_config(page_title="Daily Job Pipeline", page_icon="🚀", layout="wide")

APIFY_BASE = "https://api.apify.com/v2"
HUNTER_BASE = "https://api.hunter.io/v2"

SEARCH_QUERIES = [
    "Senior Product Manager Authentication Identity Security AI enterprise remote 2026",
    "Senior Product Manager API Platform LLM Generative AI B2B SaaS 2026",
    "Director Product Manager Enterprise AI Security Compliance Platform 2026",
    "Principal Product Manager Platform Identity Security Cloud 2026",
]

# ── Bulletproof text cleaner ───────────────────────────────────────

def clean(val):
    """Remove ALL non-ASCII characters. Foolproof."""
    if val is None:
        return ""
    if not isinstance(val, str):
        try:
            val = str(val)
        except Exception:
            return ""
    # Keep only characters with ordinal < 128
    return "".join(c for c in val if ord(c) < 128).strip()

def deep_clean(obj):
    """Recursively clean every string in dicts and lists."""
    if isinstance(obj, str):
        return clean(obj)
    if isinstance(obj, dict):
        return {k: deep_clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_clean(i) for i in obj]
    return obj

def safe_str(val, limit=None):
    """Convert to clean ASCII string, optionally truncated."""
    result = clean(str(val) if val is not None else "")
    if limit:
        result = result[:limit]
    return result

# ── API helpers ────────────────────────────────────────────────────

def run_apify(actor, input_data, apify_key, timeout=300):
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + apify_key
    }
    resp = requests.post(
        APIFY_BASE + "/acts/" + actor + "/runs",
        headers=headers,
        json=input_data,
        timeout=30
    )
    resp.raise_for_status()
    run_id = resp.json()["data"]["id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        sr = requests.get(
            APIFY_BASE + "/actor-runs/" + run_id,
            headers=headers,
            timeout=15
        )
        data = sr.json()["data"]
        status = data["status"]
        if status == "SUCCEEDED":
            dataset_id = data["defaultDatasetId"]
            items = requests.get(
                APIFY_BASE + "/datasets/" + dataset_id + "/items?limit=100",
                headers=headers,
                timeout=30
            )
            raw_items = items.json()
            # Deep clean all scraped data immediately
            return [deep_clean(item) for item in raw_items]
        if status in ("FAILED", "ABORTED"):
            raise RuntimeError("Apify run " + status)
    raise TimeoutError("Apify run timed out")

def call_claude(system, user, api_key):
    # Clean inputs before sending
    system = clean(system)
    user = clean(user)
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    result = msg.content[0].text
    return clean(result)

def parse_json_safe(text):
    try:
        cleaned = text.replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned)
    except Exception:
        return None

def hunter_find_email(first, last, company, hunter_key):
    if not hunter_key or not first or not last:
        return None
    try:
        resp = requests.get(
            HUNTER_BASE + "/email-finder",
            params={
                "company": company,
                "first_name": first,
                "last_name": last,
                "api_key": hunter_key
            },
            timeout=10
        )
        data = resp.json()
        email = data.get("data", {}).get("email")
        if email:
            score = data["data"].get("score", 0)
            return {"email": clean(email), "score": score, "verified": score > 70}
    except Exception:
        pass
    return None

def parse_name(full_name):
    parts = clean(full_name or "").split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""
    return first, last

def get_secret(key, env_key):
    if st.session_state.get(key):
        return st.session_state[key]
    try:
        return st.secrets[env_key]
    except Exception:
        return os.environ.get(env_key, "")

# ── Pipeline stages ────────────────────────────────────────────────

def stage_discover(apify_key, claude_key, status_placeholder):
    status_placeholder.info("Stage 1/7: Searching job boards...")
    all_results = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(run_apify, "apify/rag-web-browser", {"query": q, "maxResults": 5}, apify_key, 180): i
            for i, q in enumerate(SEARCH_QUERIES[:4])
        }
        for i, future in enumerate(as_completed(futures)):
            status_placeholder.info("Stage 1/7: Scraped " + str(i+1) + " of 4 queries...")
            try:
                results = future.result() or []
                all_results.extend(results)
            except Exception as e:
                st.warning("Query failed: " + str(e))

    # Build content string from already-cleaned results
    parts = []
    for r in all_results:
        url = safe_str(r.get("url",""), 200)
        title = safe_str(r.get("title",""), 100)
        text = safe_str(r.get("text",""), 500)
        parts.append("SOURCE: " + url + "\nTITLE: " + title + "\nCONTENT: " + text)
    content = "\n---\n".join(parts)

    status_placeholder.info("Stage 1/7: AI extracting job listings...")
    system = "Extract job listings from web search results. Return ONLY valid JSON array, nothing else."
    user = ('Extract all PM job listings. Return array: '
            '[{"title":"","company":"","url":"","description":"","location":"","postedDate":""}]'
            "\n\nContent:\n" + content[:8000])

    raw = call_claude(system, user, claude_key)
    jobs = parse_json_safe(raw)
    if not isinstance(jobs, list):
        return []
    result = [deep_clean(j) for j in jobs if j.get("title") and j.get("company")]
    return result


def stage_score(jobs, resume, claude_key, status_placeholder):
    status_placeholder.info("Stage 2/7: Scoring " + str(len(jobs)) + " jobs...")
    clean_resume = safe_str(resume, 1200)

    def score_job(j):
        title = safe_str(j.get("title",""))
        company = safe_str(j.get("company",""))
        desc = safe_str(j.get("description",""), 700)
        system = "Score job match. Return ONLY valid JSON, nothing else."
        user = ("Resume: " + clean_resume + "\n\nJob: " + title + " at " + company +
                "\nDesc: " + desc +
                '\n\nReturn: {"score":8.5,"domain":9,"seniority":8,"technical":8,'
                '"ai_relevance":9,"match_reason":"2 sentences","gap":"1 sentence or none",'
                '"competition":"low/medium/high","sponsorship":"confirmed/likely/unknown/no",'
                '"angle":"what to emphasize"}')
        raw = call_claude(system, user, claude_key)
        parsed = parse_json_safe(raw)
        merged = {**j, **(parsed or {"score": 0})}
        return deep_clean(merged)

    scored = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        scored = list(ex.map(score_job, jobs[:20]))

    return sorted(
        [j for j in scored if j.get("score", 0) >= 7.5],
        key=lambda x: x.get("score", 0),
        reverse=True
    )[:8]


def stage_contacts(jobs, apify_key, claude_key, status_placeholder):
    status_placeholder.info("Stage 3/7: Finding decision makers...")
    result_jobs = []
    for i, job in enumerate(jobs):
        company = safe_str(job.get("company",""))
        status_placeholder.info("Stage 3/7: Finding contacts at " + company + " (" + str(i+1) + "/" + str(len(jobs)) + ")...")
        try:
            query = '"' + company + '" "VP Product" OR "Director Product" OR "Head of Product" OR "Technical Recruiter" site:linkedin.com'
            results = run_apify("apify/rag-web-browser", {"query": query, "maxResults": 5}, apify_key, 120)
            lines = []
            for r in (results or []):
                lines.append(safe_str(r.get("title",""), 100) + " | " + safe_str(r.get("url",""), 200))
            content = "\n".join(lines)
            system = "Extract LinkedIn contacts. Return ONLY valid JSON array."
            user = ('Find people at ' + company + '. Return: '
                   '[{"name":"Full Name","title":"","linkedInUrl":"","priority":"high/medium/low"}]\n'
                   'Priority: VP/Director/Head Product=high, Recruiter=medium, PM=low\n\n'
                   'Results:\n' + content)
            raw = call_claude(system, user, claude_key)
            contacts = parse_json_safe(raw)
            cleaned = [deep_clean(c) for c in (contacts or [])[:5]]
            result_jobs.append({**job, "contacts": cleaned})
        except Exception as e:
            result_jobs.append({**job, "contacts": []})
    return result_jobs


def stage_enrich_emails(jobs, hunter_key, status_placeholder):
    if not hunter_key:
        status_placeholder.warning("Stage 4/7: No Hunter.io key — skipping email enrichment")
        time.sleep(1)
        return jobs
    status_placeholder.info("Stage 4/7: Finding direct emails via Hunter.io...")
    found, total = 0, 0
    enriched_jobs = []
    for job in jobs:
        company = safe_str(job.get("company",""))
        enriched_contacts = []
        for contact in job.get("contacts", []):
            total += 1
            first, last = parse_name(contact.get("name", ""))
            result = hunter_find_email(first, last, company, hunter_key)
            if result:
                found += 1
            status_placeholder.info("Stage 4/7: Hunter.io — " + str(found) + " emails found of " + str(total) + " searched...")
            enriched_contacts.append({
                **contact,
                "email": result["email"] if result else None,
                "email_score": result["score"] if result else None,
                "email_verified": result["verified"] if result else False
            })
        enriched_jobs.append({**job, "contacts": enriched_contacts})
    status_placeholder.success("Stage 4/7: Done — " + str(found) + " direct emails found")
    return enriched_jobs


def stage_drafts(jobs, resume, claude_key, status_placeholder):
    status_placeholder.info("Stage 5/7: Writing personalized outreach emails...")
    clean_resume = safe_str(resume, 350)
    result_jobs = []
    for job in jobs:
        title = safe_str(job.get("title",""))
        company = safe_str(job.get("company",""))
        enriched_contacts = []
        for contact in job.get("contacts", []):
            try:
                name = safe_str(contact.get("name",""))
                ctitle = safe_str(contact.get("title",""))
                system = "Write a short cold outreach email. No hyphens. No bullet points. Max 4 sentences. Human and specific. No I am excited to. Return email body only."
                user = ("Sender background: " + clean_resume +
                       "\nTo: " + name + ", " + ctitle + " at " + company +
                       "\nApplying for: " + title + "\n\nWrite email body:")
                draft = call_claude(system, user, claude_key)
                enriched_contacts.append({**contact, "email_draft": draft})
            except Exception:
                enriched_contacts.append({**contact, "email_draft": ""})
        result_jobs.append({**job, "contacts": enriched_contacts})
    return result_jobs


def stage_resumes(jobs, status_placeholder):
    status_placeholder.info("Stage 6/7: Generating Resume Tailor prompts...")
    result_jobs = []
    for job in jobs:
        company = safe_str(job.get("company",""))
        title = safe_str(job.get("title",""))
        url = safe_str(job.get("url",""))
        desc = safe_str(job.get("description",""), 2000)
        angle = safe_str(job.get("angle",""))
        gap = safe_str(job.get("gap",""))
        prompt = ("Tailor my resume for this role.\n\n"
                 "Company: " + company + "\n"
                 "Role: " + title + "\n"
                 "Apply Link: " + url + "\n\n"
                 "Job Description:\n" + (desc or "Not available") + "\n\n"
                 "Key angle to emphasize: " + angle + "\n"
                 "Known gap to address: " + gap + "\n\n"
                 "Instructions:\n"
                 "- Match keywords until score is above 90%\n"
                 "- Rewrite bullets using their exact language\n"
                 "- Flag any ATS keyword gaps\n"
                 "- Tell me what to add if a new gap is found\n"
                 "- Update memory with any new instructions")
        result_jobs.append({**job, "resume_tailor_prompt": prompt})
    return result_jobs


def generate_excel(jobs):
    wb = openpyxl.Workbook()
    today = datetime.now().strftime("%Y-%m-%d")
    hf = Font(bold=True, color="FFFFFF")
    hfill = PatternFill("solid", fgColor="1a73e8")

    ws1 = wb.active
    ws1.title = "Jobs"
    ws1.append(["Date","Rank","Company","Role","Match Score","Competition","Sponsorship","Why Match","Gap","Angle","Apply Link","Status","Notes"])
    for cell in ws1[1]:
        cell.font = hf
        cell.fill = hfill
    for i, j in enumerate(jobs):
        ws1.append([
            today, i+1,
            safe_str(j.get("company","")),
            safe_str(j.get("title","")),
            j.get("score",""),
            safe_str(j.get("competition","")),
            safe_str(j.get("sponsorship","")),
            safe_str(j.get("match_reason","")),
            safe_str(j.get("gap","")),
            safe_str(j.get("angle","")),
            safe_str(j.get("url","")),
            "Not Applied", ""
        ])

    ws2 = wb.create_sheet("Contacts and Emails")
    ws2.append(["Date","Company","Role","Contact Name","Contact Title","Direct Email","Confidence","Verified","LinkedIn","Priority","Email Draft","Sent?"])
    for cell in ws2[1]:
        cell.font = hf
        cell.fill = hfill
    for j in jobs:
        for c in j.get("contacts", []):
            score = c.get("email_score")
            ws2.append([
                today,
                safe_str(j.get("company","")),
                safe_str(j.get("title","")),
                safe_str(c.get("name","")),
                safe_str(c.get("title","")),
                safe_str(c.get("email","Not found")),
                (str(score) + "%") if score else "",
                "Yes" if c.get("email_verified") else "",
                safe_str(c.get("linkedInUrl","")),
                safe_str(c.get("priority","")),
                safe_str(c.get("email_draft","")),
                "No"
            ])

    ws3 = wb.create_sheet("Resume Tailor Prompts")
    ws3.append(["Company","Role","Paste Into Resume Tailor Project"])
    for cell in ws3[1]:
        cell.font = hf
        cell.fill = hfill
    for j in jobs:
        ws3.append([
            safe_str(j.get("company","")),
            safe_str(j.get("title","")),
            safe_str(j.get("resume_tailor_prompt",""))
        ])

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
        apify_key = st.text_input("Apify API Key", type="password",
            value=get_secret("apify_key", "APIFY_KEY"),
            help="apify.com → Settings → API & Integrations")
        claude_key = st.text_input("Claude API Key", type="password",
            value=get_secret("claude_key", "ANTHROPIC_API_KEY"),
            help="console.anthropic.com → API Keys")
        hunter_key = st.text_input("Hunter.io API Key (optional)", type="password",
            value=get_secret("hunter_key", "HUNTER_KEY"),
            help="hunter.io → Dashboard → API Key")
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
            st.download_button(
                "Download Excel Now",
                data=st.session_state["excel_bytes"],
                file_name="JobPipeline_" + datetime.now().strftime("%Y-%m-%d") + ".xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key="sidebar_dl"
            )

        st.divider()
        st.caption("Tips")
        st.caption("Claude API key from console.anthropic.com")
        st.caption("Hunter.io free tier: 25 emails/month")
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

    stages = [
        "Discover jobs", "Score matches", "Find decision makers",
        "Enrich emails", "Write outreach drafts",
        "Generate Resume Tailor prompts", "Generate spreadsheet"
    ]
    stage_ids = ["discover","score","contacts","emails","drafts","resume","export"]

    done = st.session_state.get("done_stages",[])
    active = st.session_state.get("active_stage",None)
    cols = st.columns(4)
    for i, (sid, label) in enumerate(zip(stage_ids, stages)):
        with cols[i % 4]:
            if sid in done:
                st.success(label)
            elif sid == active:
                st.info(label + "...")
            else:
                st.markdown("<span style='color:#999;font-size:13px'>" + label + "</span>", unsafe_allow_html=True)

    status_area = st.empty()

    if run_clicked:
        st.session_state["done_stages"] = []
        st.session_state["active_stage"] = None
        st.session_state["pipeline_results"] = None
        st.session_state["excel_bytes"] = None

        def mark(sid):
            current = st.session_state.get("done_stages",[])
            st.session_state["done_stages"] = current + [sid]

        try:
            st.session_state["active_stage"] = "discover"
            jobs1 = stage_discover(ak, ck, status_area)
            mark("discover")
            status_area.success("Found " + str(len(jobs1)) + " job listings")

            st.session_state["active_stage"] = "score"
            jobs2 = stage_score(jobs1, resume, ck, status_area)
            mark("score")
            status_area.success(str(len(jobs2)) + " strong matches (score 7.5+)")

            st.session_state["active_stage"] = "contacts"
            jobs3 = stage_contacts(jobs2, ak, ck, status_area)
            mark("contacts")

            st.session_state["active_stage"] = "emails"
            jobs4 = stage_enrich_emails(jobs3, hk, status_area)
            mark("emails")

            st.session_state["active_stage"] = "drafts"
            jobs5 = stage_drafts(jobs4, resume, ck, status_area)
            mark("drafts")

            st.session_state["active_stage"] = "resume"
            jobs6 = stage_resumes(jobs5, status_area)
            mark("resume")

            st.session_state["active_stage"] = "export"
            status_area.info("Stage 7/7: Generating spreadsheet...")
            jobs6 = [deep_clean(j) for j in jobs6]
            excel_bytes = generate_excel(jobs6)
            mark("export")

            st.session_state["active_stage"] = None
            st.session_state["pipeline_results"] = jobs6
            st.session_state["excel_bytes"] = excel_bytes

            email_total = sum(1 for j in jobs6 for c in j.get("contacts",[]) if c.get("email"))
            status_area.success(
                "Pipeline complete! " + str(len(jobs6)) + " jobs matched. " +
                str(email_total) + " direct emails found. Download button is in the sidebar."
            )
            st.balloons()

        except Exception as e:
            current_stage = st.session_state.get("active_stage","unknown")
            status_area.error("Pipeline error at stage [" + str(current_stage) + "]: " + str(e))
            st.session_state["active_stage"] = None

    results = st.session_state.get("pipeline_results")
    if results:
        st.divider()
        email_total = sum(1 for j in results for c in j.get("contacts",[]) if c.get("email"))
        st.subheader(str(len(results)) + " Matched Jobs · " + str(email_total) + " Direct Emails Found")

        tab_labels = [j.get("company","?") + " (" + str(round(j.get("score",0),1)) + ")" for j in results]
        tabs = st.tabs(tab_labels)

        for tab, job in zip(tabs, results):
            with tab:
                c1, c2, c3 = st.columns(3)
                c1.metric("Match Score", str(round(job.get("score",0),1)) + "/10")
                c2.metric("Competition", str(job.get("competition","")).capitalize())
                c3.metric("Sponsorship", str(job.get("sponsorship","")).capitalize())

                if job.get("url"):
                    st.markdown("[Apply Now](" + job["url"] + ")")
                if job.get("match_reason"):
                    st.success("Why you match: " + job["match_reason"])
                if job.get("gap") and str(job["gap"]).lower() not in ("none",""):
                    st.warning("Address in cover note: " + str(job["gap"]))
                if job.get("resume_tailor_prompt"):
                    with st.expander("Resume Tailor Prompt - copy and paste into your Resume Tailor project"):
                        st.text_area("", value=job["resume_tailor_prompt"], height=200,
                            key="rtp_" + str(results.index(job)),
                            label_visibility="collapsed")

                st.subheader("Decision Makers (" + str(len(job.get("contacts",[]))) + ")")
                for ci, contact in enumerate(job.get("contacts",[])):
                    priority = str(contact.get("priority",""))
                    icon = "🌟" if priority == "high" else "🔵" if priority == "medium" else "o"
                    label = icon + " " + str(contact.get("name","")) + " - " + str(contact.get("title",""))
                    with st.expander(label):
                        if contact.get("email"):
                            score_str = str(contact.get("email_score",""))
                            verified_str = " Verified" if contact.get("email_verified") else ""
                            st.markdown("**Direct Email:** `" + contact["email"] + "` (" + score_str + "% confidence" + verified_str + ")")
                        else:
                            st.caption("Email not found - use LinkedIn")
                        if contact.get("linkedInUrl"):
                            st.markdown("[LinkedIn Profile](" + str(contact["linkedInUrl"]) + ")")
                        if contact.get("email_draft"):
                            st.markdown("**Outreach Email Draft:**")
                            st.text_area("", value=str(contact["email_draft"]), height=120,
                                key="draft_" + str(results.index(job)) + "_" + str(ci),
                                label_visibility="collapsed")

if __name__ == "__main__":
    main()
