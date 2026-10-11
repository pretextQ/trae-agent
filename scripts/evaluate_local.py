"""Run the REAL Eyes coding scorer against REAL trae-agent artifacts.

No scoring logic lives here: this script only adapts downloaded artifacts into
the evidence view `integrations/deta/scorer.py#score` expects, then prints its
verdict unchanged. The scorer depends on POSIX (os.killpg), so it runs inside
the Linux container:

  docker run --rm -v <eyes-src>:/opt/eyes:ro -v <downloads>:/data:ro \
    --entrypoint /app/.venv/bin/python trae-agent:local /app/scripts/evaluate_local.py \
    --dataset /opt/eyes/integrations/deta/coding-smoke.jsonl --case fix-mean \
    --artifacts /data/<idempotency_key>
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


def load_frozen_scorer(path: Path):
    """Load integrations/deta/scorer.py by path, unmodified, from the Eyes checkout."""
    spec = importlib.util.spec_from_file_location("eyes_frozen_coding_scorer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.score


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--scorer", type=Path, default=Path("/opt/eyes/integrations/deta/scorer.py")
    )
    parser.add_argument("--case", required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    sys.path.insert(0, "/opt/eyes/src")
    score = load_frozen_scorer(args.scorer)

    case = None
    for line in args.dataset.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        loaded = json.loads(line)
        if loaded.get("case_id") == args.case:
            case = loaded
    if case is None:
        raise SystemExit(f"case {args.case!r} not found")

    artifact_files = sorted(p for p in args.artifacts.iterdir() if p.is_file())
    evidence = {
        "artifacts": [
            {
                "id": f"local-{index}",
                "metadata_content": {"name": path.name},
            }
            for index, path in enumerate(artifact_files)
        ],
        "events": [],
        "references": [],
    }
    artifact_bytes = {
        f"local-{index}": path.read_bytes() for index, path in enumerate(artifact_files)
    }

    with tempfile.TemporaryDirectory() as workspace:
        workspace_path = Path(workspace)

        def context_artifact(artifact_id: str):
            target = workspace_path / f"evidence-{artifact_id}"
            target.write_bytes(artifact_bytes[artifact_id])
            return target

        request = SimpleNamespace(
            case=case,
            evidence=evidence,
        )
        context = SimpleNamespace(
            workspace=workspace_path,
            artifact=context_artifact,
        )
        output = score(request, context)
        payload = {
            "case_id": args.case,
            "status": output.status,
            "verdict": output.verdict,
            "reason": output.reason,
            "evidence_refs": list(output.evidence_refs),
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.report:
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
