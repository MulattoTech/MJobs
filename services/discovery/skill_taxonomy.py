"""Provider-neutral technology taxonomy with original wording preserved."""
from __future__ import annotations

import re


SKILL_TAXONOMY = {
    "ACTIVE_DIRECTORY": ("Active Directory", ("Active Directory", "Microsoft Active Directory", "AD")),
    "WINDOWS_SERVER": ("Windows Server", ("Windows Server",)),
    "POWERSHELL": ("PowerShell", ("PowerShell",)),
    "MICROSOFT_365": ("Microsoft 365", ("Microsoft 365", "Office 365", "M365", "O365")),
    "ENTRA_ID": ("Entra ID", ("Entra ID", "Azure AD", "Azure Active Directory")),
    "AZURE": ("Azure", ("Microsoft Azure", "Azure")),
    "AWS": ("AWS", ("Amazon Web Services", "AWS")),
    "LINUX": ("Linux", ("Linux",)),
    "VMWARE": ("VMware", ("VMware", "vSphere", "ESXi")),
    "HYPER_V": ("Hyper-V", ("Hyper-V", "Hyper V")),
    "DNS": ("DNS", ("DNS", "Domain Name System")),
    "DHCP": ("DHCP", ("DHCP", "Dynamic Host Configuration Protocol")),
    "GROUP_POLICY": ("Group Policy", ("Group Policy", "GPO", "Group Policy Object")),
    "INTUNE": ("Intune", ("Microsoft Intune", "Intune")),
    "MECM": ("MECM", ("MECM", "SCCM", "Microsoft Endpoint Configuration Manager", "System Center Configuration Manager")),
    "CISCO": ("Cisco", ("Cisco",)),
    "DOCKER": ("Docker", ("Docker",)),
    "KUBERNETES": ("Kubernetes", ("Kubernetes", "K8s")),
    "TERRAFORM": ("Terraform", ("Terraform",)),
    "ANSIBLE": ("Ansible", ("Ansible",)),
    "VEEAM": ("Veeam", ("Veeam",)),
    "RUBRIK": ("Rubrik", ("Rubrik",)),
    "SAN": ("SAN", ("Storage Area Network", "SAN")),
    "NAS": ("NAS", ("Network Attached Storage", "NAS")),
    "PACS": ("PACS", ("Picture Archiving and Communication System", "PACS")),
    "PYTHON": ("Python", ("Python",)),
    "SQL": ("SQL", ("SQL",)),
    "JAVASCRIPT": ("JavaScript", ("JavaScript", "JS")),
    "TYPESCRIPT": ("TypeScript", ("TypeScript", "TS")),
    "JAVA": ("Java", ("Java",)),
    "CSHARP": ("C#", ("C#", "C Sharp")),
    "CPP": ("C++", ("C++", "CPP")),
    "GO": ("Go", ("Golang", "Go")),
    "RUST": ("Rust", ("Rust",)),
    "REACT": ("React", ("React", "ReactJS", "React.js")),
    "POSTGRESQL": ("PostgreSQL", ("PostgreSQL", "Postgres")),
}


def _alias_index():
    rows = []
    for identifier, (label, aliases) in SKILL_TAXONOMY.items():
        for alias in aliases:
            rows.append((alias, identifier, label))
    return sorted(rows, key=lambda row: (-len(row[0]), row[0].casefold()))

_ALIAS_INDEX = _alias_index()
_SHORT_CASE_SENSITIVE = {"AD", "Go"}


def _pattern(alias):
    return re.compile(r"(?<!\w)" + re.escape(alias) + r"(?!\w)",
                      0 if alias in _SHORT_CASE_SENSITIVE else re.I)


def canonical_skill_id(value):
    text = str(value or "").strip()
    if not text:
        return None
    normalized_id = re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")
    if normalized_id in SKILL_TAXONOMY:
        return normalized_id
    folded = text.casefold()
    for alias, identifier, _ in _ALIAS_INDEX:
        if alias.casefold() == folded:
            return identifier
    return None


def normalize_skill_list(values):
    result = []
    seen = set()
    for value in values or []:
        identifier = canonical_skill_id(value)
        if identifier is None:
            raise ValueError(f"Unknown normalized skill: {value}")
        if identifier not in seen:
            seen.add(identifier)
            result.append(identifier)
    return result


def extract_skill_mentions(text):
    source = str(text or "")
    candidates = []
    for alias, identifier, label in _ALIAS_INDEX:
        for match in _pattern(alias).finditer(source):
            candidates.append((match.start(), -(match.end() - match.start()), match.end(),
                               identifier, label, match.group(0)))
    candidates.sort()
    occupied = []
    mentions = []
    seen_ids = set()
    for start, _, end, identifier, label, original in candidates:
        if any(not (end <= left or start >= right) for left, right in occupied):
            continue
        occupied.append((start, end))
        if identifier in seen_ids:
            continue
        seen_ids.add(identifier)
        mentions.append({
            "id": identifier,
            "label": label,
            "original": original,
            "start": start,
            "end": end,
        })
    mentions.sort(key=lambda row: row["start"])
    return mentions


def extract_skills(text):
    mentions = extract_skill_mentions(text)
    return {
        "ids": [row["id"] for row in mentions],
        "labels": [row["label"] for row in mentions],
        "mentions": mentions,
    }


def skill_label(identifier):
    row = SKILL_TAXONOMY.get(identifier)
    return row[0] if row else None
