from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from . import inventory, scheduler, setup, task
from .config import ConfigError, get_cluster


Command = Callable[[list[str] | None], int]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="nk")
    result.add_argument("command", nargs="?", choices=("task", "scheduler", "cluster", "node", "workspace"))
    return result


def inventory_parser(family: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog=f"nk {family}")
    commands = result.add_subparsers(dest="action", required=True)
    if family == "cluster":
        add = commands.add_parser("add")
        add.add_argument("name")
        add.add_argument("--repository", required=True)
        remove = commands.add_parser("remove")
        remove.add_argument("name")
        for name in ("setup", "verify", "uncordon"):
            command = commands.add_parser(name)
            command.add_argument("--cluster")
            if name == "setup":
                command.add_argument("--node")
    elif family == "node":
        add = commands.add_parser("add")
        add.add_argument("name")
        add.add_argument("--cluster")
        add.add_argument("--target", required=True)
        add.add_argument("--os", choices=("linux", "macos", "windows"), required=True)
        add.add_argument("--architecture", choices=("x86_64", "aarch64"), required=True)
        add.add_argument("--gpus", type=int, required=True)
        remove = commands.add_parser("remove")
        remove.add_argument("name")
        remove.add_argument("--cluster")
    else:
        add = commands.add_parser("add")
        add.add_argument("reference")
        add.add_argument("--cluster")
        add.add_argument("--path", required=True)
        remove = commands.add_parser("remove")
        remove.add_argument("reference")
        remove.add_argument("--cluster")
        for name in ("cordon", "uncordon"):
            command = commands.add_parser(name)
            command.add_argument("reference")
            command.add_argument("--cluster")
    return result


def run_inventory(family: str, arguments: list[str]) -> int:
    args = inventory_parser(family).parse_args(arguments)
    try:
        if family == "cluster" and args.action == "add":
            inventory.cluster_add(args.name, args.repository)
        elif family == "cluster" and args.action == "remove":
            inventory.cluster_remove(args.name)
        elif family == "node" and args.action == "add":
            inventory.node_add(args.cluster, args.name, args.target, args.os, args.architecture, args.gpus)
        elif family == "node" and args.action == "remove":
            inventory.node_remove(args.cluster, args.name)
        elif family == "workspace" and args.action == "add":
            inventory.workspace_add(args.cluster, args.reference, args.path)
        elif family == "workspace" and args.action == "remove":
            inventory.workspace_remove(args.cluster, args.reference)
        elif family == "workspace" and args.action == "cordon":
            inventory.workspace_cordon(args.cluster, args.reference)
        elif family == "workspace" and args.action == "uncordon":
            inventory.workspace_uncordon(args.cluster, args.reference)
        elif family == "cluster" and args.action == "setup":
            return setup.setup_cluster(
                get_cluster(args.cluster, present=False), node_name=args.node
            )
        elif family == "cluster" and args.action == "verify":
            return setup.verify_cluster(get_cluster(args.cluster))
        elif family == "cluster" and args.action == "uncordon":
            inventory.cluster_uncordon(args.cluster)
        else:
            print(f"nk cluster {args.action}: not implemented", file=sys.stderr)
            return 2
    except ConfigError as exc:
        print(f"ERROR\t{exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "_node":
        return setup.internal(arguments[1:])
    if not arguments or arguments == ["--help"] or arguments == ["-h"]:
        parser().print_help(sys.stdout if arguments else sys.stderr)
        return 0 if arguments else 2
    command, rest = arguments[0], arguments[1:]
    handlers: dict[str, Command] = {
        "task": task.main,
        "scheduler": scheduler.main,
    }
    handler = handlers.get(command)
    if handler is not None:
        return handler(rest)
    return run_inventory(command, rest)
