"""Optional no-launch browser smoke for the MJobs Mission Control effective prompts.

Run against an already started, deliberately isolated Mission Control instance:
  python tests/integration/mission_prompt_preview_browser_smoke.py --url http://127.0.0.1:18732/
Never presses Launch CLI or Stop and never requests an API launch endpoint.
"""
from __future__ import annotations
import argparse
from playwright.sync_api import sync_playwright

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",default="http://127.0.0.1:18726/")
    args=parser.parse_args()
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page(viewport={"width":1500,"height":900})
        errors=[];launches=[]
        page.on("pageerror",lambda e: errors.append(str(e)))
        page.on("request",lambda req: launches.append(req.url) if req.url.endswith("/launch") else None)
        page.goto(args.url,wait_until="domcontentloaded",timeout=20000)
        page.locator(".launch-card").first.wait_for(timeout=45000)
        outputs=[]
        for lane in ("codex","antigravity","claude"):
            card=page.locator('.launch-card[data-slot="'+lane+'"]')
            if not card.count(): raise AssertionError("Missing launcher card: "+lane)
            model=card.locator(".launch-model").input_value()
            if not model: raise AssertionError("No installed model choice for "+lane)
            card.locator(".launch-preview").click()
            dlg=page.locator("#launchReviewDialog")
            dlg.wait_for(state="visible",timeout=9000)
            page.wait_for_function(
                """(model) => {
                  const text=document.querySelector('#launchReviewText')?.textContent||'';
                  return text.includes('model='+model+';');
                }""",arg=model,timeout=17000)
            prompt=page.locator("#launchReviewText").text_content()
            command=page.locator("#launchReviewCommand").text_content()
            if len(prompt)<200:raise AssertionError("Unexpectedly short prompt on "+lane)
            if model not in command:raise AssertionError("CLI launch flags out of sync: "+lane)
            if card.locator(".launch-start").is_enabled():raise AssertionError("Launch unlocked before review: "+lane)
            page.locator("#launchReviewClose").click()
            page.locator("#launchReviewDialog").wait_for(state="hidden",timeout=9000)
            page.wait_for_function(
                """(model) => (document.querySelector('[data-slot="'+model[0]+'"] .launch-prompt-preview')?.textContent||'').includes('model='+model[1]+';')""",
                arg=[lane,model],timeout=16000)
            outputs.append({"lane":lane,"model":model,"prompt_chars":len(prompt),"command_words":len(command.split())})
        assert not launches, "Browser triggered an unexpected CLI launch"
        assert not errors, "Browser JS failed: "+repr(errors)
        print("VERIFIED_CARDS",outputs)
        print("LAUNCH_REQUESTS",launches)
        print("BROWSER_JAVASCRIPT_ERRORS",errors)
        browser.close()
if __name__=="__main__":main()
