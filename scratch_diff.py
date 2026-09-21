"""
Three-way HTML diff for INDOS checker page.

Case A: No input submitted (idle page GET)
Case B: Fake INDOS number + fake DOB (should be REJECTED)
Case C: Fake INDOS number, NO DOB (what our executor currently sends)

We diff at raw HTML level using difflib to find the real discriminator.
"""

import requests
import difflib
from bs4 import BeautifulSoup

BASE_URL = "http://220.156.189.33/esamudraUI/jsp/examination/checker/PP_IndosChecker.jsp"

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": BASE_URL,
}

FORM_BASE = {
    "hidSystemDate": "17/09/2026",
    "hidSystemDate1": "",
    "hidProcessId": "",
    "certNo": "",
    "btnNext": "Search",
}


def fetch(label, method="GET", data=None):
    print(f"\n{'='*60}")
    print(f"Fetching Case {label}...")
    if method == "GET":
        resp = requests.get(BASE_URL, headers=COMMON_HEADERS, timeout=15)
    else:
        resp = requests.post(BASE_URL, data=data, headers=COMMON_HEADERS, timeout=15)
    print(f"  HTTP {resp.status_code}, {len(resp.text)} chars")
    return resp.text


def body_text(html):
    """Extract just the <body> inner HTML, normalised."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body")
    return (body.get_text(separator="\n", strip=True) if body else html)


def save(name, html):
    with open(f"scratch_diff_{name}.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  Saved scratch_diff_{name}.html")


# ── Case A: Idle GET (no submission) ─────────────────────────────────────────
html_a = fetch("A: Idle GET")
save("A_idle", html_a)

# ── Case B: Fake number + fake DOB (what we WANT the executor to do) ──────────
data_b = {
    **FORM_BASE,
    "cmbSearch_by": "INDOS",
    "txtNo": "TEST_STRUCTURAL_001",
    "txtDob": "01/01/1990",   # fake DOB, filled correctly
}
html_b = fetch("B: Fake number + fake DOB", method="POST", data=data_b)
save("B_fake_with_dob", html_b)

# ── Case C: Fake number, NO DOB (what our executor currently sends) ───────────
data_c = {
    **FORM_BASE,
    "cmbSearch_by": "INDOS",
    "txtNo": "TEST_STRUCTURAL_001",
    "txtDob": "",   # DOB missing — current executor behaviour
}
html_c = fetch("C: Fake number, no DOB", method="POST", data=data_c)
save("C_fake_no_dob", html_c)


# ── Diffs ─────────────────────────────────────────────────────────────────────
def show_diff(label_x, html_x, label_y, html_y):
    lines_x = html_x.splitlines(keepends=True)
    lines_y = html_y.splitlines(keepends=True)
    diff = list(difflib.unified_diff(lines_x, lines_y,
                                     fromfile=label_x, tofile=label_y,
                                     n=3))
    print(f"\n{'='*60}")
    print(f"DIFF: {label_x}  →  {label_y}   ({len(diff)} diff lines)")
    if not diff:
        print("  *** IDENTICAL — responses are byte-for-byte the same ***")
    else:
        # Print first 120 diff lines so it's readable in terminal
        for line in diff[:120]:
            print(line, end="")
        if len(diff) > 120:
            print(f"\n  ... ({len(diff) - 120} more diff lines truncated) ...")

show_diff("A_idle", html_a, "B_fake_with_dob", html_b)
show_diff("A_idle", html_a, "C_fake_no_dob",  html_c)
show_diff("B_fake_with_dob", html_b, "C_fake_no_dob", html_c)

# ── Text-level body comparison (easier to read) ───────────────────────────────
print(f"\n{'='*60}")
print("TEXT BODY COMPARISON (what a human would read in each case):")
for label, html in [("A_idle", html_a), ("B_fake_with_dob", html_b), ("C_fake_no_dob", html_c)]:
    print(f"\n--- {label} ---")
    print(body_text(html)[:2000])
    print("  ...")
