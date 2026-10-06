"""Validate the pinned reference-project registry and license-safe review artifacts."""
from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
SHA = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_POLICY_TERMS = {
    "clean-room": "clean-room implementation posture",
    "CAPTCHA": "CAPTCHA prohibition",
    "MFA": "MFA bypass prohibition",
    "access-control": "access-control bypass prohibition",
    "rate-limit": "rate-limit circumvention prohibition",
    "fingerprint spoofing": "fingerprint spoofing prohibition",
    "anti-bot evasion": "anti-bot evasion prohibition",
}


def _sha(value: object) -> bool:
    return isinstance(value, str) and bool(SHA.fullmatch(value))


def _pinned_license(license_info: object, commit: str, repository_url: object) -> list[str]:
    errors = []
    if not isinstance(license_info, dict):
        return ["license evidence must be an object"]
    for key in ("spdx_id", "name", "path", "source_url", "git_blob_sha"):
        if not isinstance(license_info.get(key), str) or not license_info[key].strip():
            errors.append(f"license evidence needs {key}")
    if not _sha(license_info.get("git_blob_sha")):
        errors.append("license git_blob_sha must be a full Git object ID")
    path = license_info.get("path")
    source_url = license_info.get("source_url")
    if isinstance(path, str) and isinstance(source_url, str):
        parsed = urlsplit(source_url)
        if parsed.scheme != "https" or parsed.netloc != "github.com":
            errors.append("license source_url must use the canonical GitHub HTTPS repository URL")
        repository = urlsplit(repository_url) if isinstance(repository_url, str) else None
        if repository is None or (parsed.scheme, parsed.netloc, parsed.path.split("/blob/", 1)[0]) != (
            repository.scheme, repository.netloc, repository.path.rstrip("/")
        ):
            errors.append("license source_url repository must match repository_url")
        if not source_url.endswith(f"/{commit}/{path}"):
            errors.append("license source_url must pin the inspected commit and exact license path")
    return errors


def _validate_registry(registry: object) -> list[str]:
    errors = []
    if not isinstance(registry, dict) or type(registry.get("schema_version")) is not int or registry.get("schema_version") != 1:
        return ["reference registry must be a schema_version 1 object"]
    try:
        date.fromisoformat(registry.get("inspected_at", ""))
    except (TypeError, ValueError):
        errors.append("reference registry inspected_at must be an ISO date")
    if not isinstance(registry.get("inspection_method"), str) or not registry["inspection_method"].strip():
        errors.append("reference registry must state its inspection method")
    if not isinstance(registry.get("default_posture"), str) or "clean-room" not in registry["default_posture"].lower():
        errors.append("reference registry must set a clean-room default use posture")
    projects = registry.get("projects")
    if not isinstance(projects, list) or not projects:
        return errors + ["reference registry must contain project records"]
    seen = set()
    for index, project in enumerate(projects):
        where = f"project[{index}]"
        if not isinstance(project, dict):
            errors.append(f"{where} must be an object")
            continue
        project_id = project.get("id")
        if not isinstance(project_id, str) or not project_id.strip():
            errors.append(f"{where} needs a stable id")
        elif project_id in seen:
            errors.append(f"duplicate reference project id: {project_id}")
        else:
            seen.add(project_id)
        for key in ("name", "repository_url", "inspected_ref", "commit_date", "allowed_use_posture"):
            if not isinstance(project.get(key), str) or not project[key].strip():
                errors.append(f"{where} needs {key}")
        commit = project.get("inspected_commit")
        if not _sha(commit):
            errors.append(f"{where} inspected_commit must be a full commit SHA")
            commit = ""
        if isinstance(project.get("commit_date"), str):
            try:
                datetime.fromisoformat(project["commit_date"].replace("Z", "+00:00"))
            except ValueError:
                errors.append(f"{where} commit_date must be an ISO timestamp")
        repo = project.get("repository_url")
        if isinstance(repo, str):
            parsed = urlsplit(repo)
            if parsed.scheme != "https" or parsed.netloc != "github.com" or not parsed.path.strip("/"):
                errors.append(f"{where} repository_url must be a canonical GitHub HTTPS URL")
        errors.extend(f"{where}: {error}" for error in _pinned_license(
            project.get("license"), commit, project.get("repository_url")
        ))
        if not project.get("allowed_use_posture"):
            errors.append(f"{where} needs an explicit allowed_use_posture")
        license_info = project.get("license")
        if isinstance(license_info, dict) and "polyform" in str(license_info.get("name", "")).lower():
            if "do not copy" not in str(project.get("allowed_use_posture", "")).lower():
                errors.append(f"{where} PolyForm posture must explicitly prohibit copying")
        history = project.get("license_history", [])
        if not isinstance(history, list):
            errors.append(f"{where} license_history must be a list")
            continue
        for hindex, row in enumerate(history):
            hwhere = f"{where} license_history[{hindex}]"
            if not isinstance(row, dict):
                errors.append(f"{hwhere} must be an object")
                continue
            ref = row.get("ref")
            if not _sha(ref):
                errors.append(f"{hwhere} ref must be a full commit SHA")
                ref = ""
            for key in ("commit_date", "spdx_id", "path", "source_url", "git_blob_sha", "allowed_use_posture"):
                if not isinstance(row.get(key), str) or not row[key].strip():
                    errors.append(f"{hwhere} needs {key}")
            if not _sha(row.get("git_blob_sha")):
                errors.append(f"{hwhere} git_blob_sha must be a full Git object ID")
            source_url = row.get("source_url")
            path = row.get("path")
            if isinstance(source_url, str) and isinstance(path, str) and not source_url.endswith(f"/{ref}/{path}"):
                errors.append(f"{hwhere} source_url must pin its historical ref and license path")
            if isinstance(source_url, str):
                parsed = urlsplit(source_url)
                repository = urlsplit(project.get("repository_url", ""))
                if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.path.split("/blob/", 1)[0] != repository.path.rstrip("/"):
                    errors.append(f"{hwhere} source_url repository must match repository_url")
    return errors


def validate_reference_provenance(root: Path = ROOT) -> list[str]:
    root = Path(root).resolve()
    errors = []
    registry = None
    paths = {
        "registry": "docs/reference/REFERENCE_PROJECTS.json",
        "policy": "docs/reference/REFERENCE_IMPLEMENTATION_POLICY.md",
        "inspiration log": "docs/reference/REFERENCE_INSPIRATION_LOG.md",
        "pull request template": ".github/PULL_REQUEST_TEMPLATE.md",
        "third-party notices": "THIRD_PARTY_NOTICES.md",
    }
    texts = {}
    for name, relative in paths.items():
        try:
            texts[name] = (root / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            errors.append(f"missing or unreadable {name}: {relative}")
    if "registry" in texts:
        try:
            registry = json.loads(texts["registry"])
            errors.extend(_validate_registry(registry))
        except json.JSONDecodeError as error:
            errors.append(f"reference registry is invalid JSON: {error.msg}")
    policy = texts.get("policy", "").lower()
    for term, label in REQUIRED_POLICY_TERMS.items():
        if term.lower() not in policy:
            errors.append(f"reference policy must include {label}")
    for term in ("material-inspiration record", "exact source", "before implementation", "third-party notices", "does not replace review"):
        if term not in policy:
            errors.append(f"reference policy must document {term}")
    template = texts.get("pull request template", "").lower()
    for term in ("reference provenance", "mjobs issue", "pinned commit", "license compatibility", "third-party notices", "captcha", "reference_provenance"):
        if term not in template:
            errors.append(f"pull request template must include {term}")
    notices = texts.get("third-party notices", "").lower()
    for term in ("as of 2026-10-06", "does not intentionally include", "before any third-party", "distribution scope"):
        if term not in notices:
            errors.append(f"third-party notices must state {term}")
    log = texts.get("inspiration log", "").lower()
    for term in ("#373 / mj-ref-003", "#374 / mj-ref-004", "#384 / mj-ref-014", "#387 / mj-ref-017", "does not assert"):
        if term not in log:
            errors.append(f"reference inspiration log must include {term}")
    if isinstance(registry, dict):
        projects = {row.get("id"): row for row in registry.get("projects", [])
                    if isinstance(row, dict) and isinstance(row.get("id"), str)}
        records = registry.get("inspiration_records")
        if not isinstance(records, list):
            errors.append("reference registry must include inspiration_records")
        else:
            for index, record in enumerate(records):
                where = f"inspiration_records[{index}]"
                if not isinstance(record, dict):
                    errors.append(f"{where} must be an object")
                    continue
                issue = record.get("mjobs_issue")
                ids = record.get("project_ids")
                commits = record.get("source_commits")
                target = record.get("mjobs_record")
                if not isinstance(issue, str) or not issue.strip() or issue.lower() not in log:
                    errors.append(f"{where} MJobs issue must be present in the inspiration log")
                if (not isinstance(ids, list) or not ids
                        or any(project_id not in projects for project_id in ids)):
                    errors.append(f"{where} must identify registered reference project IDs")
                    continue
                if not isinstance(commits, list) or not commits:
                    errors.append(f"{where} must pin source commit(s)")
                    continue
                if any(not _sha(commit) for commit in commits):
                    errors.append(f"{where} source_commits must be full commit SHAs")
                    continue
                allowed_commits = set()
                for project_id in ids:
                    project = projects[project_id]
                    allowed_commits.add(project.get("inspected_commit"))
                    allowed_commits.update(row.get("ref") for row in project.get("license_history", [])
                                           if isinstance(row, dict))
                if not set(commits).issubset(allowed_commits):
                    errors.append(f"{where} source commit is not pinned by its registry project")
                if any(commit.lower() not in log for commit in commits):
                    errors.append(f"{where} source commit(s) must be present in the inspiration log")
                if not isinstance(record.get("material_concept"), str) or not record["material_concept"].strip():
                    errors.append(f"{where} needs a material_concept")
                if not isinstance(record.get("third_party_code_or_assets_incorporated"), bool):
                    errors.append(f"{where} must state whether third-party code/assets were incorporated")
                elif record["third_party_code_or_assets_incorporated"]:
                    for project_id in ids if isinstance(ids, list) else []:
                        if project_id.lower() not in notices:
                            errors.append(f"{where} incorporated source must be listed in THIRD_PARTY_NOTICES.md")
                    for commit in commits if isinstance(commits, list) else []:
                        if commit.lower() not in notices:
                            errors.append(f"{where} incorporated source commit must be listed in THIRD_PARTY_NOTICES.md")
                    if isinstance(issue, str) and issue.lower() not in notices:
                        errors.append(f"{where} incorporating issue must be listed in THIRD_PARTY_NOTICES.md")
                if not isinstance(target, str) or not (root / target).is_file():
                    errors.append(f"{where} needs an existing MJobs issue/design/evidence record")
    return errors


def main() -> int:
    errors = validate_reference_provenance()
    for error in errors:
        print(error)
    print(f"Reference provenance validation: {'FAIL' if errors else 'PASS'}")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
