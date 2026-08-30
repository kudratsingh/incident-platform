#!/usr/bin/env python3
"""Behavioural guard for release.yml's release-vs-dispatch decision (WO-R2-31).

Runs in the `workflows` (Workflow lint) CI job, and locally with:

    python3 .github/release-path-check.py

Why this and not a static assertion: the property that matters is not "the
string GITHUB_EVENT_NAME appears somewhere", it is "a workflow_dispatch run
started from a tag ref does not move :latest and does not republish the tag".
That is a statement about what the script *does*, so this extracts the real
`Compute tags` body out of the workflow and executes it under both event
names, asserting on the tags it computes.

`docker manifest inspect` is stubbed on PATH, so the GHCR collision refusal
is exercised deterministically with no registry, no network and no daemon.

The bug this locks down: the discriminator used to be `GITHUB_REF_TYPE ==
"tag"`. A `workflow_dispatch` can be launched from any ref including a `v*`
tag, so REF_TYPE reads "tag" for both surfaces — a manual smoke-test build
from a tag took the full release path, moved `:latest`, skipped the
version-format check and the collision refusal, and ignored the version input.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github/workflows/release.yml"
OWNER = "kudratsingh"
IMAGE = f"ghcr.io/{OWNER}/incident-platform"

# `${{ }}` expressions the extracted script is allowed to contain, and the
# value each takes in this harness. Anything else aborts rather than being
# silently mangled — if someone adds a new interpolation, this says so.
KNOWN_EXPRESSIONS = {"${{ github.repository_owner }}": OWNER}


def extract_script() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text())
    steps = doc["jobs"]["build-and-push"]["steps"]
    for step in steps:
        if step.get("name") == "Compute tags":
            script = step["run"]
            break
    else:  # pragma: no cover - the workflow always has this step
        sys.exit("FAIL: release.yml has no 'Compute tags' step")

    for expr, value in KNOWN_EXPRESSIONS.items():
        script = script.replace(expr, value)
    if "${{" in script:
        leftover = script[script.index("${{") :].split("}}")[0] + "}}"
        sys.exit(
            f"FAIL: unrecognised workflow expression in Compute tags: {leftover}\n"
            "Add it to KNOWN_EXPRESSIONS with the value it should take here."
        )
    return script


def run_case(
    script: str,
    *,
    event_name: str,
    ref_type: str,
    ref_name: str,
    input_version: str = "",
    tag_exists_on_ghcr: bool = False,
) -> tuple[int, dict[str, str], str]:
    """Execute the extracted script; return (exit code, outputs, stderr+stdout)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)

        # Stub `docker` so the collision refusal is testable offline. Exit 0
        # means "manifest found", i.e. the tag already exists on GHCR.
        bindir = tmpdir / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(
            "#!/usr/bin/env bash\nexit %d\n" % (0 if tag_exists_on_ghcr else 1)
        )
        docker.chmod(0o755)

        github_output = tmpdir / "github_output"
        github_output.touch()
        script_path = tmpdir / "compute_tags.sh"
        script_path.write_text(script)

        env = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            "HOME": str(tmpdir),
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_REF_TYPE": ref_type,
            "GITHUB_REF_NAME": ref_name,
            "GITHUB_SHA": "abc1234def5678",
            "INPUT_VERSION": input_version,
            "GITHUB_OUTPUT": str(github_output),
        }
        proc = subprocess.run(
            ["bash", str(script_path)],
            env=env,
            capture_output=True,
            text=True,
        )
        outputs = {}
        for line in github_output.read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                outputs[key] = value
        return proc.returncode, outputs, proc.stdout + proc.stderr


def main() -> int:
    if shutil.which("bash") is None:  # pragma: no cover
        sys.exit("FAIL: bash is required")
    script = extract_script()
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}{(': ' + detail) if detail else ''}")
            failures.append(label)

    print("release.yml — Compute tags, behavioural matrix\n")

    # ---- the release path: a v* tag push -------------------------------
    print("push on refs/tags/v1.2.3 (the real release):")
    rc, out, log = run_case(
        script, event_name="push", ref_type="tag", ref_name="v1.2.3"
    )
    check("succeeds", rc == 0, log)
    check("version is the tag", out.get("version") == "v1.2.3", str(out))
    check(
        "publishes :latest",
        out.get("tags") == f"{IMAGE}:v1.2.3,{IMAGE}:latest",
        str(out),
    )

    # ---- THE regression: dispatch launched from a tag ref ---------------
    print("\nworkflow_dispatch from a tag ref, with a version input:")
    rc, out, log = run_case(
        script,
        event_name="workflow_dispatch",
        ref_type="tag",
        ref_name="v1.2.3",
        input_version="v0.4.0-rc1",
    )
    check("succeeds", rc == 0, log)
    check(
        "does NOT move :latest",
        ":latest" not in out.get("tags", ""),
        str(out),
    )
    check(
        "uses the version input, not the tag it was launched from",
        out.get("version") == "v0.4.0-rc1",
        str(out),
    )
    check("tags only the input version", out.get("tags") == f"{IMAGE}:v0.4.0-rc1")

    print("\nworkflow_dispatch from a tag ref, with NO version input:")
    rc, out, log = run_case(
        script, event_name="workflow_dispatch", ref_type="tag", ref_name="v1.2.3"
    )
    check("succeeds", rc == 0, log)
    check("does NOT move :latest", ":latest" not in out.get("tags", ""), str(out))
    check(
        "falls back to the SHA tag rather than republishing v1.2.3",
        out.get("version") == "sha-abc1234",
        str(out),
    )

    # ---- the guards the dispatch path is supposed to enforce -----------
    print("\nworkflow_dispatch guards:")
    rc, out, log = run_case(
        script,
        event_name="workflow_dispatch",
        ref_type="tag",
        ref_name="v1.2.3",
        input_version="not-a-version",
    )
    check("refuses a malformed version input", rc != 0)
    check("says why", "bad version format" in log, log)

    rc, out, log = run_case(
        script,
        event_name="workflow_dispatch",
        ref_type="branch",
        ref_name="master",
        input_version="v1.2.3",
        tag_exists_on_ghcr=True,
    )
    check("refuses a version already published on GHCR", rc != 0)
    check("says why", "already exists on GHCR" in log, log)

    rc, out, log = run_case(
        script, event_name="workflow_dispatch", ref_type="branch", ref_name="master"
    )
    check("plain branch dispatch still builds a SHA tag", rc == 0, log)
    check("and does not move :latest", ":latest" not in out.get("tags", ""))

    # ---- the release path validates its tag too -------------------------
    print("\npush of a tag matching v* but not vX.Y.Z:")
    rc, out, log = run_case(
        script, event_name="push", ref_type="tag", ref_name="vfoo"
    )
    check("refuses to publish it", rc != 0)
    check("says why", "bad tag format" in log, log)

    # ---- structural: the belt-and-braces :latest guard exists ----------
    # Unreachable while the branches above are correct, so it cannot be
    # exercised behaviourally; assert it is present so it is not dropped.
    print("\nstructure:")
    check(
        "an explicit non-push :latest refusal is present",
        'refusing to move :latest from a' in script,
    )
    check(
        "the release path is not gated on ref type alone",
        'if [[ "${GITHUB_REF_TYPE}" == "tag" ]]; then' not in script,
    )
    check(
        "the release path is gated on the event name",
        '"${GITHUB_EVENT_NAME}" == "push"' in script,
    )

    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("All release-path checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
