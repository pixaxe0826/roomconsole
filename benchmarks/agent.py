"""python -m benchmarks.agent: isolated Personal Agent preflight / run.

Additive to the frozen legacy benchmark. No production service or PR39 needed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .agent_config import ENGINE, FAST_MODELS, DEEP_MODELS, validate_config
from .dataset import DEFAULT_ROOT, load_suite
from .paths import result_directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite-root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--results-root', type=Path, default=Path('artifacts/benchmarks/personal-agent'))
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('preflight', 'run'):
        cmd = sub.add_parser(command)
        cmd.add_argument('--suite', required=True)
        cmd.add_argument('--category', action='append', default=[])
        cmd.add_argument('--tag', action='append', default=[])
        cmd.add_argument('--case', action='append', default=[])
        cmd.add_argument('--source-type', choices=['unique', 'paraphrase'])
        cmd.add_argument('--split')
        cmd.add_argument('--limit', type=int)
        cmd.add_argument('--agent-mode', choices=['auto', 'fast', 'deep', 'local_only'], default='auto')
        cmd.add_argument('--fast-model', choices=FAST_MODELS, default='gpt-6.1-sol')
        cmd.add_argument('--deep-model', choices=DEEP_MODELS, default='gpt-6.1-sol')
        cmd.add_argument('--reasoning-effort', choices=['low', 'medium', 'high'], default='high')
        cmd.add_argument('--timeout', type=int, default=90)
        cmd.add_argument('--allow-shared-notes', action='store_true', help='Explicitly allow shared fixture notes in cloud context')
        cmd.add_argument('--live', action='store_true', help='Explicit consent to send fixture text/catalog to official OpenAI API')
        cmd.add_argument('--max-cost-usd', help='Required with --live; run-wide application cost cap, not provider billing limit')
        cmd.add_argument('--max-calls', type=int, help='Required with --live; cap persists across all fixture/database resets')
    sub.choices['run'].add_argument('--name', required=True, help='New unique output directory; no overwrite/resume')
    sub.choices['run'].add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.live and (args.max_cost_usd is None or args.max_calls is None):
            raise ValueError('--live requires explicit --max-cost-usd AND --max-calls')
        config = validate_config({
            'live': args.live, 'timeout': args.timeout, 'agent_mode': args.agent_mode,
            'fast_model': args.fast_model, 'deep_model': args.deep_model,
            'reasoning_effort': args.reasoning_effort, 'allow_shared_notes': args.allow_shared_notes,
            'max_cost_usd': args.max_cost_usd if args.max_cost_usd is not None else '1.00',
            'max_calls': args.max_calls if args.max_calls is not None else 250,
        })
        suite = load_suite(args.suite, args.suite_root)
        if set(args.case) - {c['id'] for c in suite.cases}:
            raise ValueError('Unknown case selector')
        selected = suite.select(categories=args.category, tags=args.tag, ids=args.case,
            source_type=args.source_type, split=args.split)
        if args.limit is not None:
            if args.limit < 1:
                raise ValueError('limit must be positive')
            selected = selected[:args.limit]
        if not selected:
            raise ValueError('No selected cases')
        results = result_directory(args.results_root, suite.path)
        from .agent_runner import prepare_groups
        prepare_groups(suite, selected, config)  # Validate session prerequisites before dispatch.
        key = any(os.environ.get(k, '').strip() for k in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY'))
        if args.command == 'preflight':
            print(json.dumps({
                'engine': ENGINE, 'suite': suite.name, 'suite_hash': suite.digest,
                'total_cases': len(suite.cases), 'selected_cases': len(selected),
                'valid': True, 'local_prerequisites_ready': not args.live or key,
                'openai_key_configured': key, 'provider_verified': False,
                'api_calls': 0, 'fixture_database_created': False,
                'production_database_accessed': False, 'configuration': config,
                'results_root': str(results), 'warnings': suite.warnings,
                'scope': 'text-only isolated source runtime; not deployed HTTP/UI/STT',
            }, ensure_ascii=False, indent=2))
            return 0 if not args.live or key else 2
        from .agent_runner import run_agent_suite
        folder, metrics, status = run_agent_suite(suite, selected, name=args.name,
            results_root=results, configuration=config, quiet=args.quiet)
        print(json.dumps({'status': status, 'selected': len(selected), 'evaluated': metrics['evaluated'],
            'success': metrics['task_success'], 'api_calls': metrics['budget']['dispatched_calls'],
            'budget_stop': metrics['budget']['stop_reason'], 'report': str(folder / 'summary.html')},
            ensure_ascii=False, indent=2))
        return 0 if status == 'COMPLETED' else 3
    except (ValueError, OSError, KeyError, TypeError, RecursionError) as exc:
        print(f'Personal Agent benchmark configuration error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
