'use strict';
// Machine-verification reviews, not human or semantic code-review claims.
// Called only from trusted default-branch code; never execute PR files/artifacts.
const REPO = 'MulattoTech/MJobs';
const REQUIRED = [
  'CI / required', 'Integration / PostgreSQL and Redis',
  'Disposable Compose / mock end-to-end',
  'Dependencies / introduced vulnerabilities', 'Security / secret scan',
];
const SAFE_DOCS = /^(?:README\.md|docs\/(?:INSTALLATION\.md|DEVELOPMENT\.md|ARCHITECTURE\.md|assets\/[A-Za-z0-9_.-]+\.(?:png|svg|webp)|maintainers\/[A-Za-z0-9_.-]+\.md))$/;

function eligible({pr, files, checks, reviews = [], expectedSha}) {
  const deny = reason => ({approved: false, reason});
  if (!pr || pr.state !== 'open' || pr.draft) return deny('PR must be open and ready');
  if (pr.user?.login !== 'MulattoTech') return deny('Only owner-authored documentation is in the automatic-review allowlist');
  if (pr.base?.repo?.full_name !== REPO || pr.head?.repo?.full_name !== REPO || pr.base?.ref !== 'main') return deny('Not a same-repository main PR');
  if (!/^[a-f0-9]{40}$/.test(pr.head?.sha || '') || pr.head.sha !== expectedSha) return deny('Head changed or invalid');
  if (!Array.isArray(files) || !files.length || files.length > 25 || pr.changed_files !== files.length) return deny('Incomplete or oversized changed-file listing');
  if (files.some(f => !SAFE_DOCS.test(f.filename || '') || (f.previous_filename && !SAFE_DOCS.test(f.previous_filename)) || f.status === 'removed')) return deny('Non-documentation, renamed-sensitive or deleted file requires ordinary review');
  const decisions = new Map();
  for (const r of [...reviews].sort((a, b) => a.id - b.id)) {
    if (['APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'].includes(r.state)) decisions.set(r.user?.login, r.state);
  }
  if ([...decisions.values()].includes('CHANGES_REQUESTED')) return deny('An unresolved requested-changes review exists');
  for (const name of REQUIRED) {
    const latest = checks.filter(c => c.name === name && c.app?.id === 15368 && c.head_sha === expectedSha)
      .sort((a, b) => b.id - a.id)[0];
    if (!latest || latest.status !== 'completed' || latest.conclusion !== 'success') return deny(`Required current GitHub Actions check is not successful: ${name}`);
  }
  return {approved: true, reason: 'Owner-authored documentation; all exact-head integration gates succeeded'};
}

async function run({github, context, core}) {
  const {owner, repo} = context.repo;
  if (`${owner}/${repo}` !== REPO || context.ref !== 'refs/heads/main') throw new Error('Trusted main context required');
  const event = context.payload;
  let numbers = [];
  if (context.eventName === 'workflow_dispatch') {
    if (context.actor !== 'MulattoTech') throw new Error('Manual verification dispatch requires repository owner');
    const n = String(process.env.PR_NUMBER || '');
    if (!/^[1-9][0-9]*$/.test(n)) throw new Error('Invalid PR number');
    numbers = [Number(n)];
  } else if (context.eventName === 'workflow_run') {
    if (event.workflow_run?.conclusion !== 'success' || event.workflow_run?.repository?.full_name !== REPO) return;
    const open = await github.paginate(github.rest.pulls.list, {owner, repo, state: 'open', base: 'main', per_page: 100});
    numbers = open.filter(pr => pr.head.sha === event.workflow_run.head_sha && pr.head.repo?.full_name === REPO).map(pr => pr.number);
  } else throw new Error('Unsupported event');
  for (const pull_number of numbers) {
    const {data: pr} = await github.rest.pulls.get({owner, repo, pull_number});
    const expectedSha = pr.head.sha;
    const files = await github.paginate(github.rest.pulls.listFiles, {owner, repo, pull_number, per_page: 100});
    const response = await github.paginate(github.rest.checks.listForRef, {owner, repo, ref: expectedSha, per_page: 100});
    const reviews = await github.paginate(github.rest.pulls.listReviews, {owner, repo, pull_number, per_page: 100});
    const decision = eligible({pr, files, checks: response, reviews, expectedSha});
    core.info(`PR #${pull_number}: ${decision.reason}`);
    if (!decision.approved || reviews.some(r => r.user?.login === 'github-actions[bot]' && r.commit_id === expectedSha && r.state === 'APPROVED')) continue;
    const {data: fresh} = await github.rest.pulls.get({owner, repo, pull_number});
    if (fresh.head.sha !== expectedSha || fresh.state !== 'open' || fresh.draft) continue;
    await github.rest.pulls.createReview({owner, repo, pull_number, commit_id: expectedSha, event: 'APPROVE',
      body: `Automated verification-only approval for ${expectedSha}. This owner-authored documentation change passed all five current integration gates. This is not a human review, semantic code review, security certification, or product-release acceptance. Repository merge protections remain authoritative.`});
  }
}
module.exports = {eligible, run, REQUIRED};
