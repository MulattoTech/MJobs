"""Run a bounded local Playwright playbook with postconditions and selector recovery."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from afk_redact import sanitize
from afk_state import atomic_json


class HumanBoundary(RuntimeError):
    pass


HIGH_IMPACT = re.compile(r"\b(?:pay|purchase|checkout|delete|destroy|reset password|change password|consent|agree to terms|accept terms|authorize access|grant access|production)\b", re.I)


def guard_page(page):
    if page.locator("input[autocomplete='one-time-code'], input[type='password']").count() or page.get_by_text(re.compile(r"\b(?:CAPTCHA|two.factor authentication|MFA required)\b", re.I)).count():
        raise HumanBoundary("MFA/CAPTCHA/credential entry requires the authorized human; stop at this page")
    headings = " ".join(page.locator("h1,h2,[role='dialog']").all_text_contents())
    if re.search(r"\b(?:checkout|payment|terms of service|OAuth consent|grant access|delete account|reset password)\b", headings, re.I):
        raise HumanBoundary("High-impact browser page requires separate authorization and operator handling")


def guard_step(step):
    target = " ".join(str(step.get(key, "")) for key in ("id", "selector", "fallbackName"))
    if HIGH_IMPACT.search(target):
        raise HumanBoundary("High-impact browser action requires separate authorization and operator handling")
    if step.get("action") not in {"click", "fill"}:
        raise ValueError("Unsupported playbook action")
    if step.get("action") == "click" and not (step.get("expectedText") or step.get("expectedUrl")):
        raise ValueError("Click requires an observable postcondition")
    if step.get("action") == "fill" and not step.get("expectedValue"):
        raise ValueError("Fill requires expectedValue postcondition")


def _locator(page, step):
    selector = step.get("selector")
    if selector:
        target = page.locator(selector)
        if target.count() == 1:
            return target, False
    role, name = step.get("fallbackRole"), step.get("fallbackName")
    if role and name:
        target = page.get_by_role(role, name=name, exact=True)
        if target.count() == 1:
            return target, True
    raise ValueError(f"No unique semantic target for step {step.get('id')}")


def run_playbook(playbook, *, executable_path=None, headless=True):
    from playwright.sync_api import sync_playwright

    updated = json.loads(json.dumps(playbook))
    if not updated.get("steps"):
        raise ValueError("Empty playbook cannot pass")
    recovered = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless, executable_path=executable_path)
        try:
            page = browser.new_page()
            page.goto(updated["url"], wait_until="domcontentloaded")
            guard_page(page)
            for step in updated["steps"]:
                guard_page(page)
                guard_step(step)
                target, healed = _locator(page, step)
                target_identity = " ".join(filter(None, [target.inner_text(), target.get_attribute("aria-label"), target.get_attribute("value"), target.get_attribute("title"), target.get_attribute("name")]))
                if step["action"] == "click" and HIGH_IMPACT.search(target_identity):
                    raise HumanBoundary("High-impact browser target requires separate authorization and operator handling")
                if step.get("expectedUrl") and re.search(step["expectedUrl"], page.url):
                    raise ValueError(f"URL postcondition was already true before step {step['id']}")
                pre_existing_text = page.get_by_text(step["expectedText"], exact=True).count() if step.get("expectedText") else 0
                if pre_existing_text:
                    raise ValueError(f"Postcondition was already true before step {step['id']}")
                if healed:
                    step.pop("selector", None)
                    recovered.append(step["id"])
                if step["action"] == "click":
                    target.click(timeout=5000)
                elif step["action"] == "fill":
                    target.fill(step["value"], timeout=5000)
                guard_page(page)
                if step.get("expectedText"):
                    page.get_by_text(step["expectedText"], exact=True).wait_for(timeout=5000)
                if step.get("expectedUrl") and not re.search(step["expectedUrl"], page.url):
                    raise AssertionError(f"Expected URL transition not observed for {step['id']}")
                if step.get("expectedValue") and target.input_value() != step["expectedValue"]:
                    raise AssertionError(f"Expected field value not observed for {step['id']}")
            return {"passed": True, "finalUrl": page.url, "recoveredSelectors": recovered, "playbook": updated}
        finally:
            browser.close()


def save_playbook(path, playbook):
    forbidden = re.compile(r"cookie|password|token|secret|sessionStorage|localStorage", re.I)
    if forbidden.search(json.dumps(playbook)):
        raise ValueError("Playbook contains sensitive fields or values")
    atomic_json(path, sanitize(playbook))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("playbook", type=Path)
    parser.add_argument("--chrome", help="Existing Chrome executable path")
    parser.add_argument("--save-healed", action="store_true")
    args = parser.parse_args(argv)
    original = json.loads(args.playbook.read_text(encoding="utf-8"))
    result = run_playbook(original, executable_path=args.chrome)
    if args.save_healed and result["recoveredSelectors"]:
        save_playbook(args.playbook, result["playbook"])
    print(json.dumps(sanitize({k: v for k, v in result.items() if k != "playbook"}), indent=2))


if __name__ == "__main__":
    main()
