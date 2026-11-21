"""Connected browser acceptance for saved, profile-scoped match explanations.

The workflow renderer and app mount are integrated by the root lane. Keep this
test skipped until its component is mounted and the checked web bundle includes it.
"""

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_profiles import save_search_profile
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PROFILES = {
    "fixture": {
        "id": "fixture", "name": "Synthetic Cloud Profile",
        "email": "cloud-browser@example.test", "contact": {},
        "preferences": {"desired_roles": [], "avoid_terms": []},
        "facts": [], "answers": [],
    },
    "other": {
        "id": "other", "name": "Synthetic Network Profile",
        "email": "network-browser@example.test", "contact": {},
        "preferences": {"desired_roles": [], "avoid_terms": []},
        "facts": [], "answers": [],
    },
}


def _workflow_bundle_is_mounted() -> bool:
    """Avoid browser runs against stale bundles or a not-yet-mounted workflow."""
    main_source = (ROOT / "apps/web/src/main.tsx").read_text(encoding="utf-8")
    api_source = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")
    index = ROOT / "apps/web/dist/index.html"
    bundles = list((ROOT / "apps/web/dist/assets").glob("*.js"))
    if not index.is_file() or not bundles:
        return False
    if "MatchingExplanationWorkflow" not in main_source:
        return False
    if "create_matching_explanations_router" not in api_source:
        return False
    return any("Saved match explanation" in bundle.read_text(
        encoding="utf-8", errors="ignore") for bundle in bundles)


def _profile_scoring(profile_id,version,role_family,weights):
    return {
        "id": profile_id,"version": version,"schema_version": 1,
        "role_family": role_family,"weights": weights,"thresholds": {},
    }


def _resolution(profile_id,version,role_family):
    return {
        "selection": "ROLE_FAMILY","profile_id": profile_id,
        "profile_version": version,"requested_role_family": role_family,
        "selected_role_family": role_family,
    }


def _known_rows(skill_score,title_score,skill_source,skill_evidence,title_evidence):
    score=round((70*skill_score+30*title_score)/100,2)
    return score,[
        {
            "dimension":"skills","weight":70.0,"raw_score":float(skill_score),
            "weighted_contribution":round(70*skill_score/100,2),
            "explanation":f"Synthetic approved skill evidence gives this job a {skill_score} signal.",
            "provenance":{"source":skill_source,"evidence_ref":skill_evidence},
        },
        {
            "dimension":"title","weight":30.0,"raw_score":float(title_score),
            "weighted_contribution":round(30*title_score/100,2),
            "explanation":f"The saved role-family title comparison scored {title_score}.",
            "provenance":{"source":"job_title_match","evidence_ref":title_evidence},
        },
    ]


def _snapshot(store):
    with store.engine.connect() as connection:
        return {
            table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
            for table in m.metadata.sorted_tables
        }


@unittest.skipUnless(
    _workflow_bundle_is_mounted(),
    "Root must mount MatchingExplanationWorkflow and rebuild apps/web before Chromium acceptance",
)
class MatchingExplanationWorkflowBrowserTests(unittest.TestCase):
    def test_saved_match_explanation_scope_stale_retry_and_read_only(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-match-explanation-browser-") as directory:
            root=Path(directory)
            store=Store(f"sqlite:///{(root / 'matching-explanation.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            for profile in FIXTURE_PROFILES.values():
                store.save_profile(deepcopy(profile))
            fixture_version=store.profile("fixture")["version"]
            other_version=store.profile("other")["version"]

            save_search_profile(store,{
                "id":"cloud-search","profile_id":"fixture","name":"Cloud search",
                "enabled":False,"include_titles":[
                    "Cloud Systems Engineer","Platform Cloud Engineer",
                    "Cloud Reliability Engineer",
                ],
                "exclude_titles":[],"required_keywords":[],
                "optional_keywords":[],"excluded_keywords":[],"exact_phrases":[],
                "technology_aliases":{},"remote":"ANY","employment_types":[],"source_ids":[],
            })
            save_search_profile(store,{
                "id":"network-search","profile_id":"other","name":"Network search",
                "enabled":False,"include_titles":["Network Operations Engineer"],
                "exclude_titles":[],"required_keywords":["routing"],
                "optional_keywords":[],"excluded_keywords":[],"exact_phrases":[],
                "technology_aliases":{},"remote":"ANY","employment_types":[],"source_ids":[],
            })

            cloud_config=_profile_scoring(
                "cloud-scoring-profile","cloud-scoring-v2","Cloud Operations",
                {"skills":70.0,"title":30.0,"ai_intensity":20.0},
            )
            network_config=_profile_scoring(
                "network-scoring-profile","network-scoring-v1","Network Operations",
                {"skills":70.0,"title":30.0,"ai_intensity":20.0},
            )
            with store.engine.begin() as connection:
                for search_id,config in (("cloud-search",cloud_config),
                                         ("network-search",network_config)):
                    search=connection.execute(select(m.search_profiles).where(
                        m.search_profiles.c.id==search_id,
                    )).mappings().one()
                    criteria=dict(search["criteria"])
                    criteria["role_scoring_profile"]=config
                    connection.execute(update(m.search_profiles).where(
                        m.search_profiles.c.id==search_id,
                    ).values(criteria=criteria))
                connection.execute(m.companies.insert().values(
                    id="synthetic-employer",name="Synthetic Infrastructure Co",
                    domain="synthetic.example.test",
                ))
                jobs=(
                    ("cloud-one","Cloud Systems Engineer","Cloud systems and Linux operations"),
                    ("cloud-two","Platform Cloud Engineer","Platform services and containers"),
                    ("cloud-stale","Cloud Reliability Engineer","Cloud reliability and operations"),
                    ("network-one","Network Operations Engineer","Routing and network operations"),
                )
                for job_id,title,description in jobs:
                    connection.execute(m.jobs.insert().values(
                        id=job_id,company_id="synthetic-employer",requisition=f"fixture-{job_id}",
                        title=title,description=description,skills=["Linux"],
                        location="Remote",remote_type="REMOTE",attributes={"fictional":True},
                    ))

                    connection.execute(m.routes.insert().values(
                        id=f"route-{job_id}", job_id=job_id,
                        company_id="synthetic-employer", provider="GREENHOUSE",
                        tenant="synthetic-match-explanation", requisition=f"fixture-{job_id}",
                        url=f"https://match-explanation.example.test/apply/{job_id}",
                    ))

                score,rows=_known_rows(80,90,"approved_profile_skills","fixture:cloud-linux",
                                       "fixture:cloud-title")
                connection.execute(m.matches.insert().values(
                    id="match-cloud-one",job_id="cloud-one",profile_id="fixture",
                    profile_version=fixture_version,score=score,missing=[],
                    components={
                        "matched":["Linux"],"fit_factors":[],"missing":[],
                        "scoring_profile":_resolution("cloud-scoring-profile","cloud-scoring-v2","Cloud Operations"),
                        "scoring_contributions":rows,
                    },
                ))

                score,rows=_known_rows(40,50,"approved_profile_skills","fixture:platform-skill",
                                       "fixture:platform-title")
                connection.execute(m.matches.insert().values(
                    id="match-cloud-two",job_id="cloud-two",profile_id="fixture",
                    profile_version=fixture_version,score=score,missing=[],
                    components={
                        "matched":["Platform services"],"fit_factors":[],"missing":[],
                        "scoring_profile":_resolution("cloud-scoring-profile","cloud-scoring-v2","Cloud Operations"),
                        "scoring_contributions":rows,
                    },
                ))

                score,rows=_known_rows(70,70,"approved_profile_skills","fixture:stale-skill",
                                       "fixture:stale-title")
                connection.execute(m.matches.insert().values(
                    id="match-cloud-stale",job_id="cloud-stale",profile_id="fixture",
                    profile_version=fixture_version,score=score,missing=[],
                    components={
                        "matched":["Cloud reliability"],"fit_factors":[],"missing":[],
                        "scoring_profile":_resolution("cloud-scoring-profile","old-scoring-version","Cloud Operations"),
                        "scoring_contributions":rows,
                    },
                ))

                score,rows=_known_rows(65,75,"approved_profile_skills","other:network-routing",
                                       "other:network-title")
                connection.execute(m.matches.insert().values(
                    id="match-network-one",job_id="network-one",profile_id="other",
                    profile_version=other_version,score=score,missing=[],
                    components={
                        "matched":["Routing"],"fit_factors":[],"missing":[],
                        "scoring_profile":_resolution("network-scoring-profile","network-scoring-v1","Network Operations"),
                        "scoring_contributions":rows,
                    },
                ))

            app=create_app(
                store,"http://127.0.0.1:1",root,"redis://127.0.0.1:1/0",
                operation_mode="FIXTURE",
            )
            sock=bound_browser_socket()
            origin=f"http://127.0.0.1:{sock.getsockname()[1]}"
            server=uvicorn.Server(uvicorn.Config(app,log_level="error"))
            thread=threading.Thread(target=server.run,kwargs={"sockets":[sock]},daemon=True)
            explanation_requests=[]
            blocked_writes=[]
            external_requests=[]
            ai_requests=[]
            submission_requests=[]
            rescore_requests=[]
            expected_visits=[
                ("fixture","cloud-one",1),
                ("fixture","cloud-two",2),
                ("fixture","cloud-stale",3),
                ("other","network-one",1),
            ]
            expected_view_urls=[
                f"{origin}/profiles/{profile_id}/jobs/{job_id}/view"
                for profile_id,job_id,_ in expected_visits
            ]
            view_requests=[]
            view_receipts={}

            def read_visits():
                with store.engine.connect() as connection:
                    return {row["id"]:dict(row) for row in connection.execute(
                        select(m.profile_job_views)).mappings()}

            def assert_visit_receipt(response,index):
                self.assertEqual(response.status,201)
                receipt=response.json()
                profile_id,job_id,_=expected_visits[index]
                self.assertEqual((receipt["profile_id"],receipt["job_id"]),(profile_id,job_id))
                self.assertIsInstance(receipt["viewed_at"],str)
                self.assertIsNotNone(datetime.fromisoformat(receipt["viewed_at"]).tzinfo)
                view_receipts[(profile_id,job_id)]=receipt["viewed_at"]
                visits=read_visits()
                self.assertEqual(sorted(
                    (row["profile_id"],row["job_id"],row["visit_order"])
                    for row in visits.values()
                ),sorted(expected_visits[:index+1]))
                for row in visits.values():
                    self.assertEqual(row["viewed_at"],
                                     view_receipts[(row["profile_id"],row["job_id"])])
                return visits

            cloud_two_attempts=0
            try:
                thread.start()
                deadline=time.monotonic()+10
                while not server.started:
                    if not thread.is_alive() or time.monotonic()>deadline:
                        self.fail("Isolated matching API did not start")
                    time.sleep(0.02)
                database_before_browser=_snapshot(store)
                database_after_refresh=None
                view_table=m.profile_job_views.name
                self.assertEqual(database_before_browser[view_table],[])

                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True)
                    context=browser.new_context()

                    def read_only_fixture_route(route):
                        nonlocal cloud_two_attempts
                        request=route.request
                        parsed=urlsplit(request.url)
                        path=parsed.path.lower()
                        if parsed.netloc!=urlsplit(origin).netloc:
                            external_requests.append(request.url)
                            route.abort()
                        elif any(token in path for token in (
                            "/inference","/generate","/completions","/chat/","/ai/",
                        )):
                            ai_requests.append((request.method,request.url))
                            route.abort()
                        elif any(token in path for token in (
                            "/submit","/mock-run","/applications/submit","/apply",
                        )):
                            submission_requests.append((request.method,request.url))
                            route.abort()
                        elif path=="/matches" and request.method=="POST":
                            rescore_requests.append(request.post_data_json)
                            route.fulfill(status=202,json={
                                "operation_id":"synthetic-role-score-refresh",
                                "state":"QUEUED",
                            })
                        elif path=="/operations/synthetic-role-score-refresh" and request.method=="GET":
                            with store.engine.begin() as connection:
                                stale_match=connection.execute(select(m.matches).where(
                                    m.matches.c.job_id=="cloud-stale",
                                    m.matches.c.profile_id=="fixture",
                                )).mappings().one()
                                components=dict(stale_match["components"])
                                saved_profile=dict(components["scoring_profile"])
                                saved_profile["profile_version"]="cloud-scoring-v2"
                                components["scoring_profile"]=saved_profile
                                connection.execute(update(m.matches).where(
                                    m.matches.c.id==stale_match["id"],
                                ).values(components=components))
                            route.fulfill(json={
                                "id":"synthetic-role-score-refresh","kind":"DISCOVER",
                                "state":"COMPLETED","attempt_count":1,"error":None,
                            })
                        elif path.endswith("/match-explanation"):
                            explanation_requests.append((request.method,request.url))
                            if path.endswith("/cloud-two/match-explanation") and cloud_two_attempts==0:
                                cloud_two_attempts+=1
                                route.fulfill(status=503,json={"detail":"Synthetic retry fixture"})
                            else:
                                route.continue_()
                        elif (request.method=="POST"
                              and len(view_requests)<len(expected_view_urls)
                              and request.url==expected_view_urls[len(view_requests)]
                              and request.post_data in (None,"")):
                            # Only these explicit card opens may record visits.
                            view_requests.append(request.url)
                            route.continue_()
                        elif request.method not in {"GET","HEAD","OPTIONS"}:
                            blocked_writes.append((request.method,request.url))
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*",read_only_fixture_route)
                    page=context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin,wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile",exact=True)).to_have_value("fixture")

                    saved_search=page.get_by_label("Saved search profile",exact=True)
                    with page.expect_response(lambda response:
                        urlsplit(response.url).path=="/jobs"
                        and "search_profile_id=cloud-search" in response.url
                    ) as initial_search:
                        saved_search.select_option("cloud-search")
                    self.assertEqual(parse_qs(urlsplit(initial_search.value.url).query)
                                     .get("search_profile_id"),["cloud-search"])

                    explanation=page.locator(".job-detail section.panel").filter(has=page.get_by_role(
                        "heading",name=re.compile("match explanation",re.IGNORECASE)))
                    self.assertEqual(read_visits(),{})
                    with page.expect_response(lambda response:
                        urlsplit(response.url).path.endswith("/cloud-one/match-explanation")
                    ) as first_explanation, page.expect_response(lambda response:
                        response.request.method=="POST" and response.url==expected_view_urls[0]
                    ) as first_view:
                        page.locator(".job-card").filter(has_text="Cloud Systems Engineer").click()
                    assert_visit_receipt(first_view.value,0)
                    self.assertEqual(first_explanation.value.status,200)
                    first_body=first_explanation.value.json()
                    self.assertEqual(first_body["profile_id"],"fixture")
                    self.assertEqual(first_body["search_profile_id"],"cloud-search")
                    self.assertEqual(first_body["job_id"],"cloud-one")
                    self.assertEqual(first_body["score"],83.0)
                    expect(explanation).to_be_visible()
                    expect(explanation).to_contain_text("83")
                    expect(explanation).to_contain_text("Cloud Operations")
                    expect(explanation).to_contain_text("cloud-scoring-profile")
                    expect(explanation).to_contain_text("cloud-scoring-v2")
                    expect(explanation).to_contain_text("approved_profile_skills")
                    expect(explanation).to_contain_text("fixture:cloud-linux")
                    expect(explanation).to_contain_text("Synthetic approved skill evidence gives this job a 80 signal.")
                    expect(explanation).to_contain_text("UNKNOWN")
                    expect(explanation).to_contain_text("ai_intensity is UNKNOWN")

                    with page.expect_response(lambda response:
                        urlsplit(response.url).path.endswith("/cloud-two/match-explanation")
                    ) as transient_failure, page.expect_response(lambda response:
                        response.request.method=="POST" and response.url==expected_view_urls[1]
                    ) as second_view:
                        page.locator(".job-card").filter(has_text="Platform Cloud Engineer").click()
                    views_before_retry=assert_visit_receipt(second_view.value,1)
                    self.assertEqual(transient_failure.value.status,503)
                    expect(explanation.get_by_role("alert")).to_be_visible()
                    expect(explanation).not_to_contain_text("fixture:cloud-linux")
                    expect(explanation).not_to_contain_text("cloud-scoring-profile")
                    retry=explanation.get_by_role("button",name=re.compile("retry",re.IGNORECASE))
                    expect(retry).to_be_visible()
                    with page.expect_response(lambda response:
                        urlsplit(response.url).path.endswith("/cloud-two/match-explanation")
                    ) as recovered_explanation:
                        retry.click()
                    self.assertEqual(recovered_explanation.value.status,200)
                    self.assertEqual(recovered_explanation.value.json()["score"],43.0)
                    expect(explanation).to_contain_text("43")
                    expect(explanation).to_contain_text("fixture:platform-skill")
                    expect(explanation).not_to_contain_text("fixture:cloud-linux")
                    expect(explanation).not_to_contain_text("83")
                    self.assertEqual(read_visits(),views_before_retry,
                                     "Retrying an explanation must not record another visit")

                    with page.expect_response(lambda response:
                        urlsplit(response.url).path.endswith("/cloud-stale/match-explanation")
                    ) as stale_response, page.expect_response(lambda response:
                        response.request.method=="POST" and response.url==expected_view_urls[2]
                    ) as third_view:
                        page.locator(".job-card").filter(has_text="Cloud Reliability Engineer").click()
                    views_before_refresh=assert_visit_receipt(third_view.value,2)
                    self.assertEqual(stale_response.value.status,409)
                    expect(explanation.get_by_role("alert")).to_contain_text("out of date")
                    refresh_matches=page.get_by_role("button",name="Refresh profile matches",exact=True)
                    expect(refresh_matches).to_be_visible()
                    expect(explanation).not_to_contain_text("fixture:platform-skill")
                    expect(explanation).not_to_contain_text("43")

                    with page.expect_request(lambda request:
                        request.method=="POST" and urlsplit(request.url).path=="/matches"
                    ) as rescore_request:
                        with page.expect_response(lambda response:
                            response.request.method=="GET"
                            and urlsplit(response.url).path.endswith("/cloud-stale/match-explanation")
                        ) as recovered_stale_response:
                            refresh_matches.click()
                    self.assertEqual(rescore_request.value.post_data_json["profile_id"],"fixture")
                    self.assertEqual(recovered_stale_response.value.status,200)
                    expect(explanation).to_contain_text("70")
                    self.assertEqual(len(rescore_requests),1)
                    database_after_refresh=_snapshot(store)
                    match_table=m.matches.name
                    before_matches={row["id"]:row for row in database_before_browser[match_table]}
                    after_matches={row["id"]:row for row in database_after_refresh[match_table]}
                    changed_matches=sorted(
                        match_id for match_id in before_matches
                        if before_matches[match_id]!=after_matches[match_id]
                    )
                    self.assertEqual(changed_matches,["match-cloud-stale"])
                    self.assertEqual(
                        before_matches["match-cloud-stale"]["components"]["scoring_profile"]["profile_version"],
                        "old-scoring-version",
                    )
                    self.assertEqual(
                        after_matches["match-cloud-stale"]["components"]["scoring_profile"]["profile_version"],
                        "cloud-scoring-v2",
                    )
                    for table_name in database_before_browser:
                        if table_name==view_table:
                            self.assertEqual(
                                {row["id"]:row for row in database_after_refresh[view_table]},
                                views_before_refresh,
                                "Refreshing a match must not mutate recorded visits",
                            )
                        elif table_name!=match_table:
                            self.assertEqual(
                                database_after_refresh[table_name],database_before_browser[table_name],
                                f"Refreshing a match must not mutate {table_name}",
                            )

                    page.get_by_label("Active profile",exact=True).select_option("other")
                    expect(page.get_by_label("Active profile",exact=True)).to_have_value("other")
                    other_search=page.get_by_label("Saved search profile",exact=True)
                    other_search.select_option("network-search")
                    with page.expect_response(lambda response:
                        urlsplit(response.url).path.endswith("/network-one/match-explanation")
                    ) as other_profile_response, page.expect_response(lambda response:
                        response.request.method=="POST" and response.url==expected_view_urls[3]
                    ) as other_view:
                        page.locator(".job-card").filter(has_text="Network Operations Engineer").click()
                    views_after_other=assert_visit_receipt(other_view.value,3)
                    self.assertEqual(
                        {key:row for key,row in views_after_other.items() if row["profile_id"]=="fixture"},
                        views_before_refresh,
                        "Opening another profile's job must preserve the first profile's visits",
                    )
                    self.assertEqual(other_profile_response.value.status,200)
                    other_body=other_profile_response.value.json()
                    self.assertEqual(other_body["profile_id"],"other")
                    self.assertEqual(other_body["search_profile_id"],"network-search")
                    self.assertEqual(other_body["score"],68.0)
                    expect(explanation).to_contain_text("Network Operations")
                    expect(explanation).to_contain_text("network-scoring-profile")
                    expect(explanation).not_to_contain_text("Cloud Operations")
                    expect(explanation).not_to_contain_text("fixture:cloud-linux")
                    expect(explanation).not_to_contain_text("fixture:platform-skill")

                    self.assertEqual(cloud_two_attempts,1)
                    self.assertGreaterEqual(len(explanation_requests),6)
                    self.assertEqual(view_requests,expected_view_urls)
                    self.assertEqual(blocked_writes,[])
                    self.assertEqual(external_requests,[])
                    self.assertEqual(ai_requests,[])
                    self.assertEqual(submission_requests,[])
                    browser.close()

                database_after_browser=_snapshot(store)
                for table_name in database_after_refresh:
                    if table_name==view_table:
                        self.assertEqual(
                            {row["id"]:row for row in database_after_browser[view_table]},views_after_other,
                            "Reading explanations must not mutate the four recorded visits",
                        )
                    else:
                        self.assertEqual(database_after_browser[table_name],database_after_refresh[table_name],
                                         f"Reading score explanations must not mutate {table_name}")
            finally:
                server.should_exit=True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(),"Isolated matching API did not stop")


if __name__=="__main__":
    unittest.main()
