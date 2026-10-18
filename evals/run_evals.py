"""Run the agent evaluation suites and write evals/REPORT.md.

python evals/run_evals.py            # both suites, replaying recorded cassettes
python evals/run_evals.py --check    # also exit 1 if a gate fails (used by CI)
python evals/run_evals.py --record   # record live model drafts first (needs OPENAI_API_KEY)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carenote.evals.run import main

if __name__ == "__main__":
    raise SystemExit(main())
