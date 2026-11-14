"""Offline regression tests for one-dispatch reservations and truthful billing.

Every provider transport is a local callable fixture. Real asyncio scheduling,
timeouts, cancellation and thread contention are used, never live API credentials.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.openai_budget import BudgetGovernor, PRICING_CATALOG_METADATA, calculate_cost_microdollars, policy_from_env
from app.openai_gateway import OpenAIGateway, OpenAIGatewayError, extract_usage, verify_audio_payload
from app.store import Store


def wav(seconds=1):
    pcm = b'\0\0' * (16000 * seconds)
    return (b'RIFF' + (36 + len(pcm)).to_bytes(4, 'little') + b'WAVEfmt '
            + (16).to_bytes(4, 'little') + (1).to_bytes(2, 'little') * 2
            + (16000).to_bytes(4, 'little') + (32000).to_bytes(4, 'little')
            + (2).to_bytes(2, 'little') + (16).to_bytes(2, 'little')
            + b'data' + len(pcm).to_bytes(4, 'little') + pcm)


def answer(usage=None, **extra):
    result = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
    if usage is not None:
        result['usage'] = usage
    return result | extra


class TestAgentBudgetSafety(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'synthetic.sqlite3')
        self.gov = BudgetGovernor(self.store)
        self.gov.set_policy({'agent_enabled': True, 'cloud_enabled': True, 'audio_cloud_enabled': True})

    def gateway(self, transport, gov=None):
        return OpenAIGateway(gov or self.gov, api_key='synthetic-offline-key', transport=transport)

    async def generate(self, gw, request_id='budget-request-1', **kwargs):
        return await gw.generate_structured(
            request_id=request_id, category='agent_fast', system_prompt='Return JSON.',
            user_text=kwargs.pop('user_text', '오늘 할 일을 확인해 줘'), schema_name='offline_test',
            schema={'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                    'required': ['ok'], 'additionalProperties': False}, **kwargs)

    def reservation(self, request_id):
        return next(r for r in self.gov.list_reservations() if r['request_id'] == request_id)

    def test_concurrent_requests_acquire_exactly_one_dispatch(self):
        async def check():
            entered, release = asyncio.Event(), asyncio.Event()
            calls = []

            async def provider(*args):
                calls.append(args)
                entered.set()
                await release.wait()
                return answer({'prompt_tokens': 100, 'completion_tokens': 10})

            gw = self.gateway(provider)
            first = asyncio.create_task(self.generate(gw))
            await asyncio.wait_for(entered.wait(), 1)
            try:
                with self.assertRaises(OpenAIGatewayError) as caught:
                    await self.generate(gw)
                self.assertEqual(caught.exception.code, 'request_already_dispatched')
                self.assertEqual(caught.exception.http_status, 409)
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
            result = await first
            self.assertTrue(result['cost_known'])
            self.assertEqual(self.reservation('budget-request-1')['status'], 'settled')
            restarted_gov = BudgetGovernor(self.store)
            restarted = self.gateway(provider, restarted_gov)
            with self.assertRaises(OpenAIGatewayError):
                await self.generate(restarted)
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.gov.summary()['call_count'], 1)

        asyncio.run(check())

    def test_same_key_changed_input_conflicts_before_transport(self):
        async def check():
            calls = []

            async def provider(*args):
                calls.append(args)
                return answer({'prompt_tokens': 100, 'completion_tokens': 10})

            gw = self.gateway(provider)
            await self.generate(gw, user_text='alpha')
            with self.assertRaises(OpenAIGatewayError) as caught:
                await self.generate(gw, user_text='bravo')
            self.assertEqual(caught.exception.code, 'request_conflict')
            self.assertEqual(len(calls), 1)

        asyncio.run(check())

    def test_thread_contention_keeps_cap_and_single_owner(self):
        def reserve(_):
            return self.gov.reserve_budget('thread-id', 'agent_fast', estimated_input_tokens=1000)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(16)))
        self.assertEqual(sum(r.reservation_acquired for r in results), 1)
        self.assertEqual(sum(r.allowed_cloud for r in results), 1)
        self.gov.set_policy({'monthly_cap_usd': 1.0})

        def near_cap(i):
            return self.gov.reserve_budget('cap-' + str(i), 'agent_fast', requested_model='gpt-6.1-sol',
                                           estimated_input_tokens=100_000, max_output_tokens=50_000)

        with ThreadPoolExecutor(max_workers=4) as pool:
            cap_results = list(pool.map(near_cap, range(8)))
        self.assertEqual(sum(r.reservation_acquired for r in cap_results), 1)
        self.assertLessEqual(self.gov.summary()['committed_microdollars'], 1_000_000)

    def test_no_reservation_state_is_a_retry_permission(self):
        for state in ('active', 'settled', 'unknown', 'released'):
            request_id = 'state-' + state
            self.gov.reserve_budget(request_id, 'agent_fast')
            if state == 'settled':
                self.gov.record_usage(request_id, 'agent_fast', 'gpt-6.1-sol', prompt_tokens=20, completion_tokens=5)
            elif state == 'unknown':
                self.gov.mark_reservation_unknown(request_id)
            elif state == 'released':
                self.gov.release_reservation(request_id)
            duplicate = BudgetGovernor(self.store).reserve_budget(request_id, 'agent_fast')
            self.assertFalse(duplicate.allowed_cloud)
            self.assertFalse(duplicate.reservation_acquired)
            self.assertEqual(self.reservation(request_id)['status'], state)

    def test_missing_or_partial_usage_stays_unknown_not_zero(self):
        async def check():
            variants = [None, {}, {'prompt_tokens': 100}, {'total_tokens': 100},
                        {'prompt_tokens': 100, 'completion_tokens': True},
                        {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 999},
                        {'prompt_tokens': 100, 'completion_tokens': 10,
                         'prompt_tokens_details': {'cached_tokens': 101}}]
            for i, usage in enumerate(variants):
                async def provider(*_args):
                    return answer(usage)
                rid = 'missing-' + str(i)
                result = await self.generate(self.gateway(provider), request_id=rid)
                self.assertEqual(result['parsed'], {'ok': True})
                self.assertFalse(result['cost_known'])
                self.assertIsNone(result['cost_microdollars'])
                self.assertIsNone(result['cost_usd'])
                self.assertEqual(self.reservation(rid)['status'], 'unknown')
                self.assertGreater(self.reservation(rid)['reserved_microdollars'], 0)
            self.assertEqual(self.gov.summary()['call_count'], 0)
            self.assertGreater(self.gov.summary()['unknown_microdollars'], 0)

        asyncio.run(check())

    def test_real_timeout_and_cancellation_keep_reservation(self):
        async def check():
            entered = asyncio.Event()

            async def provider(*_args):
                entered.set()
                await asyncio.Event().wait()

            gw = self.gateway(provider)
            task = asyncio.create_task(self.generate(gw, request_id='cancelled'))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.reservation('cancelled')['status'], 'unknown')
            with self.assertRaises(OpenAIGatewayError):
                await gw.transcribe_audio(request_id='timeout', audio_bytes=wav(), timeout=0.01)
            self.assertEqual(self.reservation('timeout')['status'], 'unknown')

        asyncio.run(check())

    def test_paid_invalid_output_is_settled_before_validation(self):
        async def check():
            async def provider(*_args):
                return {'choices': [{'message': {'content': 'invalid JSON'}, 'finish_reason': 'stop'}],
                        'usage': {'prompt_tokens': 100, 'completion_tokens': 10}}
            with self.assertRaises(OpenAIGatewayError) as caught:
                await self.generate(self.gateway(provider))
            self.assertEqual(caught.exception.code, 'invalid_json')
            self.assertEqual(self.reservation('budget-request-1')['status'], 'settled')
            self.assertEqual(self.gov.summary()['spent_microdollars'], 300)

        asyncio.run(check())

    def test_token_stt_no_duration_double_charge_and_missing_usage_unknown(self):
        async def check():
            async def token_provider(*_args):
                return {'text': '오늘 날씨', 'duration': 6,
                        'usage': {'type': 'tokens', 'input_tokens': 100, 'output_tokens': 10,
                                  'total_tokens': 110, 'input_token_details': {'audio_tokens': 100, 'text_tokens': 0}}}
            result = await self.gateway(token_provider).transcribe_audio(request_id='token-stt', audio_bytes=wav(6))
            self.assertEqual(result['cost_microdollars'], 175)
            self.assertEqual(self.reservation('token-stt')['reserved_microdollars'], 30_000)

            async def missing_provider(*_args):
                return {'text': '오늘 날씨', 'duration': 6}
            unknown = await self.gateway(missing_provider).transcribe_audio(request_id='missing-stt', audio_bytes=wav(6))
            self.assertIsNone(unknown['cost_microdollars'])
            self.assertEqual(self.reservation('missing-stt')['status'], 'unknown')

            async def duration_provider(*_args):
                return {'text': '오늘 날씨', 'usage': {'type': 'duration', 'seconds': 6}}
            duration = await self.gateway(duration_provider).transcribe_audio(
                request_id='duration-stt', audio_bytes=wav(6), requested_model='whisper-1')
            self.assertEqual(duration['cost_microdollars'], 600)

        asyncio.run(check())

    def test_unverified_compressed_audio_never_sent(self):
        async def check():
            calls = []
            async def provider(*args):
                calls.append(args)
                return {'text': 'unused'}
            with self.assertRaises(OpenAIGatewayError) as caught:
                await self.gateway(provider).transcribe_audio(request_id='compressed', audio_bytes=b'OggS' + b'\0' * 60,
                                                             filename='voice.ogg', mime_type='audio/ogg', duration_seconds=1)
            self.assertEqual(caught.exception.code, 'unverified_audio_duration')
            self.assertFalse(calls)
            self.assertFalse(self.gov.list_reservations())
        asyncio.run(check())

    def test_stt_auto_language_omitted_from_real_multipart(self):
        import httpx

        async def check():
            payloads = []

            async def provider(request):
                payloads.append(await request.aread())
                self.assertEqual(request.url.path, '/v1/audio/transcriptions')
                self.assertTrue(request.headers['content-type'].startswith('multipart/form-data;'))
                return httpx.Response(200, json={'text': '테스트', 'usage': {
                    'type': 'tokens', 'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110}})

            gw = self.gateway(httpx.MockTransport(provider))
            await gw.transcribe_audio(request_id='auto-language', audio_bytes=wav(), language='auto')
            await gw.transcribe_audio(request_id='ko-language', audio_bytes=wav(), language='ko')
            self.assertNotIn(b'name="language"', payloads[0])
            self.assertIn(b'name="language"\r\n\r\nko\r\n', payloads[1])
            self.assertEqual(len(payloads), 2)

            for i, invalid in enumerate(('', None, 'ko-KR', '12', '\r\n', '한국')):
                with self.assertRaises(OpenAIGatewayError) as caught:
                    await gw.transcribe_audio(request_id='invalid-language-' + str(i),
                                              audio_bytes=wav(), language=invalid)
                self.assertEqual(caught.exception.code, 'invalid_language')
            self.assertEqual(len(payloads), 2)
            self.assertEqual(len(self.gov.list_reservations()), 2)

        asyncio.run(check())

    def test_token_stt_minute_estimate_cannot_be_settled_as_provider_usage(self):
        self.gov.reserve_budget('no-usage-stt', 'stt', audio_seconds=6)
        with self.assertRaises(ValueError):
            self.gov.record_usage('no-usage-stt', 'stt', 'gpt-4o-mini-transcribe', audio_seconds=6)
        self.assertEqual(self.reservation('no-usage-stt')['status'], 'active')
        self.assertEqual(self.gov.summary()['call_count'], 0)

    def test_wav_duration_cannot_hide_extra_data_chunks(self):
        original = wav()
        trailing = original + b'data' + (32000).to_bytes(4, 'little') + b'\0' * 32000
        corrected_riff = trailing[:4] + (len(trailing) - 8).to_bytes(4, 'little') + trailing[8:]
        for invalid in (trailing, corrected_riff, original + b'junk'):
            with self.assertRaises(ValueError):
                verify_audio_payload(invalid)

    def test_stt_paid_http_failure_settles_actual_usage(self):
        async def check():
            async def provider(*_args):
                raise OpenAIGatewayError('synthetic error', 'stt_http_error', 500,
                                         response_body={'usage': {'type': 'tokens', 'input_tokens': 100,
                                                                  'output_tokens': 10, 'total_tokens': 110}})
            with self.assertRaises(OpenAIGatewayError):
                await self.gateway(provider).transcribe_audio(request_id='paid-stt-failure', audio_bytes=wav())
            self.assertEqual(self.reservation('paid-stt-failure')['status'], 'settled')
            self.assertEqual(self.gov.summary()['spent_microdollars'], 175)
        asyncio.run(check())

    def test_settlement_uses_original_reservation_billing_month(self):
        self.gov.reserve_budget('month-boundary', 'agent_fast', at_iso='2026-10-31T14:59:59+00:00')
        self.gov.record_usage('month-boundary', 'agent_fast', 'gpt-6.1-sol', prompt_tokens=100,
                              completion_tokens=10, at_iso='2026-10-31T15:00:01+00:00')
        self.assertEqual(self.gov.summary('2026-10')['spent_microdollars'], 300)
        self.assertEqual(self.gov.summary('2026-11')['spent_microdollars'], 0)

    def test_pricing_cached_reads_writes_reasoning_and_long_context(self):
        self.assertEqual(calculate_cost_microdollars('gpt-6.1-sol', prompt_tokens=1000, completion_tokens=100), 3000)
        self.assertEqual(calculate_cost_microdollars('gpt-6.1-sol', prompt_tokens=1000, cached_tokens=1000), 100)
        self.assertEqual(calculate_cost_microdollars('gpt-6.1-terra', prompt_tokens=1000, completion_tokens=100), 1500)
        self.assertEqual(calculate_cost_microdollars('gpt-6.1-sol', prompt_tokens=12000, cached_tokens=4000,
                                                    cache_write_30m_tokens=3000, cache_write_12h_tokens=4000), 29900)
        self.assertEqual(calculate_cost_microdollars('gpt-6.1-sol', prompt_tokens=300000, completion_tokens=1000,
                                                    reasoning_tokens=900), 1215000)
        self.assertEqual(calculate_cost_microdollars('gpt-4o-mini-transcribe', prompt_tokens=100,
                                                    completion_tokens=10, audio_seconds=6), 175)
        self.assertEqual(PRICING_CATALOG_METADATA['checked_on'], '2026-11-14')

    def test_cache_write_usage_and_standard_payload(self):
        async def check():
            payloads = []
            async def provider(_method, _url, _headers, payload):
                payloads.append(payload)
                return answer({'prompt_tokens': 1000, 'completion_tokens': 10,
                               'prompt_tokens_details': {'cached_tokens': 200, 'cache_write_tokens': 400}})
            result = await self.generate(self.gateway(provider))
            self.assertEqual(result['cost_microdollars'], 1920)
            self.assertEqual(payloads[0]['service_tier'], 'default')
            self.assertEqual(payloads[0]['prompt_cache_options'], {'mode': 'implicit', 'ttl': '30m'})
            self.assertGreater(result['budget_decision']['estimated_call_microdollars'], result['cost_microdollars'])
        asyncio.run(check())

    def test_price_unknown_tier_keeps_reservation(self):
        async def check():
            async def provider(*_args):
                return answer({'prompt_tokens': 100, 'completion_tokens': 10}, service_tier='priority')
            result = await self.generate(self.gateway(provider))
            self.assertFalse(result['cost_known'])
            self.assertEqual(self.reservation('budget-request-1')['status'], 'unknown')
        asyncio.run(check())

    def test_canonical_env_seed_and_saved_policy_survives_restart(self):
        with patch.dict(os.environ, {'HUB_OPENAI_MONTHLY_CAP_USD': '5', 'HUB_OPENAI_ALLOW_SHARED_NOTES': 'true'}):
            self.assertEqual(policy_from_env().monthly_cap_usd, 5)
            self.assertTrue(policy_from_env().allow_shared_notes_in_cloud)
            other = Store(Path(self.tmp.name) / 'seed.sqlite3')
            governor = BudgetGovernor(other)
            self.assertEqual(governor.policy().monthly_cap_usd, 5)
            governor.set_policy({'monthly_cap_usd': 3.0})
        self.assertEqual(BudgetGovernor(other).policy().monthly_cap_usd, 3)


if __name__ == '__main__':
    unittest.main()
