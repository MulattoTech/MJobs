"""Assemble review evidence in scratch without promoting canonical acceptance."""
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
import ast
import hashlib
import json
import re
import subprocess

ROOT = Path('/workspace/scratch/05fe891fa40c')
REPO = ROOT / 'MJobs'
HEAD = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
TRACKED = set(subprocess.check_output(
    ['git', 'ls-tree', '-r', '--name-only', HEAD], cwd=REPO, text=True).splitlines())
REVIEW_NAMES = (
    'acceptance-review-operations.json', 'acceptance-review-operations-2.json',
    'acceptance-review-data-search.json', 'acceptance-review-docs.json',
    'acceptance-review-harness.json', 'acceptance-review-live.json',
)
FENCE = chr(96) * 3


@lru_cache(maxsize=None)
def source_bytes(path):
    return subprocess.check_output(['git', 'show', HEAD + ':' + path], cwd=REPO)


def literal_text(row):
    body = row['body'].split('## Authoritative requirement text\n\n', 1)[1]
    body = body.split('\n## Delivery contract', 1)[0].rstrip()
    return body.removesuffix('\n\n---').rstrip()


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for part in value.values():
            yield from strings(part)
    elif isinstance(value, (list, tuple)):
        for part in value:
            yield from strings(part)


def referenced_paths(value):
    text = '\n'.join(strings(value))
    return {path for path in TRACKED if path in text}


def is_test(path):
    return ((path.startswith('tests/') and Path(path).name.startswith('test_')
             and path.endswith('.py'))
            or (path.startswith('apps/web/') and '.test.' in path)
            or path == 'apps/web/scripts/quality-checks.mjs')


@lru_cache(maxsize=None)
def python_nodes(path):
    if not path.endswith('.py') or not is_test(path):
        return {}
    result = {}

    def visit(items, scope=()):
        for item in items:
            if isinstance(item, ast.ClassDef):
                visit(item.body, (*scope, item.name))
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name.startswith('test_'):
                    result.setdefault(item.name, []).append(
                        path + '::' + '::'.join((*scope, item.name)))

    tree = ast.parse(source_bytes(path))
    visit(tree.body)
    if path == 'tests/integration/test_database_indexes_sustain_165.py':
        # pytest collects these inherited unittest methods on both concrete
        # classes, never on the plain contract mixin itself.
        classes = {item.name: item for item in tree.body if isinstance(item, ast.ClassDef)}
        concrete = ('DatabaseIndexSQLiteTests', 'DatabaseIndexPostgreSQLTests')
        for name in concrete:
            assert any(isinstance(base, ast.Name) and base.id == 'DatabaseIndexContractMixin'
                       for base in classes[name].bases)
        for method in classes['DatabaseIndexContractMixin'].body:
            if isinstance(method, ast.FunctionDef) and method.name.startswith('test_'):
                mixin_node = path + '::DatabaseIndexContractMixin::' + method.name
                assert mixin_node in result[method.name]
                result[method.name] = [node for node in result[method.name] if node != mixin_node]
                result[method.name].extend(path + '::' + name + '::' + method.name
                                           for name in concrete)
    return result


def exact_test_nodes(value, test_paths):
    names = set(re.findall(r'\btest_[A-Za-z0-9_]+\b', '\n'.join(strings(value))))
    return sorted({node for path in test_paths
                   for name, nodes in python_nodes(path).items() if name in names
                   for node in nodes})


input_manifest = {}


def read_review(name):
    path = ROOT / name
    data = path.read_bytes()
    input_manifest[name] = {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest()}
    return json.loads(data)


notes = json.loads((ROOT / 'p1_acceptance_notes.json').read_text())
seed = {row['id']: row for row in map(json.loads,
        source_bytes('planning/ISSUE_SEED.jsonl').decode().splitlines())}
matrix = json.loads(source_bytes('planning/CAPABILITY_MATRIX.json'))
before = {row['requirement_id']: row for row in matrix['requirements']}
review_documents = {name: read_review(name) for name in REVIEW_NAMES}
final_claims = read_review('acceptance-review-final-claims.json')
deadline_gaps = read_review('acceptance-review-ui-deadline-gaps.json')
wwr_followup = read_review('remote-harness-wwr-live-followup.json')
live = review_documents['acceptance-review-live.json']
live_rows = {row['id']: row for row in live['requirements']}
live_228_ready = live_rows['MJ-REQ-228']['verdict'].startswith('READY_FOR_FULL_ACCEPTANCE')
partial_ids = {'MJ-REQ-254'} | (set() if live_228_ready else {'MJ-REQ-228'})

notes['MJ-REQ-229'] = (
    'The official Greenhouse careers page returned HTTP 200 through bounded public reads; '
    'production ATS detection and structural board links identified the greenhouse tenant. '
    'That tenant is explicitly bound to the separately recorded 19-job public inventory '
    'approximately seven minutes earlier. The actual a2a1e4e2/e6963812 observations and '
    'code-identical source bridge are preserved; no application was submitted. The earlier '
    'career-board REDIRECT_BLOCKED attempt remains failed historical evidence. Full acceptance '
    'is contingent on final source integration and required hosted gates.'
)
if live_228_ready:
    notes['MJ-REQ-228'] = (
        'Three real sources—Remotive, RemoteOK and Greenhouse—have bounded live-capture '
        'evidence followed by production collection, normalization, dedupe, company extraction '
        'and source-health checks against the exact captured responses, with two collections '
        'and two database reopens. No application was submitted. WWR remains a failed source '
        'observation: 99 captured entries contained 98 identities and one conflict, and production '
        'collection failed in PERSIST_1 with SOURCE_DUPLICATE_JOB_ID. Both WWR failures remain '
        'preserved; captured counts are not persisted inventory. Acceptance of the several-source '
        'test requirement does not claim WWR is repaired or that every public source works.'
    )
else:
    notes['MJ-REQ-228'] = (
        'PARTIAL: Remotive and RemoteOK have complete bounded live-capture, production collection, '
        'normalization, repeated dedupe and reopen evidence. The subsequent WWR capture contained '
        '99 entries, 98 unique source identities and one conflicting identity; production collection '
        'failed in PERSIST_1 with SOURCE_DUPLICATE_JOB_ID. Those counts are capture expectations, '
        'not persisted inventory, and adapter HEALTHY does not prove persisted source health. '
        'Both the initial oracle rejection and this production failure are retained. '
        'A third complete real-source proof is still pending.'
    )
assert notes['MJ-REQ-254'].startswith('PARTIAL:')
(ROOT / 'p1_acceptance_notes.json').write_text(
    json.dumps(notes, indent=2, ensure_ascii=False) + '\n')
input_manifest['p1_acceptance_notes.json'] = {
    'path': str(ROOT / 'p1_acceptance_notes.json'),
    'sha256': hashlib.sha256((ROOT / 'p1_acceptance_notes.json').read_bytes()).hexdigest(),
}

reviews = {}
for name, data in review_documents.items():
    rows = data['requirements']
    if isinstance(rows, dict):
        rows = [dict(value, requirement_id=key) for key, value in rows.items()]
    for row in rows:
        identifier = row.get('id', row.get('requirement_id'))
        if identifier in notes:
            reviews[identifier] = (name, row)

result = {
    'schema_version': 2,
    'status': 'DRAFT_PENDING_FINAL_EXECUTION_AND_PROTECTED_SOURCE_INTEGRATION',
    'source_head_at_assembly': HEAD, 'baseline': matrix['timestamp'],
    'canonical_status_modified': False, 'tests_executed_by_assembly': False,
    'hosted_gates_verified_by_assembly': False, 'acceptance_owner': '/root',
    'requirements': [], 'missing_reviews': [], 'review_input_manifest': input_manifest,
    'execution_limits': [
        'Assembly and source/node validation are not application test execution or new acceptance.',
        'Root must inspect actual final-head hosted gates, complete selected Chromium journeys and '
        'required PostgreSQL cases; skips do not substantiate those claims.',
        'Historical reviewer findings retain their original provenance; source updates do not '
        'rewrite earlier failed or skipped executions.',
        'Live observations retain their actual execution revisions. A code-identical source bridge '
        'is not a new network execution.',
        'No RDC, live request, GitHub mutation or canonical repository write occurs in this assembly.',
    ],
}

extension_section = source_bytes('docs/ARCHITECTURE.md').decode().split(
    '## Optional extensions and core independence', 1)[1].split('\n## Recovery', 1)[0]
extension_map = {}
for line in extension_section.splitlines():
    if line.startswith('| ') and not line.startswith(('| Addition |', '| ---')):
        cells = [cell.strip() for cell in line.strip('|').split('|')]
        assert len(cells) == 3
        extension_map[cells[0].casefold()] = {
            'addition': cells[0], 'existing_boundary': cells[1], 'isolated_future_work': cells[2]}
extension_literals = re.search(
    FENCE + r'text\n(.*?)\n' + FENCE,
    literal_text(seed['MJ-REQ-259']), re.S).group(1).splitlines()
assert len(extension_literals) == 12
assert set(extension_map) == {addition.casefold() for addition in extension_literals}
package = json.loads(source_bytes('apps/web/package.json'))
tsconfig = json.loads(source_bytes('apps/web/tsconfig.json'))
assert tsconfig['compilerOptions']['strict'] is True
assert 'eslint ' in package['scripts']['lint']
assert package['scripts']['format:check'] == 'prettier --check scripts *.config.mjs'

for identifier, note in sorted(notes.items()):
    canonical = seed[identifier]
    eligible = identifier not in partial_ids
    item = {
        'requirement_id': identifier, 'title': canonical['title'],
        'issue': int(identifier.rsplit('-', 1)[1]) + 1,
        'priority': canonical['priority'], 'before': before[identifier]['result'],
        'proposed_result': 'PENDING' if eligible else 'PARTIAL',
        'proposed_result_after_required_gates': 'PASS' if eligible else 'PARTIAL',
        'candidate_for_new_pass': eligible,
        'authoritative_requirement': literal_text(canonical),
        'acceptance_summary_draft': note, 'criteria': [],
        'test_paths': [], 'test_nodes': [], 'source_paths': [], 'evidence_paths': [],
    }
    if identifier not in reviews:
        result['missing_reviews'].append(identifier)
        result['requirements'].append(item)
        continue
    name, original = reviews[identifier]
    row = deepcopy(original)
    item['review_checkpoint'] = name
    item['reviewed_gaps_before_changes'] = row.get(
        'remaining_gaps', row.get('known_requirement_gaps', []))
    item['review_limits'] = list(row.get('limitations', []))
    item['current_remaining_gaps'] = deepcopy(item['reviewed_gaps_before_changes'])
    for criterion in row.get('criteria', row.get('acceptance_criteria', [])):
        if isinstance(criterion, str):
            copied = {'criterion': criterion, 'evidence': row.get('findings', []),
                      'source_paths': row.get('sources', [])}
        else:
            copied = deepcopy(criterion)
            copied.setdefault('criterion', copied.get('literal_criterion', ''))
            for key in ('static_verdict', 'review_status', 'result', 'status', 'verdict'):
                if key in copied:
                    copied.setdefault('source_review_assessments', {})[key] = copied.pop(key)
        item['criteria'].append(copied)

    extra_paths = set()
    if name == 'acceptance-review-docs.json':
        extra_paths.update({'tests/unit/test_public_site.py', 'tests/unit/test_public_ci_tools.py'})
        item['shared_document_validation_checkpoint'] = name + '#validation'
        documented_paths = set()
        for path in referenced_paths(row):
            if path.endswith('.md'):
                documented_paths.update(referenced_paths(source_bytes(path).decode()))
        item['documented_example_test_paths'] = sorted(
            path for path in documented_paths if is_test(path))
        item['documented_example_test_scope'] = (
            'References in the guides are preserved, but do not silently expand the '
            'criterion acceptance run or claim fresh execution. Shared documentation '
            'validation is recorded in acceptance-review-docs.json#validation.')
        extra_paths.update(path for path in documented_paths if not is_test(path))

    if identifier == 'MJ-REQ-223':
        supplement = deepcopy(final_claims['MJ-REQ-223_supplement'])
        item['supplemental_review_checkpoint'] = 'acceptance-review-final-claims.json'
        item['generic_employer_feed_supplement'] = supplement
        entries = supplement['supplemental_evidence']
        by_criterion = {
            'tenant': [1], 'company identity': [1, 2], 'job enumeration': [0, 1, 2],
            'stable job IDs': [2], 'application URL': [0, 1, 2]}
        for criterion in item['criteria']:
            criterion['generic_feed_evidence'] = [
                deepcopy(entries[index]) for index in by_criterion[criterion['criterion']]]
        extra_paths.update(referenced_paths(supplement))

    if identifier == 'MJ-REQ-254':
        for criterion in item['criteria']:
            if criterion['criterion'].casefold() == 'no mysterious frozen screens':
                criterion['implementation'] = (
                    'api/upload and five migrated UI paths now bound complete response consumption. '
                    'The remaining 21 active modules still contain 32 injected-fetch dispatch sites '
                    'with no elapsed fetch/body deadline; navigation cancellation is insufficient.')
                criterion['meaningful_oracle'] = (
                    'apps/web/src/api.test.ts, apps/web/src/request-deadline-components.test.tsx and '
                    'apps/web/src/workspace-request-deadline.test.tsx exercise the implemented subset; '
                    'acceptance-review-ui-deadline-gaps.json enumerates every remaining site.')
                criterion['current_assessment'] = 'PARTIAL'
        item['deadline_gap_checkpoint'] = 'acceptance-review-ui-deadline-gaps.json'
        item['deadline_gap_counts'] = deepcopy(deadline_gaps['counts'])
        item['current_remaining_gaps'] = [deadline_gaps['reason']]
        extra_paths.update(referenced_paths(deadline_gaps))

    if identifier == 'MJ-REQ-259':
        for criterion in item['criteria']:
            addition = criterion['criterion'].casefold()
            if addition in extension_map:
                mapped = deepcopy(extension_map[addition])
                criterion['literal_clause'] = next(
                    literal for literal in extension_literals if literal.casefold() == addition)
                criterion['documented_extension_boundary'] = mapped
                criterion['implementation'] = mapped['existing_boundary'] + '; ' + mapped['isolated_future_work']
                criterion['meaningful_oracle'] = (
                    'docs/ARCHITECTURE.md, Optional extensions and core independence: the named '
                    'boundary is documented. Domain and registry tests substantiate reusable '
                    'boundaries; future connectors and clients are not claimed implemented.')
        item['extension_map'] = [extension_map[addition.casefold()] for addition in extension_literals]
        item['literal_extension_count'] = 12
        item['current_remaining_gaps'] = []
        item['source_update'] = (
            'The missing architecture map recorded at baseline was added in the integrated docs '
            'change; all twelve additions and core-startup independence are now documented.')
        extra_paths.update(referenced_paths(extension_section))
        extra_paths.update({'docs/ARCHITECTURE.md', 'tests/unit/test_extension_point_contracts.py'})

    if identifier == 'MJ-REQ-280':
        item['criteria'] = [
            {'criterion': 'strict TypeScript',
             'implementation': 'tsconfig strict=true; tsc --noEmit is the type authority and runs before Vite build.',
             'source_paths': ['apps/web/tsconfig.json', 'apps/web/package.json'],
             'verification_commands': ['npm --prefix apps/web run typecheck', 'npm --prefix apps/web run build']},
            {'criterion': 'ESLint',
             'implementation': 'Pinned ESLint ' + package['devDependencies']['eslint'] +
                 ' uses the Babel ESTree parser for TS/TSX and JSX. The lint script covers src, '
                 'vite.config.ts, scripts and config modules, followed by the custom policy check.',
             'source_paths': ['apps/web/eslint.config.mjs', 'apps/web/package.json', '.github/workflows/ci.yml'],
             'test_paths': ['apps/web/scripts/quality-checks.mjs'],
             'verification_commands': ['npm --prefix apps/web run lint', 'npm --prefix apps/web run quality:test']},
            {'criterion': 'formatter',
             'implementation': 'Pinned Prettier ' + package['devDependencies']['prettier'] +
                 ' is configured and exercised. Required format:check covers adopted scripts and '
                 '*.config.mjs tooling files; the whole-frontend format command is available. '
                 'No whole-frontend formatting completion is claimed.',
             'source_paths': ['apps/web/prettier.config.mjs', 'apps/web/package.json', '.github/workflows/ci.yml'],
             'test_paths': ['apps/web/scripts/quality-checks.mjs'],
             'verification_commands': ['npm --prefix apps/web run format:check', 'npm --prefix apps/web run quality:test'],
             'check_scope': package['scripts']['format:check'],
             'available_write_command_not_executed_by_assembly': package['scripts']['format']},
            {'criterion': 'component tests',
             'implementation': 'Vitest/jsdom and Testing Library render React components and exercise interaction.',
             'test_paths': ['apps/web/src/selectable-card.test.tsx',
                            'apps/web/src/application-supporting-documents.test.tsx',
                            'apps/web/src/first-run-wizard.test.tsx'],
             'verification_commands': ['npm --prefix apps/web test']},
        ]
        item['current_remaining_gaps'] = []
        item['source_update'] = (
            'Baseline missing-ESLint/formatter findings are resolved by the integrated tooling change; '
            'final-head execution remains a separate gate.')
        extra_paths.update({'apps/web/eslint.config.mjs', 'apps/web/prettier.config.mjs',
                            'apps/web/scripts/quality-checks.mjs'})

    if identifier == 'MJ-REQ-228':
        failed = next(source for source in wwr_followup['sources'] if source['provider'] == 'wwr')
        assert failed['result'] == 'FAIL' and failed['error_code'] == 'SOURCE_DUPLICATE_JOB_ID'
        item['latest_wwr_observation'] = deepcopy(failed)
        item['latest_wwr_observation']['counts_scope'] = (
            'Capture expectations only; PERSIST_1 failed. No persisted inventory, dedupe or source-health pass.')
        item['latest_wwr_evidence_path'] = str(ROOT / 'remote-harness-wwr-live-followup.json')
        item['current_remaining_gaps'] = [] if live_228_ready else [
            'A third complete real-source proof is pending. Preserve the earlier oracle rejection '
            'and the later production SOURCE_DUPLICATE_JOB_ID failure; neither is a persistence pass.']

    if identifier in {'MJ-REQ-228', 'MJ-REQ-229'}:
        item['live_evidence_binding'] = deepcopy(live['evidence'])
        item['acceptance_gate_caveat'] = (
            'Review findings refer to recorded literal evidence. Root owns final required gates, '
            'protected source integration and any canonical promotion.')

    criterion_references = {key: value for key, value in item.items()
                            if key != 'documented_example_test_paths'}
    all_paths = referenced_paths(row) | referenced_paths(criterion_references) | extra_paths
    item['test_paths'] = sorted(path for path in all_paths if is_test(path))
    item['source_paths'] = sorted(path for path in all_paths if not is_test(path))
    item['test_nodes'] = exact_test_nodes([row, item], item['test_paths'])
    for criterion in item['criteria']:
        criterion['test_paths'] = sorted(set(criterion.get('test_paths', [])) |
                                         {path for path in referenced_paths(criterion) if is_test(path)})
        criterion['test_nodes'] = sorted(set(criterion.get('test_nodes', [])) |
                                         set(exact_test_nodes(criterion, item['test_paths'])))
    item['evidence_paths'] = sorted({value for value in strings([row, item])
                                     if value.startswith(str(ROOT) + '/')
                                     and not value.startswith(str(REPO) + '/')
                                     and Path(value).is_file()})
    assert item['authoritative_requirement'] == literal_text(seed[identifier])
    result['requirements'].append(item)

bridge = []
for source in live['evidence']['source_identity_across_revisions']:
    path = source['path']
    object_id = subprocess.check_output(
        ['git', 'rev-parse', HEAD + ':' + path], cwd=REPO, text=True).strip()
    assert object_id == source['git_object'], (path, object_id, source['git_object'])
    bridge.append({'path': path, 'assembly_revision': HEAD, 'git_object': object_id,
                   'same_as_recorded_revisions': source['same_in_revisions']})
result['live_source_identity_bridge'] = bridge

candidates = [row for row in result['requirements'] if row['candidate_for_new_pass']]
partial = [row for row in result['requirements'] if not row['candidate_for_new_pass']]
assert len(candidates) == (43 if live_228_ready else 42)
assert {row['requirement_id'] for row in partial} == partial_ids
assert not result['missing_reviews']
assert all(row['criteria'] for row in result['requirements'])
assert all(row['before'] != 'PASS' for row in candidates)
all_paths = {path for row in result['requirements'] for key in ('test_paths', 'source_paths')
             for path in row[key]}
documented_examples = sorted({path for row in result['requirements']
                             for path in row.get('documented_example_test_paths', [])})
result['documented_example_test_paths'] = documented_examples
result['test_manifest_scope'] = (
    'Criterion/supporting-test references, including explicitly PARTIAL rows. '
    'Guide example references are retained separately and are not additional execution claims.')
all_paths.update(documented_examples)
assert all(path in TRACKED for path in all_paths)
result['source_file_sha256'] = {
    path: hashlib.sha256(source_bytes(path)).hexdigest() for path in sorted(all_paths)}
all_evidence = {path for row in result['requirements'] for path in row['evidence_paths']}
result['evidence_file_sha256'] = {
    path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in sorted(all_evidence)}
p1 = [row for row in matrix['requirements'] if row['priority'] == 'P1']
baseline_pass = sum(row['result'] == 'PASS' for row in p1)
result['conditional_progress'] = {
    'canonical_p1_pass_at_source_head': baseline_pass, 'p1_total': len(p1),
    'new_pass_candidates': len(candidates),
    'projected_p1_pass_after_all_required_gates': baseline_pass + len(candidates),
    'projection_is_not_current_acceptance': True,
    'partial_requirement_ids': sorted(partial_ids),
}

target = ROOT / 'p1-reviewed-draft.json'
target.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
manifest = sorted({path for row in result['requirements'] for path in row['test_paths']})
(ROOT / 'p1-test-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
summary = {
    'source_head': HEAD, 'draft_requirements': len(result['requirements']),
    **result['conditional_progress'],
    'test_paths': len(manifest),
    'python_test_paths': sum(path.endswith('.py') for path in manifest),
    'frontend_test_paths': sum(path.startswith('apps/web/') for path in manifest),
    'criterion_browser_test_paths': sum(path.startswith('tests/browser/') for path in manifest),
    'documented_example_test_references': len(documented_examples),
    'exact_python_test_nodes': len({node for row in result['requirements'] for node in row['test_nodes']}),
    'source_and_test_file_hashes': len(result['source_file_sha256']),
    'missing_reviews': result['missing_reviews'],
    'MJ-REQ-223_supplement': 'Generic RSS/Atom/JSON criteria retain all nine exact supplemental nodes and source references.',
    'MJ-REQ-229': 'Five literal clauses retain original execution revisions, failed historical attempt and source bridge.',
    'MJ-REQ-259': 'All twelve extension entries plus nonblocking-core criterion retained; historical gap resolved.',
    'MJ-REQ-280': 'Four literal criteria; current ESLint and bounded Prettier tooling check replace obsolete descriptions.',
    'limits': result['execution_limits'], 'canonical_status_modified': False,
}
(ROOT / 'p1-assembly-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps(summary))
