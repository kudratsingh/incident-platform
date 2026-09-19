"""The Locust load suite must only submit job types the API accepts."""

import json
import subprocess
import sys
from pathlib import Path

from app.models.enums import JobType

_LOCUSTFILE = Path(__file__).resolve().parents[1] / "load" / "locustfile.py"

_LOADER = """\
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("locustfile_under_test", sys.argv[1])
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps(sorted(module._JOB_TYPES)))
"""


def test_job_types_match_jobtype_enum() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _LOADER, str(_LOCUSTFILE)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    job_types = json.loads(result.stdout.splitlines()[-1])
    assert set(job_types) == {t.value for t in JobType}
