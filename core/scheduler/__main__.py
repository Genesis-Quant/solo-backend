"""python -m core.scheduler sync/start/instances/tasks/log。"""

import argparse
import json
from pathlib import Path
from uuid import UUID

from core.apps import version_policy as policy
from core.apps.version_policy_views import TaskCheck, check_task
from core.database.session import database_session_factory

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
        if args.command == "start":
            input_file = Path(args.input_file).resolve()
            run_id = UUID(input_file.parent.name)
            if input_file != (policy.run_path(run_id) / "input.json").resolve():
                raise ValueError("CLI 只能提交对应数据库 runs/{id}/input.json")
            # CLI has no REST admission layer; check before even logging into DS.
            with database_session_factory()() as session:
                check_task(run_id, TaskCheck(kind=args.kind), session)
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
