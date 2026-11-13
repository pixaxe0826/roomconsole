"""Monthly $100 OpenAI API budget governor and microdollar usage ledger.

All costs are tracked in integer microdollars (1 USD = 1,000,000 microdollars)
in SQLite so floating-point rounding never drifts across thousands of requests.
When monthly spend reaches soft/conservation/hard thresholds, the governor
automatically downshifts models (gpt-4o -> gpt-4o-mini, gpt-4o-transcribe ->
gpt-4o-mini-transcribe) and falls back to Tier 0 deterministic rules or local
Whisper+Qwen at the $100 hard cap.
"""
from __future__ import annotations

import calendar
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import math
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from .store import uid, utcnow

MICRODOLLARS_PER_USD = 1_000_000

# Official OpenAI API pricing in microdollars:
# - Token rates are microdollars per 1,000,000 tokens (i.e. USD per 1M tokens * 1,000,000 -> microdollars per 1M,
#   or equivalently USD per 1M tokens numerically equals microdollars per 1 token!).
#   Wait: $0.15 per 1M tokens = 0.15 microdollars per token = 150,000 microdollars per 1M tokens.
# - Audio rates are microdollars per minute.
PRICING_CATALOG: dict[str, dict[str, int]] = {
    'gpt-4o-mini': {
        'input_per_1m_micros': 150_000,       # $0.15 / 1M input tokens
        'cached_per_1m_micros': 75_000,       # $0.075 / 1M cached input tokens
        'output_per_1m_micros': 600_000,      # $0.60 / 1M output tokens
        'audio_per_min_micros': 0,
    },
    'gpt-4o': {
        'input_per_1m_micros': 2_500_000,     # $2.50 / 1M input tokens
        'cached_per_1m_micros': 1_250_000,    # $1.25 / 1M cached input tokens
        'output_per_1m_micros': 10_000_000,   # $10.00 / 1M output tokens
        'audio_per_min_micros': 0,
    },
    'o3-mini': {
        'input_per_1m_micros': 1_100_000,     # $1.10 / 1M input tokens
        'cached_per_1m_micros': 550_000,      # $0.55 / 1M cached input tokens
        'output_per_1m_micros': 4_400_000,    # $4.40 / 1M output tokens
        'audio_per_min_micros': 0,
    },
    'gpt-4o-mini-transcribe': {
        'input_per_1m_micros': 0,
        'cached_per_1m_micros': 0,
        'output_per_1m_micros': 0,
        'audio_per_min_micros': 3_000,        # $0.003 / minute
    },
    'gpt-4o-transcribe': {
        'input_per_1m_micros': 0,
        'cached_per_1m_micros': 0,
        'output_per_1m_micros': 0,
        'audio_per_min_micros': 6_000,        # $0.006 / minute
    },
    'whisper-1': {
        'input_per_1m_micros': 0,
        'cached_per_1m_micros': 0,
        'output_per_1m_micros': 0,
        'audio_per_min_micros': 6_000,        # $0.006 / minute
    },
    'gpt-4o-mini-tts': {
        'input_per_1m_micros': 0,
        'cached_per_1m_micros': 0,
        'output_per_1m_micros': 0,
        'audio_per_min_micros': 15_000,       # $0.015 / minute
    },
    'local': {
        'input_per_1m_micros': 0,
        'cached_per_1m_micros': 0,
        'output_per_1m_micros': 0,
        'audio_per_min_micros': 0,
    },
}

ALLOWED_CHAT_MODELS = ('gpt-4o-mini', 'gpt-4o', 'o3-mini')
ALLOWED_STT_MODELS = ('gpt-4o-mini-transcribe', 'gpt-4o-transcribe', 'whisper-1')
BudgetState = Literal['NORMAL', 'SOFT_CAP', 'CONSERVATION', 'HARD_CAP_REACHED']
UsageCategory = Literal['stt', 'agent_fast', 'agent_deep', 'briefing', 'chat', 'local_fallback']


class BudgetPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    monthly_cap_usd: float = Field(default=100.0, ge=1.0, le=10000.0)
    soft_cap_ratio: float = Field(default=0.70, ge=0.10, le=0.95)
    conservation_ratio: float = Field(default=0.90, ge=0.20, le=0.99)
    hard_cap_ratio: float = Field(default=1.00, ge=0.50, le=1.00)
    fast_model: Literal['gpt-4o-mini', 'gpt-4o'] = 'gpt-4o-mini'
    deep_model: Literal['gpt-4o', 'o3-mini', 'gpt-4o-mini'] = 'gpt-4o'
    stt_model: Literal['gpt-4o-mini-transcribe', 'gpt-4o-transcribe', 'whisper-1'] = 'gpt-4o-mini-transcribe'
    auto_downgrade: bool = True
    fallback_to_local: bool = True
    prefer_tier0_rules: bool = True


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed_cloud: bool
    state: BudgetState
    selected_model: str
    requested_model: str
    downgraded: bool
    fallback_local: bool
    reason: str
    billing_month: str
    spent_microdollars: int
    cap_microdollars: int
    estimated_call_microdollars: int
    remaining_microdollars: int
    max_output_tokens: int

    def data(self) -> dict:
        d = asdict(self)
        d['spent_usd'] = round(self.spent_microdollars / MICRODOLLARS_PER_USD, 6)
        d['cap_usd'] = round(self.cap_microdollars / MICRODOLLARS_PER_USD, 2)
        d['estimated_call_usd'] = round(self.estimated_call_microdollars / MICRODOLLARS_PER_USD, 6)
        d['remaining_usd'] = round(self.remaining_microdollars / MICRODOLLARS_PER_USD, 6)
        return d


def calculate_cost_microdollars(
    model: str,
    *,
    prompt_tokens: int = 0,
    cached_tokens: int = 0,
    completion_tokens: int = 0,
    audio_seconds: float = 0.0,
) -> int:
    """Compute exact integer microdollar cost (ceil to nearest microdollar)."""
    if model.startswith('local') or model.endswith('.gguf'):
        return 0
    rates = PRICING_CATALOG.get(model)
    if rates is None:
        # Conservative fallback rate (gpt-4o pricing) for unknown cloud model aliases
        rates = PRICING_CATALOG['gpt-4o']
    uncached_prompt = max(0, int(prompt_tokens) - max(0, int(cached_tokens)))
    cached = max(0, min(int(prompt_tokens), int(cached_tokens)))
    completion = max(0, int(completion_tokens))
    seconds = max(0.0, float(audio_seconds))

    token_micros = (
        uncached_prompt * rates['input_per_1m_micros']
        + cached * rates['cached_per_1m_micros']
        + completion * rates['output_per_1m_micros']
    ) / 1_000_000.0
    audio_micros = (seconds / 60.0) * rates['audio_per_min_micros']
    total = token_micros + audio_micros
    return int(math.ceil(total)) if total > 0 else 0


class BudgetGovernor:
    """SQLite-backed $100/month budget tracker and tiered model selector."""

    def __init__(self, store):
        self.store = store
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS hub_ai_budget_policy (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    policy_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hub_ai_usage_ledger (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    billing_month TEXT NOT NULL,
                    category TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    audio_seconds REAL NOT NULL DEFAULT 0.0,
                    cost_microdollars INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ai_usage_month
                    ON hub_ai_usage_ledger(billing_month, created_at DESC);
            ''')
            row = db.execute('SELECT 1 FROM hub_ai_budget_policy WHERE id = 1').fetchone()
            if not row:
                default_policy = BudgetPolicy().model_dump()
                db.execute(
                    'INSERT INTO hub_ai_budget_policy(id, policy_json, updated_at) VALUES(1, ?, ?)',
                    (json.dumps(default_policy, ensure_ascii=False), utcnow()),
                )

    @contextmanager
    def _db(self):
        with closing(self.store.connect()) as db:
            with db:
                yield db

    def current_billing_month(self, at_iso: str | None = None) -> str:
        settings = self.store.get('settings') or {'timezone': 'Asia/Seoul'}
        tz_name = settings.get('timezone', 'Asia/Seoul')
        stamp = datetime.fromisoformat(at_iso or utcnow()).astimezone(ZoneInfo(tz_name))
        return f'{stamp.year:04d}-{stamp.month:02d}'

    def policy(self) -> BudgetPolicy:
        with self._db() as db:
            row = db.execute('SELECT policy_json FROM hub_ai_budget_policy WHERE id = 1').fetchone()
        if not row:
            return BudgetPolicy()
        return BudgetPolicy.model_validate(json.loads(row['policy_json']))

    def set_policy(self, policy: BudgetPolicy) -> BudgetPolicy:
        if not (policy.soft_cap_ratio < policy.conservation_ratio <= policy.hard_cap_ratio):
            raise ValueError('예산 단계 비율은 soft_cap < conservation <= hard_cap 순서여야 합니다.')
        with self._db() as db:
            db.execute(
                'UPDATE hub_ai_budget_policy SET policy_json = ?, updated_at = ? WHERE id = 1',
                (json.dumps(policy.model_dump(), ensure_ascii=False), utcnow()),
            )
        return self.policy()

    def month_spent_microdollars(self, billing_month: str | None = None) -> int:
        month = billing_month or self.current_billing_month()
        with self._db() as db:
            row = db.execute(
                'SELECT COALESCE(SUM(cost_microdollars), 0) FROM hub_ai_usage_ledger WHERE billing_month = ?',
                (month,),
            ).fetchone()
        return int(row[0]) if row else 0

    def state_for_spend(self, spent_micros: int, policy: BudgetPolicy | None = None) -> BudgetState:
        p = policy or self.policy()
        cap_micros = int(round(p.monthly_cap_usd * MICRODOLLARS_PER_USD))
        hard_limit = int(round(cap_micros * p.hard_cap_ratio))
        conservation_limit = int(round(cap_micros * p.conservation_ratio))
        soft_limit = int(round(cap_micros * p.soft_cap_ratio))
        if spent_micros >= hard_limit:
            return 'HARD_CAP_REACHED'
        if spent_micros >= conservation_limit:
            return 'CONSERVATION'
        if spent_micros >= soft_limit:
            return 'SOFT_CAP'
        return 'NORMAL'

    def gate(
        self,
        category: UsageCategory,
        requested_model: str | None = None,
        *,
        estimated_input_tokens: int = 1200,
        max_output_tokens: int = 512,
        audio_seconds: float = 0.0,
        force_model: bool = False,
        at_iso: str | None = None,
    ) -> BudgetDecision:
        p = self.policy()
        month = self.current_billing_month(at_iso)
        spent = self.month_spent_microdollars(month)
        cap_micros = int(round(p.monthly_cap_usd * MICRODOLLARS_PER_USD))
        hard_limit = int(round(cap_micros * p.hard_cap_ratio))

        if category == 'stt':
            default_model = p.stt_model
        elif category in {'agent_deep', 'briefing'}:
            default_model = p.deep_model
        else:
            default_model = p.fast_model

        req_model = requested_model or default_model
        state = self.state_for_spend(spent, p)
        selected = req_model
        downgraded = False
        max_tokens = max_output_tokens
        reason = 'within_normal_budget'

        if state == 'SOFT_CAP' and p.auto_downgrade and not force_model:
            if category != 'stt' and selected in {'gpt-4o', 'o3-mini'}:
                selected = 'gpt-4o-mini'
                downgraded = True
                reason = 'soft_cap_auto_downgrade_to_gpt_4o_mini'
            elif category == 'stt' and selected in {'gpt-4o-transcribe', 'whisper-1'}:
                selected = 'gpt-4o-mini-transcribe'
                downgraded = True
                reason = 'soft_cap_auto_downgrade_to_mini_transcribe'
        elif state == 'CONSERVATION' and p.auto_downgrade:
            if category != 'stt' and selected != 'gpt-4o-mini':
                selected = 'gpt-4o-mini'
                downgraded = True
            if category == 'stt' and selected != 'gpt-4o-mini-transcribe':
                selected = 'gpt-4o-mini-transcribe'
                downgraded = True
            max_tokens = min(max_output_tokens, 384)
            reason = 'conservation_mode_compact_mini'

        estimated = calculate_cost_microdollars(
            selected,
            prompt_tokens=estimated_input_tokens,
            completion_tokens=max_tokens if category != 'stt' else 0,
            audio_seconds=audio_seconds if category == 'stt' else 0.0,
        )

        if state == 'HARD_CAP_REACHED' or (spent + estimated > hard_limit):
            remaining = max(0, hard_limit - spent)
            return BudgetDecision(
                allowed_cloud=False,
                state='HARD_CAP_REACHED',
                selected_model='local',
                requested_model=req_model,
                downgraded=True,
                fallback_local=p.fallback_to_local,
                reason='monthly_100_usd_hard_cap_reached',
                billing_month=month,
                spent_microdollars=spent,
                cap_microdollars=hard_limit,
                estimated_call_microdollars=estimated,
                remaining_microdollars=remaining,
                max_output_tokens=max_tokens,
            )

        remaining = max(0, hard_limit - spent)
        return BudgetDecision(
            allowed_cloud=True,
            state=state,
            selected_model=selected,
            requested_model=req_model,
            downgraded=downgraded,
            fallback_local=False,
            reason=reason,
            billing_month=month,
            spent_microdollars=spent,
            cap_microdollars=hard_limit,
            estimated_call_microdollars=estimated,
            remaining_microdollars=remaining,
            max_output_tokens=max_tokens,
        )

    def record_usage(
        self,
        request_id: str,
        category: UsageCategory,
        model: str,
        *,
        prompt_tokens: int = 0,
        cached_tokens: int = 0,
        completion_tokens: int = 0,
        audio_seconds: float = 0.0,
        note: str = '',
        at_iso: str | None = None,
    ) -> dict:
        stamp = at_iso or utcnow()
        month = self.current_billing_month(stamp)
        cost = calculate_cost_microdollars(
            model,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            audio_seconds=audio_seconds,
        )
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute(
                'SELECT * FROM hub_ai_usage_ledger WHERE request_id = ?',
                (request_id,),
            ).fetchone()
            if existing:
                row = dict(existing)
                row['cost_usd'] = round(row['cost_microdollars'] / MICRODOLLARS_PER_USD, 6)
                row['duplicate'] = True
                return row
            entry_id = uid()
            db.execute(
                '''INSERT INTO hub_ai_usage_ledger(
                    id, request_id, billing_month, category, model,
                    prompt_tokens, cached_tokens, completion_tokens,
                    audio_seconds, cost_microdollars, note, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    entry_id,
                    request_id,
                    month,
                    category,
                    model,
                    max(0, int(prompt_tokens)),
                    max(0, int(cached_tokens)),
                    max(0, int(completion_tokens)),
                    max(0.0, float(audio_seconds)),
                    cost,
                    note[:240],
                    stamp,
                ),
            )
            row = dict(
                db.execute('SELECT * FROM hub_ai_usage_ledger WHERE id = ?', (entry_id,)).fetchone()
            )
        row['cost_usd'] = round(row['cost_microdollars'] / MICRODOLLARS_PER_USD, 6)
        row['duplicate'] = False
        return row

    def summary(self, billing_month: str | None = None, *, at_iso: str | None = None) -> dict:
        p = self.policy()
        now_iso = at_iso or utcnow()
        settings = self.store.get('settings') or {'timezone': 'Asia/Seoul'}
        tz_name = settings.get('timezone', 'Asia/Seoul')
        local_now = datetime.fromisoformat(now_iso).astimezone(ZoneInfo(tz_name))
        month = billing_month or f'{local_now.year:04d}-{local_now.month:02d}'

        with self._db() as db:
            total_row = db.execute(
                '''SELECT
                    COALESCE(SUM(cost_microdollars), 0) AS spent_micros,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(audio_seconds), 0.0) AS audio_seconds,
                    COUNT(*) AS call_count
                   FROM hub_ai_usage_ledger
                   WHERE billing_month = ?''',
                (month,),
            ).fetchone()
            by_cat = [
                {
                    'category': r['category'],
                    'calls': r['calls'],
                    'cost_microdollars': r['cost_micros'],
                    'cost_usd': round(r['cost_micros'] / MICRODOLLARS_PER_USD, 6),
                    'prompt_tokens': r['prompt_tokens'],
                    'completion_tokens': r['completion_tokens'],
                    'audio_seconds': round(r['audio_seconds'], 2),
                }
                for r in db.execute(
                    '''SELECT category, COUNT(*) AS calls,
                              COALESCE(SUM(cost_microdollars), 0) AS cost_micros,
                              COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                              COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                              COALESCE(SUM(audio_seconds), 0.0) AS audio_seconds
                       FROM hub_ai_usage_ledger
                       WHERE billing_month = ?
                       GROUP BY category
                       ORDER BY cost_micros DESC, category''',
                    (month,),
                ).fetchall()
            ]
            by_model = [
                {
                    'model': r['model'],
                    'calls': r['calls'],
                    'cost_microdollars': r['cost_micros'],
                    'cost_usd': round(r['cost_micros'] / MICRODOLLARS_PER_USD, 6),
                }
                for r in db.execute(
                    '''SELECT model, COUNT(*) AS calls,
                              COALESCE(SUM(cost_microdollars), 0) AS cost_micros
                       FROM hub_ai_usage_ledger
                       WHERE billing_month = ?
                       GROUP BY model
                       ORDER BY cost_micros DESC, model''',
                    (month,),
                ).fetchall()
            ]
            recent = [
                dict(r) | {'cost_usd': round(r['cost_microdollars'] / MICRODOLLARS_PER_USD, 6)}
                for r in db.execute(
                    '''SELECT * FROM hub_ai_usage_ledger
                       WHERE billing_month = ?
                       ORDER BY created_at DESC, id DESC LIMIT 25''',
                    (month,),
                ).fetchall()
            ]

        spent_micros = int(total_row['spent_micros'])
        cap_micros = int(round(p.monthly_cap_usd * MICRODOLLARS_PER_USD))
        hard_limit = int(round(cap_micros * p.hard_cap_ratio))
        remaining_micros = max(0, hard_limit - spent_micros)
        state = self.state_for_spend(spent_micros, p)

        year, mon = (int(x) for x in month.split('-'))
        days_in_month = calendar.monthrange(year, mon)[1]
        elapsed_days = max(1.0, local_now.day + (local_now.hour / 24.0)) if (year == local_now.year and mon == local_now.month) else float(days_in_month)
        daily_burn_usd = (spent_micros / MICRODOLLARS_PER_USD) / elapsed_days
        projected_month_usd = daily_burn_usd * days_in_month
        daily_target_usd = p.monthly_cap_usd / days_in_month

        return {
            'billing_month': month,
            'timezone': tz_name,
            'state': state,
            'policy': p.model_dump(),
            'spent_microdollars': spent_micros,
            'spent_usd': round(spent_micros / MICRODOLLARS_PER_USD, 4),
            'monthly_cap_usd': round(p.monthly_cap_usd, 2),
            'hard_cap_usd': round(hard_limit / MICRODOLLARS_PER_USD, 2),
            'soft_cap_usd': round(p.monthly_cap_usd * p.soft_cap_ratio, 2),
            'conservation_cap_usd': round(p.monthly_cap_usd * p.conservation_ratio, 2),
            'remaining_microdollars': remaining_micros,
            'remaining_usd': round(remaining_micros / MICRODOLLARS_PER_USD, 4),
            'utilization_percent': round((spent_micros / max(1, hard_limit)) * 100.0, 2),
            'call_count': int(total_row['call_count']),
            'totals': {
                'prompt_tokens': int(total_row['prompt_tokens']),
                'cached_tokens': int(total_row['cached_tokens']),
                'completion_tokens': int(total_row['completion_tokens']),
                'audio_seconds': round(float(total_row['audio_seconds']), 2),
                'audio_minutes': round(float(total_row['audio_seconds']) / 60.0, 2),
            },
            'pacing': {
                'days_in_month': days_in_month,
                'elapsed_days': round(elapsed_days, 2),
                'daily_target_usd': round(daily_target_usd, 2),
                'daily_burn_usd': round(daily_burn_usd, 4),
                'projected_month_usd': round(projected_month_usd, 2),
                'on_track': projected_month_usd <= p.monthly_cap_usd,
            },
            'by_category': by_cat,
            'by_model': by_model,
            'recent': recent,
            'pricing_catalog': PRICING_CATALOG,
        }
