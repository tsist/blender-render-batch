"""Portable entry point for the bundled Blender render-batch runtime."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "runtime" / "tools" / "blenderctl"))

from protocol import CODES, SCHEMA, Failure, envelope
from render_batch import dispatch, register_parser
import runner


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("INVALID_REQUEST", message)


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--version", action="version", version="0.3.0")
    blender = os.environ.get("BLENDER_EXECUTABLE") or shutil.which("blender") or "blender.exe"
    parser.add_argument("--blender", type=Path, default=Path(blender))
    parser.add_argument("--jobs-dir", type=Path, default=BASE / "jobs")
    parser.add_argument("--transactions-dir", type=Path, default=BASE / "transactions")
    parser.add_argument("--timeout", type=float, default=7200)
    parser.add_argument("--async", dest="background", action="store_true")
    parser.set_defaults(compact=False, full=False)
    commands = parser.add_subparsers(dest="command", required=True)
    actions = commands.add_parser("render").add_subparsers(dest="action", required=True)
    actions.add_parser("devices", help="enumerate Blender render devices")
    register_parser(actions)
    args = None
    try:
        args = parser.parse_args(argv)
        if args.action == "devices":
            job = runner.prepare({"schema_version": SCHEMA, "command": "render.devices", "params": {}},
                                 args.blender, args.jobs_dir, args.timeout, args.transactions_dir)
            if args.background:
                result, code = runner.submit(job), 0
            else:
                result, code = runner.execute(job)
        else:
            result = dispatch(args)
            code = 4 if args.batch_action == "submit" and result["data"]["state"] in ("failed", "partial") else 0
    except (Failure, OSError, ValueError) as error:
        error_code = error.code if isinstance(error, Failure) else "IO_ERROR"
        action = getattr(args, "batch_action", getattr(args, "action", None))
        result = envelope("render." + action if action else None,
                          error={"code": error_code, "message": str(error)})
        code = CODES.get(error_code, 1)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
