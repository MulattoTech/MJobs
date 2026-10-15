"""Disposable FIXTURE-only backup/fresh-restore proof; never deletes data volumes."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

import httpx

from cli import compose_recovery as recovery
from cli import local_backup
from cli import local_persistence_acceptance as persistence


def run_public_cli(subcommand, archive, *, environment, project=None, port=None):
    """Exercise the documented parser in a real child; retain no child output.

    The bounded runner checks exit status and drains private diagnostics. A zero
    exit also needs the exact public FIXTURE success summary before it is proof.
    """
    command = [sys.executable, '-m', 'cli.local_backup', subcommand]
    archive = str(Path(archive).resolve())
    if subcommand == 'backup':
        project = recovery.validate_project_name(project)
        command.extend(['--project', project, '--archive', archive])
        status = (r'Backup created: ([1-9][0-9]*) files; mode FIXTURE\n'
                  r'Configuration snapshot: non-secret runtime settings saved for manual review\n')
    elif subcommand == 'verify':
        command.extend(['--archive', archive])
        status = r'Backup verified: ([1-9][0-9]*) files; mode FIXTURE\n'
    elif subcommand == 'restore':
        project = recovery.validate_project_name(project)
        port = recovery.validate_port(port)
        command.extend(['--archive', archive, '--target-project', project, '--port', str(port)])
        status = re.escape(f'Restored FIXTURE project {project} at 127.0.0.1:{port}\n')
    else:
        raise ValueError('Unsupported public backup proof subcommand')
    output = recovery._run_bounded(command, environment=environment,
                                   timeout=120 if subcommand == 'verify' else 1800,
                                   action='Public ' + subcommand + ' CLI')
    matched = re.fullmatch(status, output)
    if matched is None:
        raise RuntimeError('Public ' + subcommand + ' CLI did not return the expected positive status')
    result = {'command': command, 'exit_code': 0, 'status': 'PASS', 'result': 'PASS'}
    if subcommand in {'backup', 'verify'}:
        result['file_count'] = int(matched[1])
    return result


def run(baseline_path, evidence_path, source_project, target_project, port, archive):
    source_project = recovery.validate_project_name(source_project)
    target_project = recovery.validate_project_name(target_project)
    if source_project == target_project:
        raise ValueError("Fresh restore proof requires distinct fixture projects")
    port = recovery.validate_port(port)
    baseline = recovery.validate_baseline(json.loads(Path(baseline_path).read_text(encoding="utf-8")))
    config_files = recovery.resolve_compose_files()
    if config_files != (recovery.COMPOSE_FILE.resolve(),):
        raise ValueError("Backup acceptance requires the canonical Compose file without overrides")
    recovery.preflight_recovery_targets(source_project, port, config_files=config_files)
    environment = local_backup._config("FIXTURE", port)
    containers = local_backup._compose(target_project, "ps", "-a", "-q", environment=environment,
                                      action="Inspect fresh proof target").decode().splitlines()
    local_backup.require_empty_target(target_project, source_project, containers, local_backup._volumes())
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15, trust_env=False) as client:
        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        recovery.verify_recovered_application(request, baseline)
        persistence.compare_baseline(baseline, persistence.observe_http(client, baseline)
                                     if 'local_persistence' in baseline else None)
    stop_services = persistence.documented_stop_services()
    running_refusal = persistence.require_running_backup_refusal(source_project, Path(archive))
    recovery.compose(source_project, port, "stop", *stop_services, config_files=config_files)
    # The runbook's FIXTURE addendum separately stops the receipt writer.
    recovery.compose(source_project, port, "stop", "mock", config_files=config_files)
    public_cli = {'backup': run_public_cli('backup', archive, environment=environment,
                                           project=source_project)}
    public_cli['verify'] = run_public_cli('verify', archive, environment=environment)
    manifest = local_backup.verify_backup(Path(archive))
    if any(public_cli[name]['file_count'] != len(manifest['files']) for name in ('backup', 'verify')):
        raise RuntimeError('Public backup CLI file counts differ from the verified archive')
    corrupt_refusal = persistence.require_corrupt_restore_refusal(Path(archive), target_project, port)
    # Read the original through the API after capture, before freeing its port.
    # The one-shot migration service stays stopped; only established services start.
    source_unchanged = {'result': 'NOT_RUN', 'reason': 'Legacy baseline has no seven-domain inventory'}
    if 'local_persistence' in baseline:
        recovery.compose(source_project, port, 'start', *recovery.RECOVERY_RESTART_SERVICES,
                         config_files=config_files)
        local_backup._wait_ready(port)
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=15, trust_env=False) as client:
            source_unchanged = persistence.compare_baseline(baseline, persistence.observe_http(client, baseline))
        recovery.compose(source_project, port, 'stop', *stop_services, config_files=config_files)
        recovery.compose(source_project, port, 'stop', 'mock', config_files=config_files)
    # Free the source's loopback port while retaining all source data volumes.
    recovery.compose(source_project, port, "down", "--remove-orphans", config_files=config_files)
    public_cli['restore'] = run_public_cli('restore', archive, environment=environment,
                                          project=target_project, port=port)
    occupied_refusal = persistence.require_occupied_restore_refusal(Path(archive), target_project, port)
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15, trust_env=False) as client:
        persistence_result = persistence.compare_baseline(
            baseline, persistence.observe_http(client, baseline)
            if 'local_persistence' in baseline else None)
    # Existing guarded recovery proof checks the exact DB application, operation,
    # request identity, explicit replay and the provider's independent receipt.
    recovery_evidence = Path(evidence_path).with_name(Path(evidence_path).stem + "-recovery.json")
    recovery.run(baseline_path, recovery_evidence, target_project, port)
    evidence = {
        "utc": datetime.now(timezone.utc).isoformat(), "result": "PASS",
        "source_project": source_project, "target_project": target_project,
        "archive_schema": manifest["schema_version"],
        "receipt_sha256": manifest["files"][local_backup.RECEIPT_PATH],
        "application_id": baseline["application_id"], "operation_id": baseline["operation_id"],
        "request_id": baseline["request_id"], "external_submission_enabled": False,
        "behavior": "Fresh database/documents/provider volumes restored; original confirmed application and request replay retained; exact independent receipt submission_count remains one",
        "recovery_evidence": str(recovery_evidence),
        "local_persistence": persistence_result,
        "documented_stop_services": list(stop_services),
        "configuration_restore": "Operator-managed configuration retained; archive settings not applied",
        "source_after_backup": source_unchanged,
        "negative_controls": {"running_writers": running_refusal,
                              "corrupt_archive": corrupt_refusal, "occupied_target": occupied_refusal},
        "public_cli": public_cli,
    }
    Path(evidence_path).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source-project", required=True)
    parser.add_argument("--target-project", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    run(args.baseline, args.evidence, args.source_project, args.target_project, args.port, args.archive)
    print("Fresh fixture backup/restore and independent receipt replay verified")


if __name__ == "__main__":
    main()
