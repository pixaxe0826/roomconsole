"""python -m benchmarks: list / validate / run / compare / offline M2 diagnostics."""
import argparse
import json
from pathlib import Path
import sys
from .dataset import DEFAULT_ROOT, list_suites, load_suite
from .reports import compare_runs, score_label
from .runner import run_suite
from .paths import result_directory, no_links


def main(argv=None):
    parser = argparse.ArgumentParser(description='Room Hub text-only assistant benchmark (isolated synthetic state).')
    parser.add_argument('--suite-root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--results-root', type=Path, default=Path('artifacts/benchmarks'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    validate = sub.add_parser('validate'); validate.add_argument('--suite', action='append', required=True)
    run = sub.add_parser('run')
    run.add_argument('--suite', action='append', required=True)
    run.add_argument('--name', default='baseline-v1')
    run.add_argument('--mode', choices=['nlu', 'decision', 'full'], default='full')
    run.add_argument('--category', action='append', default=[])
    run.add_argument('--tag', action='append', default=[])
    run.add_argument('--case', action='append', default=[])
    run.add_argument('--source-type', choices=['unique', 'paraphrase'])
    run.add_argument('--split')
    run.add_argument('--llm', choices=['disabled', 'local'], default='disabled', help='local explicitly opts into loopback inference')
    run.add_argument('--endpoint', default='http://127.0.0.1:8090/v1')
    run.add_argument('--model', default='Qwen3-0.6B-Q5_K_M.gguf')
    run.add_argument('--timeout', type=int, default=180)
    run.add_argument('--max-tokens', type=int, default=256)
    run.add_argument('--quiet', action='store_true')
    run.add_argument('--limit', type=int, help='First N selected cases (smoke only; recorded in results)')
    compare = sub.add_parser('compare')
    compare.add_argument('before'); compare.add_argument('after')
    compare.add_argument('--output', type=Path)
    compare.add_argument('--allow-incompatible', action='store_true')
    analyze = sub.add_parser('analyze', help='Offline diagnostics from a finished run; no dataset/model needed')
    analyze.add_argument('before')
    analyze.add_argument('--name', required=True)
    verify = sub.add_parser('verify-analysis', help='Recheck original and derived output hashes')
    verify.add_argument('name')
    comparison = sub.add_parser('compare-analysis', help='Compare the fixed common supported cohort')
    comparison.add_argument('before'); comparison.add_argument('after')
    comparison.add_argument('--name', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'list':
            for name in list_suites(args.suite_root): print(name)
        elif args.command == 'validate':
            for name in args.suite:
                suite = load_suite(name, args.suite_root)
                print(json.dumps({'suite': name, 'version': suite.manifest['version'], 'cases': len(suite.cases),
                                  'sha256': suite.digest, 'warnings': suite.warnings, 'valid': True}, ensure_ascii=False))
        elif args.command == 'run':
            if not 10 <= args.timeout <= 600 or not 16 <= args.max_tokens <= 1024:
                raise ValueError('timeout must be 10..600, max-tokens 16..1024')
            # Validate endpoint with the existing production contract, before starting a worker.
            from app.llm import LLMConfig
            LLMConfig(base_url=args.endpoint, model=args.model)
            for name in args.suite:
                suite = load_suite(name, args.suite_root)
                rows = suite.select(categories=args.category, tags=args.tag, ids=args.case,
                                    source_type=args.source_type, split=args.split)
                if args.case and set(args.case) - {c['id'] for c in suite.cases}:
                    raise ValueError('Unknown case ID in selector')
                if args.limit is not None:
                    if args.limit < 1:
                        raise ValueError('limit must be positive')
                    rows = rows[:args.limit]
                run_name = args.name if len(args.suite) == 1 else args.name + '-' + name
                folder, metrics = run_suite(suite, rows, name=run_name, results_root=args.results_root,
                    mode=args.mode, backend={'llm': args.llm, 'endpoint': args.endpoint, 'model': args.model,
                                            'timeout': args.timeout, 'max_tokens': args.max_tokens}, quiet=args.quiet)
                print(f"\nROOM HUB BENCHMARK\nSuite: {name}\nCases: {metrics['processed']}\nEvaluated: {metrics['evaluated']}\n"
                      f"Mode success: {score_label(metrics['mode_success'])}\nFalse Execution: {score_label(metrics['false_execution'])}\n"
                      f"LLM blocked: {metrics['llm_blocked']}\nReport: {folder / 'summary.html'}")
        elif args.command == 'analyze':
            from .analysis import analyze_run
            folder, state, metrics = analyze_run(args.results_root, args.before, args.name)
            print(json.dumps({'analysis': state['name'], 'status': state['run_status'],
                'source_run': state['source_run'], 'original_files_unchanged': True,
                'original_metrics_preserved': True, 'source_match': True, 'model_calls_during_analysis': 0,
                'overall': metrics['overall_preserved']['task_success'],
                'supported': metrics['supported']['task_success'], 'support': metrics['support'],
                'report': str(folder / 'summary.html')}, ensure_ascii=False, indent=2))
        elif args.command == 'verify-analysis':
            from .analysis import verify_analysis
            print(json.dumps(verify_analysis(args.results_root, args.name), ensure_ascii=False, indent=2))
        elif args.command == 'compare-analysis':
            from .analysis_compare import compare_analyses
            folder, result = compare_analyses(args.results_root, args.before, args.after, args.name)
            print(json.dumps({'output': str(folder), **result}, ensure_ascii=False, indent=2))
        else:
            from .dataset import NAME
            if not NAME.fullmatch(args.before) or not NAME.fullmatch(args.after):
                raise ValueError('Comparison run names must be simple identifiers')
            args.results_root = result_directory(args.results_root)
            before, after = args.results_root / args.before, args.results_root / args.after
            no_links(before); no_links(after)
            output = args.output or args.results_root / (args.before + '-vs-' + args.after)
            output = result_directory(output)
            result = compare_runs(before, after, output, args.allow_incompatible)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, RecursionError) as exc:
        print(f'Benchmark error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
