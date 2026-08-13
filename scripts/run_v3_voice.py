from __future__ import annotations

import json
import os
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
from scripts.planner_schema_guard import install_schema_guard
from scripts.task_level_planner_router import install_router
from scripts.voice_mesh import install_voice_mesh


def main() -> None:
    install_schema_guard()
    install_router()
    install_voice_mesh()
    request = json.loads(Path(os.environ["REQUEST_FILE"]).read_text(encoding="utf-8"))
    out = orchestrator.produce(
        topic=request["topic"],
        requested_format=request["format"],
        dry_run=False,
        do_research=True,
    )
    print(f"Production completed: {out.name}")


if __name__ == "__main__":
    main()
