#!/usr/bin/env python3
"""CLI tool to manage the method registry."""

import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from registry.repository import MethodRegistry
from registry.models import MethodStatus


def cmd_list(args):
    r = MethodRegistry()
    methods = r.find_methods(country=args.country, document_type=args.doc_type)
    if args.status:
        methods = [m for m in methods if m.status.name == args.status.upper()]
    if not methods:
        print("No methods found.")
        return
    for m in methods:
        print(f"  [{m.status.name:10}]  {m.method_id:20}  {m.country or '?':30}  {m.document_type or '?'}")


def cmd_show(args):
    r = MethodRegistry()
    m = r.get_method(args.method_id)
    if not m:
        print(f"Method '{args.method_id}' not found.")
        return
    print(json.dumps(m.model_dump(), indent=2, default=str))


def cmd_delete(args):
    r = MethodRegistry()
    if args.method_id == "ALL":
        methods = r.find_methods()
        for m in methods:
            r.delete_method(m.method_id)
            print(f"  Deleted {m.method_id}")
        print(f"Deleted {len(methods)} method(s).")
        return
    if r.delete_method(args.method_id):
        print(f"Deleted {args.method_id}.")
    else:
        print(f"Method '{args.method_id}' not found.")


def cmd_cleanup(args):
    r = MethodRegistry()
    removed = []
    for m in r.find_methods():
        if m.status in (MethodStatus.UNHEALTHY, MethodStatus.INACTIVE):
            r.delete_method(m.method_id)
            removed.append(m.method_id)
            print(f"  Deleted [{m.status.name}] {m.method_id}")
    print(f"\nCleaned up {len(removed)} method(s).")


def main():
    parser = argparse.ArgumentParser(
        description="Manage the DVS method registry.",
        prog="registry_tool",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", aliases=["ls"], help="List all methods")
    p_list.add_argument("--country", help="Filter by country")
    p_list.add_argument("--doc-type", help="Filter by document type")
    p_list.add_argument("--status", help="Filter by status (ACTIVE, INACTIVE, UNHEALTHY, TESTING, DEGRADED)")
    p_list.set_defaults(func=cmd_list)

    # show
    p_show = sub.add_parser("show", help="Show full details of a method")
    p_show.add_argument("method_id", help="Method ID to inspect")
    p_show.set_defaults(func=cmd_show)

    # delete
    p_del = sub.add_parser("delete", aliases=["rm"], help="Delete a method (use 'ALL' to wipe)")
    p_del.add_argument("method_id", help="Method ID to delete, or 'ALL'")
    p_del.set_defaults(func=cmd_delete)

    # cleanup
    p_clean = sub.add_parser("cleanup", help="Delete all UNHEALTHY and INACTIVE methods")
    p_clean.set_defaults(func=cmd_cleanup)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
