import re
import time
from urllib.parse import urlparse, urljoin

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from ddgs import DDGS
from io import BytesIO

st.set_page_config(page_title="Fresh Lead Finder", page_icon="🔎", layout="wide")
st.title("🔎 Fresh Lead Finder")
st.write("Find candidate company websites, check public contact details, and export an Excel workbook.")

st.info("This first web version uses public web search to discover candidate sites. It does not directly scrape Google Maps. Search results may include directories; review the source URLs. It does not bypass CAPTCHAs, logins, paywalls, or rate limits.")

with st.form("search_form"):
    query = st.text_input("Business / industry query", placeholder="e.g. Real estate agencies")
    area = st.text_input("Country / city / area (optional)", placeholder="e.g. Karachi, Pakistan")
    target = st.select_slider("Companies to find", options=[10, 20, 50, 100], value=100)
    submitted = st.form_submit_button("Start search and website research", type="primary")

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
UA = {"User-Agent": "Mozilla/5.0 (compatible; FreshLeadFinder/1.0; public business research)"}
BLOCKED_DOMAINS = ("google.com", "google.co.", "maps.google.", "youtube.com", "facebook.com",
                   "instagram.com", "linkedin.com", "wikipedia.org", "ensun.io", "yelp.com",
                   "yellowpages.", "tripadvisor.")

def root_url(raw):
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    p = urlparse(raw)
    return f"{p.scheme}://{p.netloc}/" if p.netloc else ""

def discover(q, location, target, log):
    base = " ".join(x.strip() for x in [q, location] if x and x.strip())
    queries = [base, base + " official website", base + " contact", base + " companies"]
    found, seen = [], set()
    with DDGS() as ddgs:
        for search_q in queries:
            if len(found) >= target:
                break
            log(f"Searching: {search_q}")
            try:
                for result in ddgs.text(search_q, max_results=min(50, target)):
                    raw = result.get("href") or result.get("url") or ""
                    site = root_url(raw)
                    if not site or any(x in urlparse(site).netloc.lower() for x in BLOCKED_DOMAINS):
                        continue
                    host = urlparse(site).netloc.lower().replace("www.", "")
                    title = (result.get("title") or "").strip()
                    if not title or host in seen:
                        continue
                    seen.add(host)
                    name = re.split(r"\s[|–-]\s", title)[0].strip()[:160]
                    found.append({
                        "Business Name": name,
                        "Website": site,
                        "Phone": "",
                        "Email": "",
                        "Business Description": (result.get("body") or "")[:500],
                        "Source URL": raw,
                        "Status": "Discovered; research pending"
                    })
                    log(f"Discovered {len(found)}/{target}: {name}")
                    if len(found) >= target:
                        break
            except Exception as exc:
                log(f"Search provider limit/error: {str(exc)[:180]}")
                time.sleep(1)
    return found

def contact_links(soup, base):
    out = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        label = (a.get_text(" ", strip=True) + " " + href).lower()
        if any(k in label for k in ("contact", "about", "get-in-touch", "reach-us")):
            full = urljoin(base, href)
            if urlparse(full).netloc == urlparse(base).netloc and full not in out:
                out.append(full)
    return out[:4]

def research(row, log):
    emails, phones, visited, queue = set(), set(), set(), [row["Website"]]
    description = row.get("Business Description", "")
    checked = 0
    while queue and checked < 5:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            response = requests.get(url, headers=UA, timeout=12, allow_redirects=True)
            if response.status_code >= 400 or "html" not in response.headers.get("content-type", "").lower():
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            text = soup.get_text(" ", strip=True)
            checked += 1
            if not description:
                meta = soup.find("meta", attrs={"name": re.compile("description", re.I)})
                description = (meta.get("content", "") if meta else "") or text[:350]
            emails.update(e.lower().strip(".,;:()[]<>") for e in EMAIL_RE.findall(response.text))
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.lower().startswith("mailto:"):
                    email = href.split(":", 1)[1].split("?", 1)[0].strip().lower()
                    if EMAIL_RE.fullmatch(email):
                        emails.add(email)
                if href.lower().startswith("tel:"):
                    phone = href.split(":", 1)[1].split("?", 1)[0].strip()
                    if 8 <= len(re.sub(r"\D", "", phone)) <= 15:
                        phones.add(phone)
            for phone in re.findall(r"(?:\+\d{1,3}[\s().-]?)?(?:\(?\d{2,5}\)?[\s.-]?)?\d[\d\s().-]{5,}\d", text):
                digits = re.sub(r"\D", "", phone)
                if 8 <= len(digits) <= 15:
                    phones.add(re.sub(r"\s+", " ", phone).strip())
            if checked == 1:
                queue.extend(contact_links(soup, response.url))
            time.sleep(0.25)
        except requests.RequestException as exc:
            log(f"Website could not be checked: {url} — {str(exc)[:100]}")
    preferred = sorted(emails, key=lambda e: (not any(x in e.split("@")[0] for x in
                       ("info", "sales", "contact", "hello", "office", "enquiry", "support")), len(e)))
    row["Email"] = preferred[0] if preferred else ""
    row["Phone"] = sorted(phones, key=len)[0] if phones else ""
    row["Business Description"] = description
    row["Status"] = f"Checked {checked} page(s); " + ("public contact found" if row["Email"] or row["Phone"] else "no public phone/email found")
    return row

def make_excel(rows, query, area):
    frame = pd.DataFrame(rows, columns=["Business Name", "Website", "Phone", "Email", "Business Description", "Source URL", "Status"])
    summary = pd.DataFrame([{"Field": "Query", "Value": query},
                            {"Field": "Area", "Value": area},
                            {"Field": "Companies researched", "Value": len(rows)},
                            {"Field": "Important note", "Value": "Search results may include directories. Verify sources before outreach."}])
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="Leads")
        summary.to_excel(writer, index=False, sheet_name="Search Summary")
        ws = writer.sheets["Leads"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for col, width in {"A":34,"B":38,"C":22,"D":34,"E":65,"F":42,"G":42}.items():
            ws.column_dimensions[col].width = width
    buffer.seek(0)
    return buffer.getvalue()

if submitted:
    if not query.strip():
        st.error("Please enter a business or industry query.")
    else:
        log_area = st.empty()
        log_lines = []
        def log(message):
            log_lines.append(message)
            log_area.code("\n".join(log_lines[-18:]))
        try:
            with st.spinner("Searching public web results..."):
                candidates = discover(query.strip(), area.strip(), int(target), log)
            st.write(f"Discovered **{len(candidates)} candidate websites**. Now checking each website.")
            results = []
            progress = st.progress(0)
            status = st.empty()
            for index, candidate in enumerate(candidates, 1):
                status.write(f"Researching {index}/{len(candidates)}: {candidate['Website']}")
                results.append(research(candidate, log))
                progress.progress(index / max(1, len(candidates)))
                st.session_state["fresh_leads_rows"] = results.copy()
            st.session_state["fresh_leads_query"] = query.strip()
            st.session_state["fresh_leads_area"] = area.strip()
            st.success(f"Finished: researched {len(results)} companies. The target is not guaranteed.")
        except Exception as exc:
            st.error(f"Search stopped: {type(exc).__name__}: {exc}")
            if "fresh_leads_rows" in st.session_state:
                st.warning("Partial results are available below if any were collected.")

if "fresh_leads_rows" in st.session_state and st.session_state["fresh_leads_rows"]:
    rows = st.session_state["fresh_leads_rows"]
    st.subheader(f"Results ({len(rows)})")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    data = make_excel(rows, st.session_state.get("fresh_leads_query", ""),
                      st.session_state.get("fresh_leads_area", ""))
    st.download_button("Download Excel workbook (.xlsx)", data=data, file_name="fresh_leads.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
