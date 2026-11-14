"""Monthly $100 OpenAI API budget governor, reservation ledger, and microdollar usage ledger.

All costs are tracked in integer microdollars (1 USD = 1,000,000 microdollars)
in SQLite so floating-point rounding never drifts across thousands of requests.
Concurrent requests reserve their maximum estimated cost with `BEGIN IMMEDIATE`
before any network call, and active + unknown reservations count toward the cap.
"""
from __future__ import annotations

import calendar
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import math
import os
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from .store import uid, utcnow

MICRODOLLARS_PER_USD = 1_000_000

# Official OpenAI API pricing in microdollars:
# - Token rates are microdollars per 1,000,000 tokens.
# - Audio rates are microdollars per minute.
PRICING_CATALOG: dict[str, dict[str, int]] = {
    'gpt-6.1-sol': {
        'input_per_1m_micros': 2_500_000,     # $2.50 / 1M input tokens
        'cached_per_1m_micros': 1_250_000,    # $1.25 / 1M cached input tokens
        'output_per_1m_micros': 10_000_000,   # $10.00 / 1M output (incl. reasoning) tokens
        'audio_per_min_micros': 0,
    },
    'gpt-6.1-terra': {
        'input_per_1m_micros': 5_000_000,     # $5.00 / 1M input tokens
        'cached_per_1m_micros': 2_500_000,    # $2.50 / 1M cached input tokens
        'output_per_1m_micros': 15_000_000,   # $15.00 / 1M output tokens
        'audio_per_min_micros': 0,
    },
    'gpt-6-sol': {
        'input_per_1m_micros': 2_500_000,
        'cached_per_1m_micros': 1_250_000,
        'output_per_1m_micros': 10_000_000,
        'audio_per_min_micros': 0,
    },
    'gpt-5.6-sol': {
        'input_per_1m_micros': 2_000_000,
        'cached_per_1m_micros': 1_000_000,
        'output_per_1m_micros': 8_000_000,
        'audio_per_min_micros': 0,
    },
    'o3': {
        'input_per_1m_micros': 2_000_000,
        'cached_per_1m_micros': 500_000,
        'output_per_1m_micros': 8_000_000,
        'audio_per_min_micros': 0,
    },
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
        'input_per_1m_micros': 2_500_000,
        'cached_per_1m_micros': 1_250_000,
        'output_per_1m_micros': 10_000_000,
        'audio_per_min_micros': 3_000,        # $0.003 / minute
    },
    'gpt-4o-transcribe': {
        'input_per_1m_micros': 2_500_000,
        'cached_per_1m_micros': 1_250_000,
        'output_per_1m_micros': 10_000_000,
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

ALLOWED_CHAT_MODELS = (
    'gpt-6.1-sol',
    'gpt-6.1-terra',
    'gpt-6-sol',
    'gpt-5.6-sol',
    'o3',
    'gpt-4o-mini',
    'gpt-4o',
    'o3-mini',
)
ALLOWED_STT_MODELS = ('gpt-4o-mini-transcribe', 'gpt-4o-transcribe', 'whisper-1')
BudgetState = Literal['NORMAL', 'SOFT_CAP', 'CONSERVATION', 'HARD_CAP_REACHED']
UsageCategory = Literal['stt', 'agent_fast', 'agent_deep', 'briefing', 'chat', 'local_fallback']


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_float(name: str, default: float, *, low: float, high: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw.strip())
    except ValueError:
        return default
    if math.isnan(val) or math.isinf(val) or not (low <= val <= high):
        return default
    return val


class BudgetPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    agent_enabled: bool = False
    cloud_enabled: bool = False
    audio_cloud_enabled: bool = False
    allow_shared_notes_in_cloud: bool = False
    monthly_cap_usd: float = Field(default=100.0, ge=1.0, le=10000.0)
    soft_cap_ratio: float = Field(default=0.70, ge=0.10, le=0.95)
    conservation_ratio: float = Field(default=0.90, ge=0.20, le=0.99)
    hard_cap_ratio: float = Field(default=1.00, ge=0.50, le=1.00)
    fast_model: Literal['gpt-6.1-sol', 'gpt-4o-mini', 'gpt-4o', 'o3-mini'] = 'gpt-6.1-sol'
    deep_model: Literal['gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-4o', 'o3', 'o3-mini', 'gpt-4o-mini'] = 'gpt-6.1-sol'
    reasoning_effort: Literal['low', 'medium', 'high'] = 'high'
    stt_model: Literal['gpt-4o-mini-transcribe', 'gpt-4o-transcribe', 'whisper-1'] = 'gpt-4o-mini-transcribe'
    auto_downgrade: bool = True
    fallback_to_local: bool = True
    prefer_tier0_rules: bool = True


def policy_from_env() -> BudgetPolicy:
    """Build initial BudgetPolicy from environment variables (used only when no policy row is stored yet)."""
    fast_env = os.environ.get('HUB_OPENAI_FAST_MODEL', '').strip()
    deep_env = os.environ.get('HUB_OPENAI_DEEP_MODEL', '').strip()
    effort_env = os.environ.get('HUB_OPENAI_REASONING_EFFORT', '').strip().lower()
    stt_env = os.environ.get('HUB_OPENAI_STT_MODEL', '').strip()

    fast_valid = {'gpt-6.1-sol', 'gpt-4o-mini', 'gpt-4o', 'o3-mini'}
    deep_valid = {'gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-4o', 'o3', 'o3-mini', 'gpt-4o-mini'}
    effort_valid = {'low', 'medium', 'high'}
    stt_valid = {'gpt-4o-mini-transcribe', 'gpt-4o-transcribe', 'whisper-1'}

    soft = _env_float('HUB_OPENAI_SOFT_CAP_RATIO', 0.70, low=0.10, high=0.95)
    cons = _env_float('HUB_OPENAI_CONSERVATION_RATIO', 0.90, low=0.20, high=0.99)
    hard = _env_float('HUB_OPENAI_HARD_CAP_RATIO', 1.00, low=0.50, high=1.00)
    if not (soft < cons <= hard):
        soft, cons, hard = 0.70, 0.90, 1.00

    return BudgetPolicy(
        agent_enabled=_env_bool('HUB_PERSONAL_AGENT_ENABLED', False),
        cloud_enabled=_env_bool('HUB_OPENAI_ENABLED', False),
        audio_cloud_enabled=_env_bool('HUB_OPENAI_AUDIO_ENABLED', False),
        allow_shared_notes_in_cloud=_env_bool('HUB_OPENAI_ALLOW_SHARED_NOTES', False),
        monthly_cap_usd=_env_float('HUB_OPENAI_MONTHLY_CAP_USD', 100.0, low=1.0, high=10000.0),
        soft_cap_ratio=soft,
        conservation_ratio=cons,
        hard_cap_ratio=hard,
        fast_model=fast_env if fast_env in fast_valid else 'gpt-6.1-sol',  # type: ignore[arg-type]
        deep_model=deep_env if deep_env in deep_valid else 'gpt-6.1-sol',  # type: ignore[arg-type]
        reasoning_effort=effort_env if effort_env in effort_valid else 'high',  # type: ignore[arg-type]
        stt_model=stt_env if stt_env in stt_valid else 'gpt-4o-mini-transcribe',  # type: ignore[arg-type]
    )


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
    reasoning_effort: str = 'high'
    reserved_microdollars: int = 0
    unknown_microdollars: int = 0

    def data(self) -> dict[str, Any]:
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
    reasoning_tokens: int = 0,
    audio_seconds: float = 0.0,
) -> int:
    """Compute exact integer microdollar cost (ceil to nearest microdollar).

    Note: In the OpenAI Chat Completions / Responses API usage object,
    `completion_tokens_details.reasoning_tokens` is a subset of `completion_tokens`
    and is already included in `completion_tokens`. We never add `reasoning_tokens`
    on top of `completion_tokens` (no double-billing).
    Unknown models are rejected with ValueError rather than silently substituted.
    """
    if model.startswith('local') or model.endswith('.gguf'):
        return 0
    rates = PRICING_CATALOG.get(model)
    if rates is None:
        raise ValueError(f'가격 카탈로그에 등록되지 않은 모델입니다: {model}')
    if isinstance(audio_seconds, (int, float)) and (
        math.isnan(float(audio_seconds)) or math.isinf(float(audio_seconds)) or float(audio_seconds) < 0.0
    ):
        raise ValueError('유효하지 않은 오디오 길이(audio_seconds)입니다.')
    _ = reasoning_tokens  # Subset of completion_tokens; never double-billed.
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
    """SQLite-backed $100/month budget tracker, concurrent reservation manager, and tiered model selector."""

    def __init__(self, store):
        self.store = store
        self._schema_ready = False
        self._initial_policy = policy_from_env()

    def _ensure_schema(self, db) -> None:
        if self._schema_ready:
            return
        for stmt in (
            '''CREATE TABLE IF NOT EXISTS hub_ai_budget_policy (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                policy_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )''',
            '''CREATE TABLE IF NOT EXISTS hub_ai_usage_ledger (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                billing_month TEXT NOT NULL,
                category TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                audio_seconds REAL NOT NULL DEFAULT 0.0,
                cost_microdollars INTEGER NOT NULL DEFAULT 0,
                cost_source TEXT NOT NULL DEFAULT 'provider_usage',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )''',
            '''CREATE INDEX IF NOT EXISTS idx_ai_usage_month
                ON hub_ai_usage_ledger(billing_month, created_at DESC)''',
            '''CREATE TABLE IF NOT EXISTS hub_ai_budget_reservations (
                request_id TEXT PRIMARY KEY,
                billing_month TEXT NOT NULL,
                category TEXT NOT NULL,
                model TEXT NOT NULL,
                reserved_microdollars INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'settled', 'released', 'unknown')),
                actual_microdollars INTEGER,
                cost_source TEXT NOT NULL DEFAULT 'reservation',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )''',
            '''CREATE INDEX IF NOT EXISTS idx_ai_reservations_month
                ON hub_ai_budget_reservations(billing_month, status)''',
        ):
            db.execute(stmt)
        cols = {r['name'] for r in db.execute('PRAGMA table_info(hub_ai_usage_ledger)').fetchall()}
        if 'reasoning_tokens' not in cols:
            db.execute('ALTER TABLE hub_ai_usage_ledger ADD COLUMN reasoning_tokens INTEGER NOT NULL DEFAULT 0')
        if 'cost_source' not in cols:
            db.execute(
                "ALTER TABLE hub_ai_usage_ledger ADD COLUMN cost_source TEXT NOT NULL DEFAULT 'provider_usage'"
            )
        row = db.execute('SELECT 1 FROM hub_ai_budget_policy WHERE id = 1').fetchone()
        if not row:
            initial_policy = self._initial_policy.model_dump()
            db.execute(
                'INSERT INTO hub_ai_budget_policy(id, policy_json, updated_at) VALUES(1, ?, ?)',
                (json.dumps(initial_policy, ensure_ascii=False), utcnow()),
            )
        if not self.store.in_unit_of_work():
            db.commit()
        self._schema_ready = True

    @contextmanager
    def _db(self):
        with closing(self.store.connect()) as db:
            with db:
                if not self._schema_ready:
                    self._ensure_schema(db)
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
            return policy_from_env()
        raw = json.loads(row['policy_json'])
        merged = policy_from_env().model_dump() | raw
        return BudgetPolicy.model_validate(merged)

    def get_policy(self) -> BudgetPolicy:
        return self.policy()

    def set_policy(self, policy: BudgetPolicy | dict[str, Any]) -> BudgetPolicy:
        if isinstance(policy, dict):
            current = self.policy().model_dump()
            current.update(policy)
            policy_obj = BudgetPolicy.model_validate(current)
        else:
            policy_obj = policy
        if not (policy_obj.soft_cap_ratio < policy_obj.conservation_ratio <= policy_obj.hard_cap_ratio):
            raise ValueError('예산 단계 비율은 soft_cap < conservation <= hard_cap 순서여야 합니다.')
        with self._db() as db:
            db.execute(
                'UPDATE hub_ai_budget_policy SET policy_json = ?, updated_at = ? WHERE id = 1',
                (json.dumps(policy_obj.model_dump(), ensure_ascii=False), utcnow()),
            )
        return self.policy()

    def update_policy(self, patch: BudgetPolicy | dict[str, Any]) -> BudgetPolicy:
        return self.set_policy(patch)

    def _month_totals_in_tx(self, db, billing_month: str) -> tuple[int, int, int]:
        spent_row = db.execute(
            'SELECT COALESCE(SUM(cost_microdollars), 0) FROM hub_ai_usage_ledger WHERE billing_month = ?',
            (billing_month,),
        ).fetchone()
        active_row = db.execute(
            "SELECT COALESCE(SUM(reserved_microdollars), 0) FROM hub_ai_budget_reservations WHERE billing_month = ? AND status = 'active'",
            (billing_month,),
        ).fetchone()
        unknown_row = db.execute(
            "SELECT COALESCE(SUM(reserved_microdollars), 0) FROM hub_ai_budget_reservations WHERE billing_month = ? AND status = 'unknown'",
            (billing_month,),
        ).fetchone()
        spent = int(spent_row[0]) if spent_row else 0
        active_res = int(active_row[0]) if active_row else 0
        unknown_res = int(unknown_row[0]) if unknown_row else 0
        return spent, active_res, unknown_res

    def month_spent_microdollars(self, billing_month: str | None = None) -> int:
        month = billing_month or self.current_billing_month()
        with self._db() as db:
            spent, _, _ = self._month_totals_in_tx(db, month)
        return spent

    def spent_microdollars(self, billing_month: str | None = None, *, at_iso: str | None = None) -> int:
        month = billing_month or self.current_billing_month(at_iso)
        return self.month_spent_microdollars(month)

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

    def _compute_decision(
        self,
        *,
        p: BudgetPolicy,
        month: str,
        spent: int,
        active_res: int,
        unknown_res: int,
        category: UsageCategory,
        requested_model: str | None,
        estimated_input_tokens: int,
        max_output_tokens: int,
        audio_seconds: float,
        force_model: bool,
    ) -> BudgetDecision:
        effective_spent = spent + active_res + unknown_res
        cap_micros = int(round(p.monthly_cap_usd * MICRODOLLARS_PER_USD))
        hard_limit = int(round(cap_micros * p.hard_cap_ratio))

        if category == 'stt':
            default_model = p.stt_model
        elif category in {'agent_deep', 'briefing'}:
            default_model = p.deep_model
        else:
            default_model = p.fast_model

        req_model = requested_model or default_model
        if req_model not in PRICING_CATALOG:
            raise ValueError(f'가격 카탈로그에 등록되지 않은 모델입니다: {req_model}')

        state = self.state_for_spend(effective_spent, p)
        selected = req_model
        downgraded = False
        max_tokens = max_output_tokens
        reasoning = p.reasoning_effort
        reason = 'within_normal_budget'

        if state == 'SOFT_CAP' and p.auto_downgrade and not force_model:
            if category != 'stt' and selected in {'gpt-4o', 'o3-mini', 'gpt-6.1-terra', 'o3'}:
                selected = 'gpt-4o-mini'
                downgraded = True
                reason = 'soft_cap_auto_downgrade_to_gpt_4o_mini'
            elif category != 'stt' and selected == 'gpt-6.1-sol' and p.fast_model == 'gpt-4o-mini':
                selected = 'gpt-4o-mini'
                downgraded = True
                reason = 'soft_cap_auto_downgrade_to_gpt_4o_mini'
            elif category == 'stt' and selected in {'gpt-4o-transcribe', 'whisper-1'}:
                selected = 'gpt-4o-mini-transcribe'
                downgraded = True
                reason = 'soft_cap_auto_downgrade_to_mini_transcribe'
        elif state == 'CONSERVATION' and p.auto_downgrade:
            if category != 'stt' and selected in {'gpt-4o', 'o3-mini', 'o3', 'gpt-6.1-terra'}:
                selected = 'gpt-4o-mini'
                downgraded = True
            elif category != 'stt' and selected == 'gpt-6.1-sol' and not force_model and p.fast_model == 'gpt-4o-mini':
                selected = 'gpt-4o-mini'
                downgraded = True
            if category == 'stt' and selected != 'gpt-4o-mini-transcribe':
                selected = 'gpt-4o-mini-transcribe'
                downgraded = True
            max_tokens = (
                min(max_output_tokens, 384)
                if not selected.startswith(('gpt-6', 'gpt-5', 'o'))
                else max(max_output_tokens, 2048)
            )
            reason = 'conservation_mode_compact_mini'

        if category != 'stt' and selected.startswith(('gpt-6', 'gpt-5', 'o')) and state in {'NORMAL', 'SOFT_CAP'}:
            max_tokens = max(max_tokens, 4096)

        estimated = calculate_cost_microdollars(
            selected,
            prompt_tokens=estimated_input_tokens,
            completion_tokens=max_tokens if category != 'stt' else 0,
            audio_seconds=audio_seconds if category == 'stt' else 0.0,
        )

        if state == 'HARD_CAP_REACHED' or (effective_spent + estimated > hard_limit):
            remaining = max(0, hard_limit - effective_spent)
            return BudgetDecision(
                allowed_cloud=False,
                state='HARD_CAP_REACHED',
                selected_model='local',
                requested_model=req_model,
                downgraded=True,
                fallback_local=p.fallback_to_local,
                reason='monthly_100_usd_hard_cap_reached',
                billing_month=month,
                spent_microdollars=effective_spent,
                cap_microdollars=hard_limit,
                estimated_call_microdollars=estimated,
                remaining_microdollars=remaining,
                max_output_tokens=max_tokens,
                reasoning_effort=reasoning,
                reserved_microdollars=active_res,
                unknown_microdollars=unknown_res,
            )

        remaining = max(0, hard_limit - effective_spent)
        return BudgetDecision(
            allowed_cloud=True,
            state=state,
            selected_model=selected,
            requested_model=req_model,
            downgraded=downgraded,
            fallback_local=False,
            reason=reason,
            billing_month=month,
            spent_microdollars=effective_spent,
            cap_microdollars=hard_limit,
            estimated_call_microdollars=estimated,
            remaining_microdollars=remaining,
            max_output_tokens=max_tokens,
            reasoning_effort=reasoning,
            reserved_microdollars=active_res,
            unknown_microdollars=unknown_res,
        )

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
        with self._db() as db:
            spent, active_res, unknown_res = self._month_totals_in_tx(db, month)
        return self._compute_decision(
            p=p,
            month=month,
            spent=spent,
            active_res=active_res,
            unknown_res=unknown_res,
            category=category,
            requested_model=requested_model,
            estimated_input_tokens=estimated_input_tokens,
            max_output_tokens=max_output_tokens,
            audio_seconds=audio_seconds,
            force_model=force_model,
        )

    def reserve_budget(
        self,
        request_id: str,
        category: UsageCategory,
        requested_model: str | None = None,
        *,
        estimated_input_tokens: int = 1200,
        max_output_tokens: int = 512,
        audio_seconds: float = 0.0,
        force_model: bool = False,
        note: str = '',
        at_iso: str | None = None,
    ) -> BudgetDecision:
        """Atomically evaluate budget with `BEGIN IMMEDIATE` and create an active reservation if allowed."""
        p = self.policy()
        stamp = at_iso or utcnow()
        month = self.current_billing_month(stamp)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            spent, active_res, unknown_res = self._month_totals_in_tx(db, month)
            decision = self._compute_decision(
                p=p,
                month=month,
                spent=spent,
                active_res=active_res,
                unknown_res=unknown_res,
                category=category,
                requested_model=requested_model,
                estimated_input_tokens=estimated_input_tokens,
                max_output_tokens=max_output_tokens,
                audio_seconds=audio_seconds,
                force_model=force_model,
            )
            if not decision.allowed_cloud:
                return decision

            existing = db.execute(
                'SELECT status, reserved_microdollars FROM hub_ai_budget_reservations WHERE request_id = ?',
                (request_id,),
            ).fetchone()
            if existing:
                if existing['status'] in {'active', 'settled'}:
                    return decision
            db.execute(
                '''INSERT OR REPLACE INTO hub_ai_budget_reservations(
                    request_id, billing_month, category, model,
                    reserved_microdollars, status, actual_microdollars,
                    cost_source, note, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'active', NULL, 'reservation', ?, ?, ?)''',
                (
                    request_id,
                    month,
                    category,
                    decision.selected_model,
                    max(1, decision.estimated_call_microdollars),
                    note[:240],
                    stamp,
                    stamp,
                ),
            )
        return decision

    def release_reservation(self, request_id: str, note: str = '') -> None:
        """Release a pre-call reservation when the call failed before reaching the network."""
        stamp = utcnow()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute(
                '''UPDATE hub_ai_budget_reservations
                   SET status = 'released', actual_microdollars = 0, note = ?, updated_at = ?
                   WHERE request_id = ? AND status = 'active' ''',
                (note[:240], stamp, request_id),
            )

    def mark_reservation_unknown(self, request_id: str, note: str = '') -> None:
        """Keep reservation counted conservatively when a call timed out or dropped after dispatch."""
        stamp = utcnow()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute(
                '''UPDATE hub_ai_budget_reservations
                   SET status = 'unknown', cost_source = 'unknown_unsettled', note = ?, updated_at = ?
                   WHERE request_id = ? AND status = 'active' ''',
                (note[:240], stamp, request_id),
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
        reasoning_tokens: int = 0,
        audio_seconds: float = 0.0,
        cost_source: str = 'provider_usage',
        note: str = '',
        at_iso: str | None = None,
    ) -> dict[str, Any]:
        stamp = at_iso or utcnow()
        month = self.current_billing_month(stamp)
        cost = calculate_cost_microdollars(
            model,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            audio_seconds=audio_seconds,
        )
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute(
                'SELECT * FROM hub_ai_usage_ledger WHERE request_id = ?',
                (request_id,),
            ).fetchone()
            if existing:
                db.execute(
                    '''UPDATE hub_ai_budget_reservations
                       SET status = 'settled', actual_microdollars = ?, cost_source = ?, updated_at = ?
                       WHERE request_id = ?''',
                    (existing['cost_microdollars'], existing['cost_source'], stamp, request_id),
                )
                row = dict(existing)
                row['cost_usd'] = round(row['cost_microdollars'] / MICRODOLLARS_PER_USD, 6)
                row['duplicate'] = True
                return row
            entry_id = uid()
            db.execute(
                '''INSERT INTO hub_ai_usage_ledger(
                    id, request_id, billing_month, category, model,
                    prompt_tokens, cached_tokens, completion_tokens, reasoning_tokens,
                    audio_seconds, cost_microdollars, cost_source, note, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    entry_id,
                    request_id,
                    month,
                    category,
                    model,
                    max(0, int(prompt_tokens)),
                    max(0, int(cached_tokens)),
                    max(0, int(completion_tokens)),
                    max(0, int(reasoning_tokens)),
                    max(0.0, float(audio_seconds)),
                    cost,
                    cost_source[:64],
                    note[:240],
                    stamp,
                ),
            )
            db.execute(
                '''UPDATE hub_ai_budget_reservations
                   SET status = 'settled', actual_microdollars = ?, cost_source = ?, note = ?, updated_at = ?
                   WHERE request_id = ?''',
                (cost, cost_source[:64], note[:240], stamp, request_id),
            )
            row = dict(
                db.execute('SELECT * FROM hub_ai_usage_ledger WHERE id = ?', (entry_id,)).fetchone()
            )
        row['cost_usd'] = round(row['cost_microdollars'] / MICRODOLLARS_PER_USD, 6)
        row['duplicate'] = False
        return row

    def list_reservations(self, billing_month: str | None = None) -> list[dict[str, Any]]:
        month = billing_month or self.current_billing_month()
        with self._db() as db:
            rows = db.execute(
                'SELECT * FROM hub_ai_budget_reservations WHERE billing_month = ? ORDER BY created_at DESC',
                (month,),
            ).fetchall()
        return [dict(r) for r in rows]

    def summary(self, billing_month: str | None = None, *, at_iso: str | None = None) -> dict[str, Any]:
        p = self.policy()
        now_iso = at_iso or utcnow()
        settings = self.store.get('settings') or {'timezone': 'Asia/Seoul'}
        tz_name = settings.get('timezone', 'Asia/Seoul')
        local_now = datetime.fromisoformat(now_iso).astimezone(ZoneInfo(tz_name))
        month = billing_month or f'{local_now.year:04d}-{local_now.month:02d}'

        with self._db() as db:
            spent_micros, active_res, unknown_res = self._month_totals_in_tx(db, month)
            total_row = db.execute(
                '''SELECT
                    COALESCE(SUM(cost_microdollars), 0) AS spent_micros,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
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

        committed_micros = spent_micros + active_res + unknown_res
        cap_micros = int(round(p.monthly_cap_usd * MICRODOLLARS_PER_USD))
        hard_limit = int(round(cap_micros * p.hard_cap_ratio))
        remaining_micros = max(0, hard_limit - committed_micros)
        state = self.state_for_spend(committed_micros, p)

        year, mon = (int(x) for x in month.split('-'))
        days_in_month = calendar.monthrange(year, mon)[1]
        elapsed_days = (
            max(1.0, local_now.day + (local_now.hour / 24.0))
            if (year == local_now.year and mon == local_now.month)
            else float(days_in_month)
        )
        daily_burn_usd = (committed_micros / MICRODOLLARS_PER_USD) / elapsed_days
        projected_month_usd = daily_burn_usd * days_in_month
        daily_target_usd = p.monthly_cap_usd / days_in_month

        return {
            'billing_month': month,
            'timezone': tz_name,
            'state': state,
            'policy': p.model_dump(),
            'spent_microdollars': spent_micros,
            'spent_usd': round(spent_micros / MICRODOLLARS_PER_USD, 4),
            'reserved_microdollars': active_res,
            'reserved_usd': round(active_res / MICRODOLLARS_PER_USD, 4),
            'unknown_microdollars': unknown_res,
            'unknown_usd': round(unknown_res / MICRODOLLARS_PER_USD, 4),
            'committed_microdollars': committed_micros,
            'committed_usd': round(committed_micros / MICRODOLLARS_PER_USD, 4),
            'monthly_cap_usd': round(p.monthly_cap_usd, 2),
            'hard_cap_usd': round(hard_limit / MICRODOLLARS_PER_USD, 2),
            'soft_cap_usd': round(p.monthly_cap_usd * p.soft_cap_ratio, 2),
            'conservation_cap_usd': round(p.monthly_cap_usd * p.conservation_ratio, 2),
            'remaining_microdollars': remaining_micros,
            'remaining_usd': round(remaining_micros / MICRODOLLARS_PER_USD, 4),
            'utilization_percent': round((committed_micros / max(1, hard_limit)) * 100.0, 2),
            'call_count': int(total_row['call_count']),
            'totals': {
                'prompt_tokens': int(total_row['prompt_tokens']),
                'cached_tokens': int(total_row['cached_tokens']),
                'completion_tokens': int(total_row['completion_tokens']),
                'reasoning_tokens': int(total_row['reasoning_tokens']),
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

    def monthly_summary(self, billing_month: str | None = None, *, at_iso: str | None = None) -> dict[str, Any]:
        return self.summary(billing_month, at_iso=at_iso)
