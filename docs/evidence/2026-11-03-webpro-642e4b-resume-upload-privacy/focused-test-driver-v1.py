"""Run only named unittest modules and retain exact runtime source bindings."""
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.getcwd())
output = Path(sys.argv[1])
modules = sys.argv[2:]
output.mkdir(parents=True, exist_ok=True)
bindings = {}
for name in ["services.documents.uploads", *modules]:
    module = importlib.import_module(name)
    path = Path(module.__file__).resolve()
    bindings[name] = {"module_file": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
print("RUNTIME_SOURCE_BINDINGS " + json.dumps(bindings, sort_keys=True), flush=True)

class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.subtest_passed = 0
        self.subtest_failed = 0
        self.subtest_errors = 0

    def startTest(self, test):
        super().startTest(test)
        self.current = {"id": test.id(), "status": "PASS", "details": []}
        self.started = time.monotonic()

    def stopTest(self, test):
        self.current["duration_seconds"] = time.monotonic() - self.started
        self.records.append(self.current)
        super().stopTest(test)

    def addFailure(self, test, error):
        self.current["status"] = "FAIL"
        self.current["details"].append(self._exc_info_to_string(error, test))
        super().addFailure(test, error)

    def addError(self, test, error):
        self.current["status"] = "ERROR"
        self.current["details"].append(self._exc_info_to_string(error, test))
        super().addError(test, error)

    def addSkip(self, test, reason):
        self.current["status"] = "SKIP"
        self.current["details"].append(reason)
        super().addSkip(test, reason)

    def addSubTest(self, test, subtest, error):
        if error is None:
            self.subtest_passed += 1
        else:
            failed = issubclass(error[0], test.failureException)
            self.subtest_failed += int(failed)
            self.subtest_errors += int(not failed)
            self.current["status"] = "FAIL" if failed else "ERROR"
            self.current["details"].append(subtest.id() + "\n" + self._exc_info_to_string(error, subtest))
        super().addSubTest(test, subtest, error)

start = datetime.now(timezone.utc)
suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
end = datetime.now(timezone.utc)
receipt = {
    "schema_version": 1,
    "command": [sys.executable, *sys.argv],
    "cwd": os.getcwd(),
    "platform": sys.platform,
    "os_name": os.name,
    "started_utc": start.isoformat(),
    "finished_utc": end.isoformat(),
    "duration_seconds": (end - start).total_seconds(),
    "source_bindings": bindings,
    "tests": result.testsRun,
    "passed": sum(row["status"] == "PASS" for row in result.records),
    "failed_testcases": sum(row["status"] == "FAIL" for row in result.records),
    "error_testcases": sum(row["status"] == "ERROR" for row in result.records),
    "failures": len(result.failures),
    "errors": len(result.errors),
    "skipped": len(result.skipped),
    "subtests_passed": result.subtest_passed,
    "subtests_failed": result.subtest_failed,
    "subtests_errors": result.subtest_errors,
    "result": "PASS" if result.wasSuccessful() and not result.skipped else
              "PASS_WITH_EXPLICIT_SKIPS" if result.wasSuccessful() else "FAIL",
    "records": result.records,
}
(output / "execution.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n")
root = ET.Element("testsuite", {
    "name": "mjobs-upload-privacy",
    "tests": str(result.testsRun),
    "failures": str(receipt["failed_testcases"]),
    "errors": str(receipt["error_testcases"]),
    "skipped": str(receipt["skipped"]),
    "timestamp": start.isoformat().replace("+00:00", "Z"),
    "time": str(receipt["duration_seconds"]),
})
for row in result.records:
    classname, _, name = row["id"].rpartition(".")
    case = ET.SubElement(root, "testcase", {
        "classname": classname, "name": name, "time": str(row["duration_seconds"]),
    })
    if row["status"] != "PASS":
        child = ET.SubElement(case, {"FAIL": "failure", "ERROR": "error", "SKIP": "skipped"}[row["status"]])
        child.text = "\n".join(row["details"])
ET.indent(root)
ET.ElementTree(root).write(output / "tests.xml", encoding="utf-8", xml_declaration=True)
print("RUNTIME_SUMMARY " + json.dumps({k: v for k, v in receipt.items()
    if k not in ("source_bindings", "records", "command", "cwd")}, sort_keys=True), flush=True)
sys.exit(0 if result.wasSuccessful() else 1)
