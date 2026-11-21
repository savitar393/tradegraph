"""TradeGraph command-line entry point."""

from argparse import ArgumentParser
import json
from pathlib import Path
import sys

from tradegraph.kb.loader import ENGINE_PROFILE_ID, KnowledgeBaseError, compile_kb, verify_roundtrip
from tradegraph.kb.roundtrip import restore_roundtrip
from tradegraph.kb.validation import validate_owl


def _run_sources_command(args) -> int:
    from tradegraph.sources.parsers import (
        build_adhoc_source_meta,
        parse_atiga_psr_pdf,
        parse_guidance_html,
        parse_indonesian_statute_pdf,
        parse_pmk_tariff_pdf,
        parse_rcep_psr_pdf,
        read_jsonl,
        resolve_manifest_source,
        write_jsonl,
    )
    from tradegraph.sources.rdf_staging import generate_staging_turtle, validate_trial_merge
    from tradegraph.sources.scrapers import JdihKemendagScraper, JdihKemenkeuScraper, archive_regulation

    if args.command == "scrape":
        if args.portal == "kemenkeu":
            scraper = JdihKemenkeuScraper(delay_seconds=args.delay)
            records = scraper.scrape(args.query, bentuk=args.bentuk, limit=args.limit)
        else:
            scraper = JdihKemendagScraper(delay_seconds=args.delay)
            records = scraper.scrape(args.query, tahun=args.tahun, status=args.status, limit=args.limit)

        archived = []
        if args.download_dir is not None:
            for item in records:
                source_rec = archive_regulation(item, args.download_dir)
                if source_rec is not None:
                    archived.append({"slug": item.slug, "id": source_rec.id, "local_ref": source_rec.local_ref})

        written = write_jsonl(args.output, records)
        print(json.dumps({
            "portal": args.portal,
            "query": args.query,
            "count": written,
            "output": args.output.as_posix(),
            "archived": archived,
        }))
        return 0

    if args.command == "parse":
        if args.source_key:
            source_path, meta = resolve_manifest_source(args.manifest, args.source_key)
        elif args.input_path:
            source_path = args.input_path
            meta = build_adhoc_source_meta(source_path, source_key=args.input_path.stem.upper())
        else:
            raise ValueError("Provide either --source-key or --input.")

        if args.kind == "psr":
            parsed = parse_atiga_psr_pdf(
                source_path, source_meta=meta, start_page=args.start_page,
                end_page=args.end_page, max_records=args.max_records,
            )
        elif args.kind == "rcep-psr":
            parsed = parse_rcep_psr_pdf(
                source_path, source_meta=meta, start_page=args.start_page,
                end_page=args.end_page, max_records=args.max_records,
            )
        elif args.kind == "tariff":
            parsed = parse_pmk_tariff_pdf(
                source_path, source_meta=meta, start_page=args.start_page,
                end_page=args.end_page, max_records=args.max_records,
            )
        elif args.kind == "statute":
            parsed = parse_indonesian_statute_pdf(
                source_path, source_meta=meta, start_page=args.start_page,
                end_page=args.end_page, max_records=args.max_records,
            )
        else:
            parsed = parse_guidance_html(source_path, source_meta=meta, max_records=args.max_records)

        written = write_jsonl(args.output, parsed)
        print(json.dumps({
            "kind": args.kind,
            "source_key": meta.source_key,
            "count": written,
            "output": args.output.as_posix(),
        }))
        return 0

    if args.command == "ingest-batch":
        from tradegraph.sources.batch_ingest import ingest_hs_batch

        hs_codes = [c.strip() for c in args.hs_codes.split(",") if c.strip()]
        hs_prefixes = [p.strip() for p in args.hs_prefixes.split(",") if p.strip()]
        agreements = tuple(a.strip().upper() for a in args.agreements.split(",") if a.strip())
        report = ingest_hs_batch(
            args.manifest,
            hs8_codes=hs_codes,
            hs_prefixes=hs_prefixes,
            max_hs=args.max_hs,
            output_dir=args.output_dir,
            include_agreements=agreements,
            emit_ttl=args.emit_ttl,
            canonical_kb=args.canonical_kb if args.emit_ttl else None,
        )
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "to-rdf":
        records: list[dict] = []
        for path in args.inputs:
            records.extend(read_jsonl(path))
        canonical = args.canonical_kb if args.canonical_kb.is_file() else None
        turtle, summary = generate_staging_turtle(
            records,
            canonical_kb_path=canonical,
            verify=not args.allow_unverified,
            review_status=args.review_status,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(turtle, encoding="utf-8")
        payload = {
            "output": args.output.as_posix(),
            "records_processed": summary.records_processed,
            "verified_records": summary.verified_records,
            "flagged_records": summary.flagged_records,
            "flagged_diagnostics": list(summary.flagged_diagnostics),
            "triples_generated": summary.triples_generated,
            "source_documents": summary.source_documents_emitted,
            "source_clauses": summary.source_clauses_emitted,
            "hs_subheadings": summary.hs_subheadings_emitted,
            "hs_codes": summary.hs_codes_emitted,
            "psr_rules": summary.psr_rules_emitted,
            "origin_thresholds": summary.origin_thresholds_emitted,
            "legal_relations": summary.legal_relations_emitted,
            "skipped_existing": list(summary.skipped_existing_iris),
        }
        if args.validate:
            if canonical is None:
                raise ValueError(f"Canonical KB not found for trial validation: {args.canonical_kb}")
            result = validate_trial_merge(
                canonical, args.output, engine_profile_id=args.engine_profile,
                run_owl_validator=not args.skip_owl_check,
            )
            payload["validation"] = {
                "kb_compiled": result.kb_compiled,
                "owl_validated": result.owl_validated,
                "canonical_sha256": result.canonical_sha256,
                "merged_sha256": result.merged_sha256,
                "canonical_owl_triples": result.canonical_owl_triples,
                "merged_owl_triples": result.merged_owl_triples,
                "diagnostics": list(result.diagnostics),
            }
            print(json.dumps(payload))
            return 0 if result.kb_compiled and result.owl_validated is not False else 1
        print(json.dumps(payload))
        return 0

    if args.command == "monitor":
        from tradegraph.sources.monitor import DEFAULT_WATCHLIST, RegulatoryMonitor

        if args.list_targets:
            print(json.dumps({"targets": [t.to_dict() for t in DEFAULT_WATCHLIST]}, indent=args.indent, ensure_ascii=False))
            return 0

        monitor = RegulatoryMonitor(
            args.manifest,
            args.canonical_kb,
            engine_profile_id=args.engine_profile,
        )
        if args.offline_audit:
            audit = monitor.audit_local_freshness(
                as_of_date=args.as_of_date,
                stale_days=args.stale_days,
                verify_hashes=args.verify_hashes,
            )
            payload = audit.to_dict()
            print(json.dumps(payload, indent=args.indent, ensure_ascii=False))
            if args.fail_on_critical and audit.overall_health == "ACTION_REQUIRED":
                return 2
            return 0

        if args.input_jsonl is not None:
            replay_records = read_jsonl(args.input_jsonl)
            scan_report = monitor.evaluate_regulations(
                replay_records,
                targets_checked=(args.input_jsonl.name,),
                mode="replay",
                as_of_date=args.as_of_date,
            )
        else:
            target_ids = [t.strip() for t in args.targets.split(",") if t.strip()] if args.targets else None
            portals = [p.strip() for p in args.portals.split(",") if p.strip()] if args.portals else None
            scan_report = monitor.scan_live(
                target_ids=target_ids,
                portals=portals,
                custom_query=args.query,
                custom_portal=args.portal,
                tahun=args.tahun,
                limit_per_target=args.limit,
                delay_seconds=args.delay,
                as_of_date=args.as_of_date,
            )

        output_payload = scan_report.to_dict(include_regulations=False)
        staging_result = None
        if args.stage_dir is not None or args.validate:
            staging_result = monitor.build_staging_diff(
                scan_report,
                output_dir=args.stage_dir,
                archive_pdfs=args.archive_pdfs,
                validate_merge=args.validate,
                run_owl_validator=not args.skip_owl_check,
            )
            output_payload["staging"] = staging_result.to_dict(include_turtle=args.stage_dir is None)

        print(json.dumps(output_payload, indent=args.indent, ensure_ascii=False))
        if staging_result is not None and staging_result.validation is not None:
            if not staging_result.validation.kb_compiled or staging_result.validation.owl_validated is False:
                return 1
        if args.fail_on_critical and scan_report.critical_count > 0:
            return 2
        return 0

    raise ValueError(f"Unsupported sources command: {args.command}")


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

    sources = groups.add_parser("sources", help="Scrape, parse, and stage trade sources into trial RDF")
    src_cmds = sources.add_subparsers(dest="command", required=True)

    scrape = src_cmds.add_parser("scrape", help="Scrape regulation metadata from JDIH Kemenkeu or Kemendag")
    scrape.add_argument("--portal", choices=("kemenkeu", "kemendag"), required=True)
    scrape.add_argument("--query", required=True)
    scrape.add_argument("--limit", type=int, default=3)
    scrape.add_argument("--bentuk", default="Peraturan Menteri")
    scrape.add_argument("--tahun", type=int, default=None)
    scrape.add_argument("--status", default=None)
    scrape.add_argument("--delay", type=float, default=0.5)
    scrape.add_argument("--download-dir", type=Path, default=None)
    scrape.add_argument("--output", type=Path, required=True)

    parse = src_cmds.add_parser("parse", help="Parse an archived PDF or HTML source into structured JSONL")
    parse.add_argument("--kind", choices=("psr", "rcep-psr", "tariff", "statute", "html"), required=True)
    parse.add_argument("--source-key", default=None)
    parse.add_argument("--input", dest="input_path", type=Path, default=None)
    parse.add_argument("--manifest", type=Path, default=Path("sources/manifest.json"))
    parse.add_argument("--start-page", type=int, default=1)
    parse.add_argument("--end-page", type=int, default=None)
    parse.add_argument("--max-records", type=int, default=None)
    parse.add_argument("--output", type=Path, required=True)

    to_rdf = src_cmds.add_parser("to-rdf", help="Convert staged JSONL files into trial OWL 2 DL Turtle")
    to_rdf.add_argument("inputs", nargs="+", type=Path)
    to_rdf.add_argument("--output", type=Path, required=True)
    to_rdf.add_argument("--canonical-kb", type=Path, default=Path("knowledge/tradegraph.ttl"))
    to_rdf.add_argument("--review-status", default=None)
    to_rdf.add_argument("--allow-unverified", action="store_true")
    to_rdf.add_argument("--validate", action="store_true")
    to_rdf.add_argument("--skip-owl-check", action="store_true")
    to_rdf.add_argument("--engine-profile", default=ENGINE_PROFILE_ID)

    ingest_batch = src_cmds.add_parser("ingest-batch", help="Bounded multi-chapter ingestion across tariffs, PSRs, and Lartas PDFs")
    ingest_batch.add_argument("--hs-codes", default="", help="Comma-separated HS8 codes (e.g. 7208.10.00,8507.60.33)")
    ingest_batch.add_argument("--hs-prefixes", default="", help="Comma-separated HS2/HS4/HS6 prefixes to sample from MFN")
    ingest_batch.add_argument("--max-hs", type=int, default=20, help="Hard upper cap on HS8 codes processed")
    ingest_batch.add_argument("--manifest", type=Path, default=Path("sources/manifest.json"))
    ingest_batch.add_argument("--output-dir", type=Path, required=True)
    ingest_batch.add_argument("--emit-ttl", type=Path, default=None)
    ingest_batch.add_argument("--canonical-kb", type=Path, default=Path("knowledge/tradegraph.ttl"))
    ingest_batch.add_argument("--agreements", default="MFN,ATIGA,ACFTA,IJEPA,IKCEPA,IACEPA,RCEP")

    monitor_cmd = src_cmds.add_parser(
        "monitor",
        help="Monitor JDIH Kemenkeu/Kemendag freshness, diff against manifest/KB, and stage trial OWL Turtle",
    )
    monitor_cmd.add_argument("--list-targets", action="store_true", help="List curated bounded JDIH watchlist targets and exit")
    monitor_cmd.add_argument("--offline-audit", action="store_true", help="Run zero-network freshness, integrity, and KB-vs-manifest gap audit")
    monitor_cmd.add_argument("--verify-hashes", action="store_true", help="Verify SHA-256 checksums of archived local files during --offline-audit")
    monitor_cmd.add_argument("--stale-days", type=int, default=180, help="Flag archived snapshots older than N days during --offline-audit")
    monitor_cmd.add_argument("--input-jsonl", type=Path, default=None, help="Offline replay: diff scraped regulation JSONL instead of querying live JDIH")
    monitor_cmd.add_argument("--targets", default=None, help="Comma-separated watchlist target IDs (e.g. bea-keluar,lartas-ekspor)")
    monitor_cmd.add_argument("--portals", default=None, help="Comma-separated portal filter: kemenkeu,kemendag")
    monitor_cmd.add_argument("--query", default=None, help="Optional ad-hoc JDIH search query")
    monitor_cmd.add_argument("--portal", choices=("kemenkeu", "kemendag"), default="kemenkeu", help="Portal for ad-hoc --query")
    monitor_cmd.add_argument("--tahun", type=int, default=None, help="Optional year filter for JDIH Kemendag")
    monitor_cmd.add_argument("--limit", type=int, default=3, help="Max regulations fetched per watch target (1..20, default 3)")
    monitor_cmd.add_argument("--delay", type=float, default=0.5, help="Polite delay between JDIH detail requests in seconds")
    monitor_cmd.add_argument("--as-of-date", default=None, help="Reference date YYYY-MM-DD (defaults to today)")
    monitor_cmd.add_argument("--manifest", type=Path, default=Path("sources/manifest.json"))
    monitor_cmd.add_argument("--canonical-kb", type=Path, default=Path("knowledge/tradegraph.ttl"))
    monitor_cmd.add_argument("--engine-profile", default=ENGINE_PROFILE_ID)
    monitor_cmd.add_argument("--stage-dir", type=Path, default=None, help="Optional directory to write staging_diff.ttl, candidates.jsonl, manifest_patch.json")
    monitor_cmd.add_argument("--archive-pdfs", action="store_true", help="Download fulltext PDFs for unarchived candidates into <stage-dir>/raw")
    monitor_cmd.add_argument("--validate", action="store_true", help="Run trial merge validation (SHACL + OWL 2 DL/HermiT) on staged diff")
    monitor_cmd.add_argument("--skip-owl-check", action="store_true", help="Skip Java OWLAPI/HermiT check during --validate")
    monitor_cmd.add_argument("--fail-on-critical", action="store_true", help="Exit with code 2 if critical regulatory findings or revocations are detected")
    monitor_cmd.add_argument("--indent", type=int, default=2)

    opt = groups.add_parser("optimize", help="Rank eligible Multi-FTA schemes with RoO, Lartas, landed import taxes, and compliance plan")
    opt.add_argument("case_file", type=Path, help="JSON shipment specification")
    opt.add_argument("--kb", type=Path, default=Path("knowledge/tradegraph.ttl"))
    opt.add_argument("--engine-profile", default=ENGINE_PROFILE_ID)
    opt.add_argument("--agreements", default=None, help="Comma-separated agreement filter (e.g. atiga,rcep)")
    opt.add_argument("--plan", action="store_true", help="Generate KB-driven FTA + Lartas compliance action steps")
    opt.add_argument("--explain", action="store_true", help="Resolve full primary source citations and audit-grade provenance explanations")
    opt.add_argument("--indent", type=int, default=2)

    assess = groups.add_parser("assess", help="Assess one explicit FTA scheme for a JSON shipment specification")
    assess.add_argument("case_file", type=Path, help="JSON shipment specification")
    assess.add_argument("--agreement", required=True, help="Agreement slug (atiga, rcep, acfta, ijepa, ikcepa, iacepa)")
    assess.add_argument("--kb", type=Path, default=Path("knowledge/tradegraph.ttl"))
    assess.add_argument("--engine-profile", default=ENGINE_PROFILE_ID)
    assess.add_argument("--plan", action="store_true", help="Generate KB-driven FTA + Lartas compliance action steps")
    assess.add_argument("--explain", action="store_true", help="Resolve full primary source citations and audit-grade provenance explanations")
    assess.add_argument("--indent", type=int, default=2)

    exp_cmd = groups.add_parser("export", help="Evaluate outbound Indonesian export compliance (PEB, Export Lartas, Bea Keluar, Back-to-Back CO, Third-Party Invoicing, Cumulation)")
    exp_cmd.add_argument("case_file", type=Path, help="JSON export specification")
    exp_cmd.add_argument("--kb", type=Path, default=Path("knowledge/tradegraph.ttl"))
    exp_cmd.add_argument("--engine-profile", default=ENGINE_PROFILE_ID)
    exp_cmd.add_argument("--explain", action="store_true", help="Resolve full primary source legal citations")
    exp_cmd.add_argument("--indent", type=int, default=2)

    batch_cmd = groups.add_parser("batch", help="Run multi-SKU Import (PIB) or Export (PEB + Bea Keluar) batch screening and export CSV/Excel audit reports")
    batch_cmd.add_argument("input_file", type=Path, help="Batch file (.json, .csv, or .xlsx)")
    batch_cmd.add_argument("--mode", choices=("import", "export"), required=True, help="Screening mode: import (PIB) or export (PEB)")
    batch_cmd.add_argument("--depth", choices=("full", "fast_lartas"), default="full", help="Import screening depth (full HermiT RoO or fast_lartas)")
    batch_cmd.add_argument("--kb", type=Path, default=Path("knowledge/tradegraph.ttl"))
    batch_cmd.add_argument("--engine-profile", default=ENGINE_PROFILE_ID)
    batch_cmd.add_argument("--explain", action="store_true", help="Include deduplicated primary legal citations")
    batch_cmd.add_argument("--out-csv", type=Path, default=None, help="Optional output path for UTF-8 CSV audit report")
    batch_cmd.add_argument("--out-xlsx", type=Path, default=None, help="Optional output path for styled Excel (.xlsx) audit report")
    batch_cmd.add_argument("--indent", type=int, default=2)

    impact_cmd = groups.add_parser("impact", help="Analyze temporal regulatory change diff & portfolio impact between two dates (What-If)")
    impact_cmd.add_argument("input_file", type=Path, help="Portfolio batch file (.json, .csv, or .xlsx)")
    impact_cmd.add_argument("--mode", choices=("import", "export"), required=True, help="Portfolio direction: import (PIB) or export (PEB)")
    impact_cmd.add_argument("--from-date", required=True, help="Baseline date YYYY-MM-DD")
    impact_cmd.add_argument("--to-date", required=True, help="Target date YYYY-MM-DD")
    impact_cmd.add_argument("--depth", choices=("fast_lartas", "full"), default="fast_lartas", help="Import screening depth")
    impact_cmd.add_argument("--kb", type=Path, default=Path("knowledge/tradegraph.ttl"))
    impact_cmd.add_argument("--engine-profile", default=ENGINE_PROFILE_ID)
    impact_cmd.add_argument("--explain", action="store_true", help="Include deduplicated primary legal citations")
    impact_cmd.add_argument("--out-csv", type=Path, default=None, help="Optional output path for UTF-8 CSV impact report")
    impact_cmd.add_argument("--out-xlsx", type=Path, default=None, help="Optional output path for styled 4-sheet Excel (.xlsx) impact report")
    impact_cmd.add_argument("--indent", type=int, default=2)

    serve = groups.add_parser("serve", help="Run the FastAPI HTTP API and Interactive Compliance Workbench")
    serve.add_argument("--kb", type=Path, default=Path("knowledge/tradegraph.ttl"))
    serve.add_argument("--engine-profile", default=ENGINE_PROFILE_ID)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--case-db", type=Path, default=None, help="Optional SQLite path for immutable case persistence")
    serve.add_argument("--fuseki-url", default=None, help="Optional Apache Jena Fuseki dataset URL (e.g. http://localhost:3030/tradegraph)")
    serve.add_argument("--fuseki-user", default=None)
    serve.add_argument("--fuseki-password", default=None)
    serve.add_argument("--sync-kb-on-startup", action="store_true", help="Publish canonical KB snapshot to Fuseki on startup")

    fuseki_grp = groups.add_parser("fuseki", help="Manage Apache Jena Fuseki TDB2 persistence and KB sync")
    fuseki_sub = fuseki_grp.add_subparsers(dest="fuseki_command", required=True)
    f_ping = fuseki_sub.add_parser("ping", help="Check Fuseki dataset reachability and latency")
    f_ping.add_argument("--url", required=True, help="Fuseki dataset URL (e.g. http://localhost:3030/tradegraph)")
    f_ping.add_argument("--user", default=None)
    f_ping.add_argument("--password", default=None)
    f_sync = fuseki_sub.add_parser("sync-kb", help="Idempotently publish canonical KB OWL snapshot into Fuseki TDB2")
    f_sync.add_argument("--url", required=True, help="Fuseki dataset URL (e.g. http://localhost:3030/tradegraph)")
    f_sync.add_argument("--kb", type=Path, default=Path("knowledge/tradegraph.ttl"))
    f_sync.add_argument("--engine-profile", default=ENGINE_PROFILE_ID)
    f_sync.add_argument("--user", default=None)
    f_sync.add_argument("--password", default=None)

    args = parser.parse_args(argv)

    try:
        if args.group == "sources":
            return _run_sources_command(args)

        if args.group == "serve":
            import uvicorn
            from tradegraph.api.app import create_app

            app = create_app(
                kb_path=args.kb,
                engine_profile=args.engine_profile,
                case_db_path=args.case_db,
                fuseki_url=args.fuseki_url,
                fuseki_user=args.fuseki_user,
                fuseki_password=args.fuseki_password,
                sync_kb_on_startup=args.sync_kb_on_startup or None,
            )
            uvicorn.run(app, host=args.host, port=args.port)
            return 0

        if args.group == "fuseki":
            from tradegraph.graph.store import FusekiGraphStore

            store = FusekiGraphStore(args.url, username=args.user, password=args.password)
            if args.fuseki_command == "ping":
                info = store.ping()
                print(json.dumps(info, indent=2))
                return 0 if info.get("reachable") else 1
            compiled = compile_kb(args.kb, args.engine_profile)
            synced = store.publish_kb(compiled)
            print(json.dumps(synced, indent=2))
            return 0

        if args.group == "export":
            from dataclasses import replace
            from tradegraph.kb.validation import resolve_runtime_classpath
            from tradegraph.optimization.export_service import ExportComplianceAdvisor, ExportSpec
            from tradegraph.optimization.service import MultiFtaOptimizer

            raw = json.loads(args.case_file.read_text(encoding="utf-8"))
            spec = ExportSpec.from_dict(raw)
            if args.explain and not spec.include_explain:
                spec = replace(spec, include_explain=True)
            compiled = compile_kb(args.kb, args.engine_profile)
            advisor = ExportComplianceAdvisor(MultiFtaOptimizer(compiled, resolve_runtime_classpath()))
            result = advisor.evaluate(spec)
            print(json.dumps(result.to_dict(), indent=args.indent, ensure_ascii=False))
            return 0

        if args.group == "batch":
            from tradegraph.kb.validation import resolve_runtime_classpath
            from tradegraph.optimization.batch_service import BatchScreener, load_batch_rows_from_file
            from tradegraph.optimization.export_service import ExportComplianceAdvisor
            from tradegraph.optimization.service import MultiFtaOptimizer

            rows = load_batch_rows_from_file(args.input_file)
            compiled = compile_kb(args.kb, args.engine_profile)
            optimizer = MultiFtaOptimizer(compiled, resolve_runtime_classpath())
            screener = BatchScreener(optimizer, ExportComplianceAdvisor(optimizer))
            result = screener.screen(
                rows,
                mode=args.mode,
                screening_depth=args.depth,
                include_explain=args.explain,
            )
            if args.out_csv is not None:
                args.out_csv.parent.mkdir(parents=True, exist_ok=True)
                args.out_csv.write_bytes(result.to_csv_bytes())
            if args.out_xlsx is not None:
                args.out_xlsx.parent.mkdir(parents=True, exist_ok=True)
                args.out_xlsx.write_bytes(result.to_xlsx_bytes())
            print(json.dumps(result.to_dict(), indent=args.indent, ensure_ascii=False))
            return 0

        if args.group == "impact":
            from tradegraph.kb.validation import resolve_runtime_classpath
            from tradegraph.optimization.batch_service import BatchScreener, load_batch_rows_from_file
            from tradegraph.optimization.export_service import ExportComplianceAdvisor
            from tradegraph.optimization.impact_service import RegulatoryImpactAnalyzer
            from tradegraph.optimization.service import MultiFtaOptimizer

            rows = load_batch_rows_from_file(args.input_file)
            compiled = compile_kb(args.kb, args.engine_profile)
            optimizer = MultiFtaOptimizer(compiled, resolve_runtime_classpath())
            screener = BatchScreener(optimizer, ExportComplianceAdvisor(optimizer))
            analyzer = RegulatoryImpactAnalyzer(optimizer, screener)
            result = analyzer.analyze_portfolio(
                rows,
                mode=args.mode,
                baseline_date=args.from_date,
                target_date=args.to_date,
                screening_depth=args.depth,
                include_explain=args.explain,
            )
            if args.out_csv is not None:
                args.out_csv.parent.mkdir(parents=True, exist_ok=True)
                args.out_csv.write_bytes(result.to_csv_bytes())
            if args.out_xlsx is not None:
                args.out_xlsx.parent.mkdir(parents=True, exist_ok=True)
                args.out_xlsx.write_bytes(result.to_xlsx_bytes())
            print(json.dumps(result.to_dict(), indent=args.indent, ensure_ascii=False))
            return 0

        if args.group in {"optimize", "assess"}:
            from dataclasses import replace
            from tradegraph.kb.validation import resolve_runtime_classpath
            from tradegraph.optimization.service import MultiFtaOptimizer, ShipmentSpec

            raw = json.loads(args.case_file.read_text(encoding="utf-8"))
            spec = ShipmentSpec.from_dict(raw)
            if args.plan and not spec.include_plan:
                spec = replace(spec, include_plan=True)
            if args.explain and not spec.include_explain:
                spec = replace(spec, include_explain=True)
            compiled = compile_kb(args.kb, args.engine_profile)
            classpath = resolve_runtime_classpath()
            optimizer = MultiFtaOptimizer(compiled, classpath)
            if args.group == "assess":
                ev = optimizer.assess_scheme(spec, args.agreement)
                print(json.dumps(ev.to_dict(), indent=args.indent))
                return 0
            agreements = (
                tuple(item.strip().lower() for item in args.agreements.split(",") if item.strip())
                if args.agreements else None
            )
            result = optimizer.optimize(spec, agreements=agreements)
            print(json.dumps(result.to_dict(), indent=args.indent))
            return 0

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
    except (KnowledgeBaseError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"conforms": False, "findings": (str(error),)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
