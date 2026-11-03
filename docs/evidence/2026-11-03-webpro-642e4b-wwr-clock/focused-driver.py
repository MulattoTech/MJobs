from pathlib import Path
import hashlib, importlib, json, os, sys
root=Path.cwd().resolve()
assert root.name=="MJobs-webpro-20261103T031738Z-642e4b-clock"
modules=["tests.integration.test_relative_posted_source_intake","tests.integration.test_wwr","apps.api.main","services.discovery.search_semantics","services.discovery.sources.wwr","packages.persistence.store"]
bindings={}
for name in modules:
    module=importlib.import_module(name)
    source=Path(module.__file__).resolve()
    assert source.is_relative_to(root), name
    bindings[name]={"path":source.relative_to(root).as_posix(),"sha256":hashlib.sha256(source.read_bytes()).hexdigest()}
print("CLOCK_SOURCE_BINDING "+json.dumps({"cwd":"<owned-clock-worktree>","python_version":sys.version.split()[0],"os_name":os.name,"modules":bindings},sort_keys=True),flush=True)
import pytest
raise SystemExit(pytest.main(sys.argv[1:]))
