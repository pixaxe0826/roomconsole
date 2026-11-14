"""Process-level AI topology; independent of stored cloud consent and API keys.

The Odyssey profile never enables a paid request by itself. It removes local/PC
model dispatch from the serving process, including fallback and diagnostic paths.
Changing this boundary requires an explicit service configuration and restart.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Literal


@dataclass(frozen=True)
class AIProfile:
    name: Literal['legacy', 'odyssey_openai'] = 'legacy'

    def __post_init__(self):
        if self.name not in {'legacy', 'odyssey_openai'}:
            raise ValueError('HUB_AI_PROFILE must be legacy or odyssey_openai.')

    @classmethod
    def from_env(cls) -> 'AIProfile':
        return cls(os.getenv('HUB_AI_PROFILE', 'legacy').strip().lower() or 'legacy')

    @property
    def pc_free(self) -> bool:
        return self.name == 'odyssey_openai'

    def public(self) -> dict:
        return {
            'runtime_profile': self.name,
            'orchestrator': 'odyssey' if self.pc_free else 'room_hub',
            'pc_required': False if self.pc_free else None,
            'ssh_tunnel_required': False if self.pc_free else None,
            'legacy_inference_allowed': not self.pc_free,
            'local_rules_available': True,
        }
