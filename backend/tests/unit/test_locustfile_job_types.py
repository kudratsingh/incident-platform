"""The Locust load suite must only submit job types the API accepts.

``_JOB_TYPES`` in backend/tests/load/locustfile.py is derived from
``app.models.enums.JobType``; this test pins the two together so the list
can never again drift from the enum (three of four hardcoded entries once
failed validation with 422 on every submission, voiding the load numbers).

The locustfile is loaded via ``importlib.util.spec_from_file_location`` in
a child interpreter rather than in-process: importing ``locust`` runs
gevent's ``monkey.patch_all()``, which would irreversibly patch
socket/ssl/threading in this pytest process and destabilise the
asyncio-based API tests that run later in the suite. The child also
mirrors real usage — ``locust -f`` loads the file in a fresh interpreter.
"""

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
