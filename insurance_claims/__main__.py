"""Run the demo server:  python -m insurance_claims [--mock] [--host H] [--port P]"""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="SOP-guided insurance claims agent demo server")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument(
        "--mock",
        action="store_true",
        help="run offline with the regex/template test double instead of the model",
    )
    args = parser.parse_args()
    if args.mock:
        os.environ["SOP_LLM_MODE"] = "mock"  # read when the server module loads its settings
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run("insurance_claims.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
