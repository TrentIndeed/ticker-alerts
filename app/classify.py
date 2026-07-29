"""Decide how loud a filing or headline should be.

The bias throughout is deliberate: when in doubt, alert. A form type we don't
recognise still gets through as INFO rather than being silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CRITICAL = "critical"  # dilution / offering — the ones that cost money
HIGH = "high"  # material event worth reading now
INFO = "info"  # routine, logged and sent quietly
NEWS = "news"  # headline, not a filing

SEV_ORDER = {CRITICAL: 0, HIGH: 1, NEWS: 2, INFO: 3}
SEV_EMOJI = {CRITICAL: "\U0001f6a8", HIGH: "\U0001f7e0", NEWS: "\U0001f535", INFO: "⚪"}

# ---------------------------------------------------------------------------
# Form types
# ---------------------------------------------------------------------------
# Prospectus supplements are where an offering actually gets PRICED. If you
# only ever watched one thing, it would be 424B5.
_OFFERING_FORMS = {
    "424A": "Prospectus amendment",
    "424B1": "Prospectus (offering)",
    "424B2": "Prospectus supplement — offering",
    "424B3": "Prospectus supplement — offering",
    "424B4": "Prospectus supplement — offering",
    "424B5": "Prospectus supplement — PRICED OFFERING",
    "424B7": "Prospectus supplement — selling stockholders",
    "424B8": "Prospectus supplement",
    "424H": "Prospectus (ABS)",
    "FWP": "Free writing prospectus — offering marketing",
    "SUPPL": "Prospectus supplement (foreign)",
    "S-1": "Registration statement — new shares",
    "S-1/A": "Registration statement amended",
    "S-1MEF": "Registration — additional shares",
    "S-3": "SHELF registration",
    "S-3/A": "Shelf registration amended",
    "S-3ASR": "Automatic SHELF registration",
    "S-3D": "Shelf — dividend reinvestment",
    "S-3MEF": "Shelf — additional shares",
    "F-1": "Registration (foreign issuer)",
    "F-1/A": "Registration amended (foreign)",
    "F-3": "SHELF registration (foreign issuer)",
    "F-3/A": "Shelf amended (foreign)",
    "F-3ASR": "Automatic shelf (foreign)",
    "POS AM": "Post-effective amendment",
    "EFFECT": "Registration DECLARED EFFECTIVE — offering can price",
    "CERT": "Exchange certification",
}

_HIGH_FORMS = {
    "8-K": "Material event",
    "6-K": "Foreign issuer report",
    "SC 13D": "Activist 5%+ stake",
    "SC 13D/A": "Activist stake amended",
    "SC 13G": "Passive 5%+ stake",
    "SC 13G/A": "Passive stake amended",
    "SC TO-T": "Tender offer",
    "SC TO-I": "Issuer tender offer",
    "DEFM14A": "Merger proxy",
    "PREM14A": "Preliminary merger proxy",
    "25": "DELISTING notice",
    "25-NSE": "DELISTING notice (exchange)",
    "NT 10-K": "LATE annual report",
    "NT 10-Q": "LATE quarterly report",
    "8-K/A": "Material event amended",
    "S-8": "Employee share plan registration",
    "S-4": "Registration — merger/exchange",
}

_INFO_FORMS = {
    "10-K": "Annual report",
    "10-Q": "Quarterly report",
    "4": "Insider transaction",
    "3": "Insider initial ownership",
    "5": "Insider annual statement",
    "DEF 14A": "Proxy statement",
    "144": "Notice of proposed sale",
    "13F-HR": "Institutional holdings",
}

# 8-K item codes that mean dilution or a financing event.
_DILUTIVE_8K_ITEMS = {
    "1.01": "Entry into material agreement (often a purchase/ATM agreement)",
    "3.02": "UNREGISTERED SALE OF EQUITY — dilution",
    "3.03": "Material modification to security holder rights",
    "2.03": "Direct financial obligation created",
    "5.03": "Charter amendment (often authorized-share increase or reverse split)",
}

_HIGH_8K_ITEMS = {
    "1.03": "Bankruptcy or receivership",
    "2.02": "Results of operations",
    "4.01": "Auditor change",
    "4.02": "Non-reliance on prior financials",
    "5.02": "Officer/director departure",
    "3.01": "Listing rule non-compliance / delisting notice",
}

# ---------------------------------------------------------------------------
# Text signals — catches what form type alone misses (ATMs, ELOCs, PIPEs).
# ---------------------------------------------------------------------------
_DILUTION_PATTERNS: list[tuple[str, str]] = [
    (r"at[- ]the[- ]market\s+(offering|sales|program|issuance)", "ATM program"),
    (r"\bATM\s+(program|agreement|offering|facility)", "ATM program"),
    (r"equity\s+(line|purchase)\s+of\s+credit|\bELOC\b", "Equity line of credit"),
    (r"standby\s+equity\s+(purchase|distribution)", "Standby equity facility"),
    (r"registered\s+direct\s+offering", "Registered direct offering"),
    (r"underwritten\s+public\s+offering", "Underwritten public offering"),
    (r"\bPIPE\b|private\s+investment\s+in\s+public\s+equity", "PIPE financing"),
    (r"convertible\s+(note|debenture|preferred)", "Convertible security"),
    (r"reverse\s+stock\s+split", "Reverse split"),
    (r"increase\s+(the\s+)?(number\s+of\s+)?authorized\s+shares", "Authorized share increase"),
    (r"shelf\s+registration", "Shelf registration"),
    (r"selling\s+stockholders?\s+may\s+(offer|sell|resell)", "Resale registration"),
    (r"warrants?\s+to\s+purchase", "Warrants attached"),
    (r"pre[- ]funded\s+warrants?", "Pre-funded warrants"),
    (r"securities\s+purchase\s+agreement", "Securities purchase agreement"),
    (r"placement\s+agent", "Placement agent involved"),
    (r"dilut(ion|ive)", "Dilution language"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in _DILUTION_PATTERNS]

# Headline signals for news alerts.
_NEWS_CRITICAL = [
    (r"\boffering\b", "Offering"),
    (r"\bpricing\s+of\b|\bprices\b.*\boffering\b", "Offering priced"),
    (r"at[- ]the[- ]market", "ATM"),
    (r"\bdilut", "Dilution"),
    (r"reverse\s+split", "Reverse split"),
    (r"\bregistered\s+direct\b", "Registered direct"),
    (r"public\s+offering", "Public offering"),
    (r"private\s+placement", "Private placement"),
    (r"\bdelist", "Delisting"),
    (r"\bbankrupt|chapter\s+11", "Bankruptcy"),
    (r"going\s+concern", "Going concern"),
]

_NEWS_HIGH = [
    (r"\bhalt(ed|s)?\b", "Trading halt"),
    (r"\bFDA\b|clinical|phase\s+[123]", "Clinical/FDA"),
    (r"acquisition|acquires|merger|to\s+be\s+acquired", "M&A"),
    (r"\bcontract\b|\bawarded\b|\bpartnership\b|\bdeal\b", "Contract/partnership"),
    (r"earnings|results|guidance|revenue", "Earnings/guidance"),
    (r"\bSEC\b.*(investigat|subpoena)|investigation", "Investigation"),
    (r"short\s+(report|seller)", "Short report"),
    (r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price\s+target", "Analyst action"),
]

_NEWS_CRITICAL_C = [(re.compile(p, re.IGNORECASE), lbl) for p, lbl in _NEWS_CRITICAL]
_NEWS_HIGH_C = [(re.compile(p, re.IGNORECASE), lbl) for p, lbl in _NEWS_HIGH]


@dataclass
class Verdict:
    severity: str
    label: str
    reasons: list[str]

    @property
    def emoji(self) -> str:
        return SEV_EMOJI.get(self.severity, "⚪")


def normalize_form(form: str) -> str:
    return re.sub(r"\s+", " ", (form or "").strip().upper())


def classify_filing(form: str, items: str = "", title: str = "") -> Verdict:
    """Classify by form type and 8-K item codes, before we've read the document."""
    f = normalize_form(form)

    if f in _OFFERING_FORMS:
        return Verdict(CRITICAL, _OFFERING_FORMS[f], ["Offering-related form type"])

    # Some filers use suffixed variants we haven't enumerated (e.g. "424B5/A").
    base = f.split("/")[0].strip()
    if base in _OFFERING_FORMS:
        return Verdict(
            CRITICAL, _OFFERING_FORMS[base] + " (amended)", ["Offering-related form type"]
        )

    if f in ("8-K", "8-K/A", "6-K") and items:
        codes = [c.strip() for c in re.split(r"[,;]", items) if c.strip()]
        dilutive = [c for c in codes if c in _DILUTIVE_8K_ITEMS]
        highs = [c for c in codes if c in _HIGH_8K_ITEMS]
        if dilutive:
            return Verdict(
                CRITICAL,
                f"8-K Item {dilutive[0]} — {_DILUTIVE_8K_ITEMS[dilutive[0]]}",
                [f"Item {c}: {_DILUTIVE_8K_ITEMS[c]}" for c in dilutive],
            )
        if highs:
            return Verdict(
                HIGH,
                f"8-K Item {highs[0]} — {_HIGH_8K_ITEMS[highs[0]]}",
                [f"Item {c}: {_HIGH_8K_ITEMS[c]}" for c in highs],
            )

    # A 6-K is how foreign private issuers announce offerings. Form type alone
    # tells us nothing, so these always get through and get a text scan.
    if f == "6-K":
        return Verdict(HIGH, "6-K — foreign issuer report", ["Foreign issuer filing"])

    if f in _HIGH_FORMS:
        return Verdict(HIGH, _HIGH_FORMS[f], ["Material form type"])
    if base in _HIGH_FORMS:
        return Verdict(HIGH, _HIGH_FORMS[base], ["Material form type"])

    if f in _INFO_FORMS:
        return Verdict(INFO, _INFO_FORMS[f], [])
    if base in _INFO_FORMS:
        return Verdict(INFO, _INFO_FORMS[base], [])

    # Unknown form type: never drop it, just send it quietly.
    return Verdict(INFO, f or "Unknown form", ["Unrecognised form type"])


def scan_text(text: str, limit: int = 6) -> list[str]:
    """Find offering/dilution language in filing body text."""
    hits: list[str] = []
    for pattern, label in _COMPILED:
        if pattern.search(text):
            if label not in hits:
                hits.append(label)
        if len(hits) >= limit:
            break
    return hits


def escalate_with_text(verdict: Verdict, text: str) -> Verdict:
    """Upgrade a verdict once we've actually read the filing."""
    hits = scan_text(text)
    if not hits:
        return verdict
    strong = {
        "ATM program",
        "Equity line of credit",
        "Standby equity facility",
        "Registered direct offering",
        "Underwritten public offering",
        "PIPE financing",
        "Shelf registration",
        "Pre-funded warrants",
        "Convertible security",
        "Reverse split",
        "Authorized share increase",
        "Securities purchase agreement",
    }
    severity = verdict.severity
    if strong.intersection(hits) and SEV_ORDER[severity] > SEV_ORDER[CRITICAL]:
        severity = CRITICAL
    return Verdict(severity, verdict.label, verdict.reasons + hits)


def classify_headline(headline: str, summary: str = "") -> Verdict:
    """Classify a news headline. Everything gets through; this sets the tone."""
    blob = f"{headline} {summary}"
    reasons = [lbl for rx, lbl in _NEWS_CRITICAL_C if rx.search(blob)]
    if reasons:
        return Verdict(CRITICAL, "Market-moving headline", reasons)
    reasons = [lbl for rx, lbl in _NEWS_HIGH_C if rx.search(blob)]
    if reasons:
        return Verdict(HIGH, "Headline", reasons)
    return Verdict(NEWS, "Headline", [])


def extract_offering_size(text: str) -> str | None:
    """Best-effort pull of the dollar size of an offering out of filing text."""
    patterns = [
        r"aggregate\s+offering\s+price\s+of\s+up\s+to\s+\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion)?",
        r"gross\s+proceeds\s+of\s+(?:approximately\s+)?\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion)?",
        r"up\s+to\s+\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion)?\s+of\s+(?:our\s+)?common\s+stock",
        r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion)\s+(?:public|registered|underwritten)\s+offering",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            amount = m.group(1)
            unit = (m.group(2) or "").lower()
            suffix = {"million": "M", "billion": "B"}.get(unit, "")
            return f"${amount}{suffix}"
    return None
