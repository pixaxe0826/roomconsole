"""Side-effect-free configuration for the additive Personal Agent benchmark."""
from __future__ import annotations

from .agent_gateway import cost_limit_microdollars

ENGINE = 'personal-agent-v1'
FORMAT_VERSION = 1
FAST_MODELS = ('gpt-6.1-sol', 'gpt-4o-mini', 'gpt-4o')
DEEP_MODELS = FAST_MODELS + ('gpt-6.1-terra', 'o3')
KEYS = {'live', 'timeout', 'agent_mode', 'fast_model', 'deep_model',
        'reasoning_effort', 'allow_shared_notes', 'max_cost_usd', 'max_calls'}


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict) or set(config) != KEYS:
        raise ValueError('Invalid benchmark configuration keys')
    for name in ('live', 'allow_shared_notes'):
        if type(config[name]) is not bool:
            raise ValueError('Invalid boolean benchmark configuration')
    if type(config['timeout']) is not int or not 10 <= config['timeout'] <= 600:
        raise ValueError('timeout must be 10..600 seconds')
    if config['agent_mode'] not in {'auto', 'fast', 'deep', 'local_only'}:
        raise ValueError('Unknown Personal Agent mode')
    if config['fast_model'] not in FAST_MODELS or config['deep_model'] not in DEEP_MODELS:
        raise ValueError('Model is not supported by the current production policy')
    if config['reasoning_effort'] not in {'low', 'medium', 'high'}:
        raise ValueError('Invalid reasoning effort')
    if type(config['max_calls']) is not int or not 1 <= config['max_calls'] <= 10000:
        raise ValueError('max-calls must be 1..10000')
    cost_limit_microdollars(config['max_cost_usd'])
    return dict(config)
