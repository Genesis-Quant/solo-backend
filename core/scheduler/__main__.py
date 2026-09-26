"""python -m core.scheduler sync/start/instances/tasks/log。"""

import argparse
import json

from .client import DolphinSchedulerClient
from .workflows import sync_workflows


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync")
    start = commands.add_parser("start")
    start.add_argument("kind", choices=["factor", "model", "optimize", "control", "execution", "strategy"])
    start.add_argument("--input-file", required=True)
    instances = commands.add_parser("instances")
    instances.add_argument("kind", choices=["factor", "model", "optimize", "control", "execution", "strategy"])
    for name in ("instance", "tasks", "log"):
        command = commands.add_parser(name)
        command.add_argument("id", type=int)
        if name == "log":
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    if args.command == "sync":
        result = sync_workflows()
    else:
        with DolphinSchedulerClient() as client:
            if args.command == "start":
                result = client.start(args.kind, args.input_file)
            elif args.command == "instances":
                result = client.instances(args.kind)
            elif args.command == "log":
                result = client.log(args.id, args.offset, args.limit)
            else:
                result = getattr(client, args.command)(args.id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
