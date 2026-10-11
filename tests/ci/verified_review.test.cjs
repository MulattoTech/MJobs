'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {eligible, REQUIRED} = require('../../tools/ci/verified_review.cjs');
const SHA = '1'.repeat(40);
function fixture() {
  return {expectedSha: SHA,
    pr: {state: 'open', draft: false, user: {login: 'MulattoTech'}, changed_files: 1,
      head: {sha: SHA, repo: {full_name: 'MulattoTech/MJobs'}},
      base: {ref: 'main', repo: {full_name: 'MulattoTech/MJobs'}}},
    files: [{filename: 'README.md', status: 'modified'}], reviews: [],
    checks: REQUIRED.map((name, id) => ({name, id, head_sha: SHA, app: {id: 15368}, status: 'completed', conclusion: 'success'}))};
}
test('allows an owner documentation PR only after every exact-head gate', () => assert.equal(eligible(fixture()).approved, true));
test('rejects a fork even with green checks', () => {const f=fixture();f.pr.head.repo.full_name='attacker/MJobs';assert.equal(eligible(f).approved,false);});
test('rejects unknown author', () => {const f=fixture();f.pr.user.login='other';assert.equal(eligible(f).approved,false);});
test('rejects draft and closed PRs', () => {for(const key of ['draft','state']){const f=fixture();f.pr[key]=key==='draft'?true:'closed';assert.equal(eligible(f).approved,false);}});
test('rejects missing checks', () => {const f=fixture();f.checks.pop();assert.equal(eligible(f).approved,false);});
test('rejects stale successful checks', () => {const f=fixture();f.checks[0].head_sha='2'.repeat(40);assert.equal(eligible(f).approved,false);});
test('does not mistake another app for GitHub Actions', () => {const f=fixture();f.checks[0].app.id=99;assert.equal(eligible(f).approved,false);});
test('new failing or pending rerun outranks an old green result', () => {for(const conclusion of ['failure','cancelled','skipped',null]){const f=fixture();f.checks.push({...f.checks[0],id:999,conclusion});assert.equal(eligible(f).approved,false);}});
test('rejects changed head', () => {const f=fixture();f.expectedSha='3'.repeat(40);assert.equal(eligible(f).approved,false);});
test('never auto-approves workflow, credential, source or requirement edits', () => {for(const filename of ['.github/workflows/ci.yml','services/credentials/vault.py','apps/web/src/main.tsx','planning/CAPABILITY_MATRIX.json','docs/spec/areas/governance_safety.md','LICENSE','tools/ci/verified_review.cjs']){const f=fixture();f.files[0].filename=filename;assert.equal(eligible(f).approved,false);}});
test('rejects moving sensitive code into an allowed documentation path', () => {const f=fixture();f.files[0].previous_filename='.github/workflows/ci.yml';assert.equal(eligible(f).approved,false);});
test('rejects deleted files and incomplete pagination', () => {let f=fixture();f.files[0].status='removed';assert.equal(eligible(f).approved,false);f=fixture();f.pr.changed_files=2;assert.equal(eligible(f).approved,false);});
test('does not override an unresolved requested-changes review', () => {const f=fixture();f.reviews=[{id:1,state:'CHANGES_REQUESTED',user:{login:'maintainer'}}];assert.equal(eligible(f).approved,false);});
test('dismissed requested changes no longer blocks the policy', () => {const f=fixture();f.reviews=[{id:1,state:'CHANGES_REQUESTED',user:{login:'maintainer'}},{id:2,state:'DISMISSED',user:{login:'maintainer'}}];assert.equal(eligible(f).approved,true);});
