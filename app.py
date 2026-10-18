
import re
import time
from io import BytesIO
from urllib.parse import urlparse, urljoin

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from ddgs import DDGS

st.set_page_config(page_title="Fresh Lead Finder", page_icon="🔎", layout="wide")
st.title("🔎 Fresh Lead Finder")
st.write("Find company websites, check their own contact pages, and export Excel.")

st.info(
    "Blogs, directories and listicles are filtered where identifiable. "
    "Contact details are collected from the candidate company's website only. "
    "Please review source URLs because automated verification is not perfect."
)

BLOCKED_DOMAINS = {
    "google.com", "facebook.com", "instagram.com", "linkedin.com",
    "youtube.com", "wikipedia.org", "yelp.com", "yellowpages.com",
    "businesslist.pk", "zameen.com", "ensun.io", "crunchbase.com",
    "zoominfo.com", "clutch.co", "justdial.com", "kompass.com",
    "medium.com", "blogspot.com", "wordpress.com", "substack.com",
    "pinterest.com", "mapquest.com", "foursquare.com", "hotfrog.com",
    "cybo.com", "reddit.com", "tripadvisor.com"
}

BAD_WORDS = (
    "top 10", "top 20", "top 11", "best companies", "best agencies",
    "list of", "directory", "directories", "business listing",
    "company listing", "companies in", "blog", "guide to", "ranking"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    )
}

EMAIL_RE = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I
)


def get_host(url):
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def blocked_domain(host):
    return any(
        host == domain or host.endswith("." + domain)
        for domain in BLOCKED_DOMAINS
    )


def root_url(url):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    host = get_host(url)
    return "https://" + host + "/" if host else ""


def fetch(url):
    try:
        response = requests.get(
            url, headers=HEADERS, timeout=10, allow_redirects=True
        )
        if response.status_code >= 400:
            return "", response.url
        if "html" not in response.headers.get("Content-Type", "").lower():
            return "", response.url
        return response.text[:1500000], response.url
    except requests.RequestException:
        return "", url


def visible_text(soup):
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def is_list_page(title, snippet, url):
    text = f"{title} {snippet} {url}".lower()
    return any(word in text for word in BAD_WORDS)


def get_contacts(html):
    soup = BeautifulSoup(html, "html.parser")
    emails = set()
    phones = set()

    for link in soup.select('a[href^="mailto:"]'):
        value = link.get("href", "").split(":", 1)[1].split("?")[0].strip()
        if EMAIL_RE.fullmatch(value):
            emails.add(value.lower())

    for link in soup.select('a[href^="tel:"]'):
        value = link.get("href", "").split(":", 1)[1].strip()
        if len(re.sub(r"\D", "", value)) >= 7:
            phones.add(value)

    text = visible_text(soup)

    for email in EMAIL_RE.findall(text):
        if "example.com" not in email.lower():
            emails.add(email.lower())

    phone_pattern = r"(?<!\w)\+?\d[\d\s()./-]{6,}\d(?!\w)"
    for phone in re.findall(phone_pattern, text):
        digits = re.sub(r"\D", "", phone)
        if 7 <= len(digits) <= 16:
            phones.add(re.sub(r"\s+", " ", phone).strip())

    return sorted(emails), sorted(phones), soup


def discover(query, area, target, log):
    found = {}

    searches = [
        f'"{query}" official website {area}',
        f'{query} companies {area} contact us',
        f'{query} company website {area}',
        f'{query} business official contact {area}',
    ]

    with DDGS() as searcher:
        for search_query in searches:
            log.write(f"Searching: {search_query}")
            try:
                results = searcher.text(search_query, max_results=30)

                for result in results:
                    title = result.get("title", "")
                    url = result.get("href", "") or result.get("url", "")
                    snippet = result.get("body", "") or result.get("snippet", "")

                    if not url.startswith(("http://", "https://")):
                        continue

                    host = get_host(url)

                    if not host or blocked_domain(host):
                        continue

                    if is_list_page(title, snippet, url):
                        continue

                    found.setdefault(host, {
                        "title": title,
                        "url": root_url(url)
                    })

                    if len(found) >= target * 3:
                        break

            except Exception as exc:
                log.write(
                    f"Search issue: {type(exc).__name__}. "
                    "Continuing without bypassing limits."
                )

            time.sleep(0.5)

    return list(found.values())


def research_site(candidate, query, area):
    html, final_url = fetch(candidate["url"])

    if not html:
        return None

    host = get_host(final_url)
    if not host or blocked_domain(host):
        return None

    emails, phones, soup = get_contacts(html)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    page_text = visible_text(soup)

    if is_list_page(title, page_text[:2000], final_url):
        return None

    if any(word in host for word in (
        "blog", "directory", "directories", "yellowpages", "businesslist"
    )):
        return None

    site_name = soup.find("meta", attrs={"property": "og:site_name"})
    name = site_name.get("content", "").strip() if site_name else ""

    if not name:
        name = re.split(r"\s+[|–—-]\s+", title)[0].strip()

    if not name or len(name) < 2:
        name = host.split(".")[0].replace("-", " ").title()

    description = ""
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta:
        description = meta.get("content", "").strip()

    # Find contact/about pages linked from the company's own website.
    pages = []
    for link in soup.find_all("a", href=True):
        href = urljoin(final_url, link["href"])
        label = (link.get_text(" ", strip=True) + " " + href).lower()

        if (
            get_host(href) == host
            and any(word in label for word in (
                "contact", "about", "reach us", "get in touch", "imprint"
            ))
            and href.startswith(("http://", "https://"))
        ):
            if href not in pages:
                pages.append(href)

    for path in ("/contact", "/contact-us", "/about", "/about-us"):
        pages.append(urljoin(final_url, path))

    visited = set()

    for page_url in pages[:10]:
        if page_url in visited or get_host(page_url) != host:
            continue

        visited.add(page_url)
        contact_html, resolved_url = fetch(page_url)

        if not contact_html or get_host(resolved_url) != host:
            continue

        page_emails, page_phones, _ = get_contacts(contact_html)
        emails.extend(page_emails)
        phones.extend(page_phones)

        if emails and phones:
            break

        time.sleep(0.15)

    return {
        "Business Name": name,
        "Website": final_url,
        "Phone": next(iter(dict.fromkeys(phones)), ""),
        "Email": next(iter(dict.fromkeys(emails)), ""),
        "Business Description": description[:500],
        "Location Searched": area,
        "Search Query": query,
        "Source URL": final_url,
        "Contact Source URL": final_url if emails or phones else ""
    }


with st.form("lead_search"):
    query = st.text_input(
        "Business / industry",
        placeholder="e.g. Real estate agencies"
    )
    area = st.text_input(
        "Country / city / area (optional)",
        placeholder="e.g. Karachi, Pakistan"
    )
    target = st.select_slider(
        "Companies to find",
        options=[10, 20, 50, 100],
        value=100
    )
    submitted = st.form_submit_button(
        "Start search and website research",
        type="primary"
    )

if submitted:
    if not query.strip():
        st.warning("Please enter a business or industry.")
        st.stop()

    log = st.container()
    progress = st.progress(0)

    with st.spinner("Searching for candidate company websites..."):
        candidates = discover(query.strip(), area.strip(), int(target), log)

    if not candidates:
        st.warning(
            "No suitable websites found. Try a different query or location."
        )
        st.stop()

    st.write(f"Candidate websites found: {len(candidates)}")

    rows = []
    seen = set()

    for index, candidate in enumerate(candidates):
        if len(rows) >= target:
            break

        try:
            row = research_site(candidate, query.strip(), area.strip())

            if row:
                host = get_host(row["Website"])
                if host not in seen:
                    seen.add(host)
                    rows.append(row)
                    log.write(
                        f"Accepted {len(rows)}/{target}: "
                        f"{row['Business Name']} — {row['Website']}"
                    )
        except Exception as exc:
            log.write(f"Skipped one website: {type(exc).__name__}")

        progress.progress((index + 1) / len(candidates))
        time.sleep(0.2)

    if not rows:
        st.warning(
            "No websites passed the filters. Try a broader query or another area."
        )
        st.stop()

    df = pd.DataFrame(rows)

    st.success(
        f"Finished: {len(df)} websites passed the filters. "
        "The requested number is a target, not a guarantee."
    )
    st.dataframe(df, use_container_width=True, hide_index=True)

    excel = BytesIO()

    with pd.ExcelWriter(excel, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Company Leads")
        pd.DataFrame([{
            "Query": query.strip(),
            "Area": area.strip(),
            "Requested": int(target),
            "Found": len(df),
            "Note": (
                "Review each source URL. Contact details are taken from "
                "the candidate website's own pages only."
            )
        }]).to_excel(writer, index=False, sheet_name="Search Summary")

    st.download_button(
        "Download Excel workbook (.xlsx)",
        data=excel.getvalue(),
        file_name="fresh_lead_finder_results.xlsx",
        mime=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        )
    )
