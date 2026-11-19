"""TradeGraph command-line entry point."""

from argparse import ArgumentParser
import json
from pathlib import Path
import sys

from tradegraph.kb.loader import ENGINE_PROFILE_ID, KnowledgeBaseError, compile_kb, verify_roundtrip
from tradegraph.kb.roundtrip import restore_roundtrip
from tradegraph.kb.validation import validate_owl


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="tradegraph")
    groups = parser.add_subparsers(dest="group", required=True)
    kb = groups.add_parser("kb", help="Work with the canonical knowledge base")
    commands = kb.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Compile Turtle and check the OWL and SWRL projections")
    validate.add_argument("path", type=Path)
    validate.add_argument("--engine-profile", default=ENGINE_PROFILE_ID)
    roundtrip = commands.add_parser("verify-roundtrip", help="Check RDF graph isomorphism after saving")
    roundtrip.add_argument("before", type=Path)
    roundtrip.add_argument("after", type=Path)
    restore = commands.add_parser("restore-roundtrip", help="Recover RDF after a verified no-edit save")
    restore.add_argument("before", type=Path)
    restore.add_argument("after", type=Path)
    restore.add_argument("destination", type=Path)
    restore.add_argument("--engine-profile", default=ENGINE_PROFILE_ID)
    args = parser.parse_args(argv)

    try:
        if args.command == "verify-roundtrip":
            report = verify_roundtrip(args.before, args.after)
            print(json.dumps({"conforms": report.conforms, "findings": report.findings}))
            return 0 if report.conforms else 1
        if args.command == "restore-roundtrip":
            report = restore_roundtrip(args.before, args.after, args.destination, args.engine_profile)
            print(json.dumps({"conforms": report.conforms, "findings": report.findings}))
            return 0 if report.conforms else 1

        compiled = compile_kb(args.path, args.engine_profile)
        report = validate_owl(compiled)
        print(json.dumps({
            "conforms": report.conforms,
            "kb_id": compiled.snapshot.kb_id,
            "version": compiled.snapshot.version,
            "sha256": compiled.snapshot.sha256,
            "engine_profile_id": compiled.snapshot.engine_profile_id,
            "owl_profile": "OWL 2 DL",
            "swrl_rules": len(compiled.rule_atom_counts),
            "shape_triples": len(compiled.dataset.graph(compiled.shapes_graph_ref)),
            "fields": sorted(compiled.field_catalog),
            "queries": sorted(compiled.query_catalog),
            "actions": sorted(compiled.action_catalog),
            "findings": report.findings,
        }))
        return 0 if report.conforms else 1
    except (KnowledgeBaseError, OSError) as error:
        print(json.dumps({"conforms": False, "findings": (str(error),)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
