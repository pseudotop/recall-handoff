"""Actual Chromium + MCP + optional local-model smoke. All saved actions are demo records."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[3]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume-result", type=Path)
    parser.add_argument("--pace-ms", type=int, default=0, help="Reading pauses for a demo recording, excluded from model latency")
    parser.add_argument("--browser-executable", type=Path,
                        help="Optional existing Chromium/Chrome executable; otherwise use Playwright's installed browser")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    from playwright.sync_api import sync_playwright
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic(); console_errors = []
    with sync_playwright() as playwright:
        launch_options = {"headless": True}
        if args.browser_executable is not None:
            launch_options["executable_path"] = str(args.browser_executable)
        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context(viewport={"width": 1280, "height": 900},
                   record_video_dir=str(args.output / "video"), record_video_size={"width": 1280, "height": 900})
        page = context.new_page(); page.on("pageerror", lambda error: console_errors.append(str(error)))
        page.goto(args.url, wait_until="networkidle")
        page.screenshot(path=str(args.output / "start.png"), full_page=True)
        page.wait_for_timeout(args.pace_ms)
        if args.resume_result:
            original = json.loads(args.resume_result.read_text())
            page.locator("#resume-id").fill(original["demo_case_id"])
            page.locator("#resume button").click()
            page.wait_for_function("current !== null && current.status === 'USER_REPORTED_STOPPED'")
            resumed = page.evaluate("current")
            if resumed != original["final_demo_case"]: raise AssertionError("Restart changed the saved review")
            result = {"status": "BROWSER_MCP_RESTART_RESUME_PASS", "saved_case_unchanged": True,
                      "demo_case_id": resumed["id"], "model_required_to_resume": False}
        else:
            page.locator("#search-button").click()
            page.wait_for_function("snapshot !== null && document.querySelector('#search-button').disabled === false", timeout=90000)
            found = page.evaluate("snapshot")
            page.wait_for_timeout(args.pace_ms)
            card = page.locator("#notices article").filter(has_text="Model text appears").first
            card.locator("input[type=checkbox]").check()
            card.locator("button").click()
            page.wait_for_function("current !== null")
            saved = page.evaluate("current")
            page.wait_for_timeout(args.pace_ms)
            if saved["status"] != "REVIEW_OPEN": raise AssertionError("Search/save did not open a review")
            with page.expect_response(lambda response: response.url.endswith("/api/explain"), timeout=120000) as response:
                page.locator("#explain").click()
            model_result = response.value.json()
            page.locator("#explanation blockquote").wait_for(timeout=5000)
            if model_result.get("status") != "MODEL_RANKED_EXCERPT": raise AssertionError("No model-ranked excerpt")
            page.wait_for_function("busy === false")
            page.locator("#review").screenshot(path=str(args.output / "review-with-model.png"))
            page.wait_for_timeout(args.pace_ms)
            denied = page.evaluate("""async()=>{const r=await fetch('/api/tool',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'record_action',arguments:{}})});return r.status;}""")
            if denied != 403: raise AssertionError("A request without the local app token was not rejected")
            page.locator("#action-note").fill("DEVELOPMENT DEMO: inspected the public notice text; no owned product or manufacturer action.")
            page.locator("#action-confirm").check(); page.locator("#record").click()
            page.wait_for_function("current.steps.notice_conditions_checked === true && busy === false")
            page.wait_for_timeout(args.pace_ms)
            page.locator("#action-note").fill("DEVELOPMENT DEMO stopped; no remedy request or manufacturer contact.")
            page.locator("#action-confirm").check(); page.locator("#stop").click()
            page.wait_for_function("current.status === 'USER_REPORTED_STOPPED' && busy === false")
            page.wait_for_timeout(args.pace_ms)
            final_case = page.evaluate("current")
            if final_case["steps"]["remedy_request_recorded"]: raise AssertionError("The demo invented a remedy request")
            result = {"status": "BROWSER_MCP_LOCAL_MODEL_PASS", "demo_case_id": saved["id"],
                      "query_status": found["query_status"], "query_sources": found["query_sources"],
                      "query_errors": found["query_errors"], "candidate_count": len(found["candidates"]),
                      "selected_notice_url": saved["notice"]["URL"], "model_result": model_result,
                      "missing_app_token_rejected": denied == 403,
                      "closed_without_remedy_request": True, "final_demo_case": final_case,
                      "physical_product_verification": "NOT_RUN", "alexa_connection": "NOT_RUN"}
        if console_errors: raise AssertionError(console_errors)
        result.update(checked_at_utc=datetime.now(timezone.utc).isoformat(), elapsed_sec=round(time.monotonic()-started, 3),
                      scope="One author-driven local browser development smoke; no customer, manufacturer, Alexa or independent accuracy validation",
                      source_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted((ROOT/'competitions/amazon-build-ship-shape2026/src').glob('*')) if p.is_file()})
        page.screenshot(path=str(args.output / "finished.png"), full_page=True)
        video = page.video; context.close(); video.save_as(str(args.output / "actual-workflow.webm")); browser.close()
    (args.output/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({"status":result['status'],"elapsed_sec":result['elapsed_sec'],"output":str(args.output)}))


if __name__ == "__main__": main()
