from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.worldpanel.probe import run_phase0_probe


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture redacted DOM and XHR evidence for one real Pivot member-tree + click."
    )
    parser.add_argument("--report-parameter", help="Optional Ready-to-Use/Data Explorer report query parameter.")
    parser.add_argument("--report-set", help="Optional report set override.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("runtime") / "phase0-probe" / datetime.now().strftime("%Y%m%d-%H%M%S"),
    )
    args = parser.parse_args()

    try:
        output = asyncio.run(
            run_phase0_probe(
                get_settings(),
                args.output_dir,
                report_parameter=args.report_parameter,
                report_set=args.report_set,
            )
        )
    except Exception as exc:
        print(f"Phase 0 probe failed: {type(exc).__name__}: {exc}")
        print(f"Progress log: {(args.output_dir / 'progress.jsonl').resolve()}")
        return 1
    print(output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
