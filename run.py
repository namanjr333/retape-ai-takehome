"""CLI: evaluate one case folder and print the Result as JSON.

    python run.py cases/case1_feasible_even
"""

from __future__ import annotations

import json
import sys

from feasibility.engine import evaluate_offer
from feasibility.models import load_case


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python run.py <case_dir>", file=sys.stderr)
        return 2
    client, offer, rules = load_case(argv[1])
    result = evaluate_offer(client, offer, rules)
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
