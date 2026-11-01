"""One bounded GET-only observation per public source; no application APIs."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--repo-root', type=Path, required=True)
parser.add_argument('--evidence', type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.repo_root))
from packages.persistence.store import identity
from services.ats_detection.detector import detect_ats
from services.company_discovery.public_client import PublicCompanyClient
from services.dedupe.policy import duplicate_confidence
from services.discovery.sources.registry import source_adapter

class Budget:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds
    def __call__(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('bounded live probe deadline')
    def remaining_seconds(self):
        self()
        return self.deadline - time.monotonic()

report = {'revision': subprocess.check_output(['git','rev-parse','HEAD'], cwd=args.repo_root, text=True).strip(),
          'observed_at': datetime.now(timezone.utc).isoformat(),
          'mode': 'PUBLIC_READ_ONLY', 'external_submission_requests': 0,
          'sources': [], 'company': None}
for provider, tenant in [('greenhouse','greenhouse'),('remotive','default'),('wwr','default'),('remoteok','default')]:
    start=time.monotonic()
    record={'provider':provider,'tenant':tenant,'result':'FAIL'}
    try:
        source=source_adapter({'provider':provider,'tenant':tenant,'request_timeout':6,'rate_limit':60,'concurrency':1},check=Budget(20))
        batch=source.search() if provider=='greenhouse' else source.search(limit=3)
        assert batch.items, 'No public jobs returned'
        examples=[]
        for item in batch.items[:3]:
            job=source.normalize(item.raw)
            assert job['title'] and job['requisition'] and job['description']
            assert item.company.name and item.company.identity_parts
            cid=identity(*item.company.identity_parts)
            candidate={**job,'company_id':cid,'company':{'known_identity_ids':[cid],'name':item.company.name},
                       'source':batch.endpoint,'source_job_id':job['requisition']}
            verdict=duplicate_confidence(candidate, candidate)
            assert verdict['disposition']=='AUTO_MERGE_ELIGIBLE', verdict['reason']
            assert job['attributes']['submission_capability']=='MANUAL_ONLY'
            examples.append({'id':job['requisition'],'title':job['title'],'company':item.company.name,
                             'normalization':'PASS','same_posting_dedupe':verdict['reason'],
                             'raw_sha256':hashlib.sha256(json.dumps(item.raw,sort_keys=True).encode()).hexdigest(),
                             'application_url_available':bool(job['application_url'])})
        assert source.health()=='HEALTHY'
        record.update(result='PASS',endpoint=batch.endpoint,jobs_returned=len(batch.items),
                      examples=examples,health=source.health())
    except Exception as error:
        record.update(error_type=type(error).__name__,error_code=getattr(error,'code',str(error))[:150])
    record['duration_seconds']=round(time.monotonic()-start,2)
    report['sources'].append(record)
    print(json.dumps(record),flush=True)

career='https://job-boards.greenhouse.io/greenhouse'
record={'career_url':career,'result':'FAIL'}
try:
    client=PublicCompanyClient(career,total_timeout=20)
    try:
        response=client.get(career)
    finally:
        client.close()
    detection=detect_ats(str(response.url),response.text,redirect_chain=[str(row.url) for row in response.history])
    assert detection.status=='MATCH' and detection.provider=='greenhouse'
    inventory=next(row for row in report['sources'] if row['provider']=='greenhouse')
    assert inventory['result']=='PASS' and inventory['jobs_returned']>0
    record.update(result='PASS',status=response.status_code,provider=detection.provider,
                  evidence=list(detection.evidence),jobs_enumerated=inventory['jobs_returned'],
                  page_sha256=hashlib.sha256(response.content).hexdigest())
except Exception as error:
    record.update(error_type=type(error).__name__,error_code=getattr(error,'code',str(error))[:150])
report['company']=record
print(json.dumps(record),flush=True)
path=args.evidence
path.write_text(json.dumps(report,indent=2)+'\n')
print(path,flush=True)
