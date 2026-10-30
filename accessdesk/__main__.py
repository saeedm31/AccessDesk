"""Command line: python -m accessdesk {build|chat E010 "message"|eval [--live]}"""

from __future__ import annotations

import json
import sys

from . import evals, simulate
from .config import Settings


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "build":
        simulate.build(Settings().data_dir)
        print(f"Built the simulated company in {Settings().data_dir}")
        return 0
    if cmd == "chat":
        from .service import open_service

        svc = open_service()
        r = svc.chat(sys.argv[2], sys.argv[3])
        print(json.dumps({k: r[k] for k in ("reply", "proposal", "outcome", "remember")}, indent=2, ensure_ascii=False))
        svc.hub.close()
        return 0
    if cmd == "eval":
        return evals.main(sys.argv[2:])
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
