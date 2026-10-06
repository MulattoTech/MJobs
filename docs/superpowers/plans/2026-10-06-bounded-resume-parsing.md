# Bounded Native Resume Parsing

## Objective

Close the wall-clock gap for native PDF and DOCX extraction under open P0 issue #5 / MJ-REQ-004. Preserve local-only processing, existing format/resource limits, OCR review behavior, and the rule that extracted facts remain unverified.

## Acceptance behavior

- PDF and DOCX native parsing runs in a shell-free child with the document sent over stdin, a 15-second wall deadline, discarded stderr, captured bounded JSON output, and an allowlisted environment without application credentials.
- Linux parser children also have address-space, CPU, file-size, and core-dump limits. Windows relies on the parent process deadline.
- Deadline, launch, malformed-output, and child-failure paths return a safe unreadable/review result without raw parser diagnostics, facts, or contact suggestions.
- Existing hard page/archive/XML bounds still fail with their established safe `ValueError` categories.
- OCR remains in the parent after native PDF parsing, uses its existing per-tool/total time limits, and preserves native text when OCR fails. No OCR process is started by the parser child.
- TXT remains inline UTF-8 decoding; all returned facts remain unverified and unapproved.

## Implementation and validation

1. Define tests for timeout classification, safe child invocation/environment, and continued real child extraction for PDF and DOCX.
2. Move native PDF/DOCX parsing into `parser_worker`; validate and cap the JSON response in the parent.
3. Run resume extraction/OCR units, profile import API integration, uploaded-document comparison, and profile import browser coverage.
4. Run the full local deterministic suite, repository validators, and browser/build checks affected by the API behavior.
5. Record exact source/input hashes, commands, UTC times, failures/skips, and environment limitations. Keep issue #5 open and MJ-REQ-004 PARTIAL because this slice does not cover all operation classes.

## Limitations

The parser process wall timeout is locally unit-tested at the subprocess boundary; CPU/RAM limits are Linux-only. Real Linux container and production API deployment checks remain dependent on locally available infrastructure. No AI-provider path exists to validate provider timeouts here.
