"""Small, provider-independent short-utterance metrics; no model or data access.

CER uses the unchanged normalization/Levenshtein convention of the existing
deploy/termux/speech_model.py comparison. That Linux-only control module is not
imported here. WER is whitespace-tokenized, not a Korean morphological score.
"""
from __future__ import annotations

import unicodedata


def normalize_for_cer(text: str) -> str:
    return ''.join(ch for ch in unicodedata.normalize('NFC', text).casefold()
                   if not ch.isspace() and not unicodedata.category(ch).startswith('P'))


def _distance(left, right) -> int:
    previous = list(range(len(right) + 1))
    for index, item in enumerate(left, 1):
        row = [index]
        for other, value in enumerate(right, 1):
            row.append(min(row[-1] + 1, previous[other] + 1,
                           previous[other - 1] + (item != value)))
        previous = row
    return previous[-1]


def text_errors(reference: str, hypothesis: str) -> dict:
    if not isinstance(reference, str) or not isinstance(hypothesis, str):
        raise ValueError('Reference and transcript must be text')
    left, right = normalize_for_cer(reference), normalize_for_cer(hypothesis)
    if not left or max(len(left), len(right)) > 4000:
        raise ValueError('Reference must be scorable; short transcripts have a 4000-character limit')
    def words(text):
        return ''.join(ch for ch in unicodedata.normalize('NFC', text).casefold()
                       if not unicodedata.category(ch).startswith('P')).split()
    word_left, word_right = words(reference), words(hypothesis)
    char_edits, word_edits = _distance(left, right), _distance(word_left, word_right)
    return {'character_edits': char_edits, 'reference_characters': len(left),
            'cer': char_edits / len(left), 'word_edits': word_edits,
            'reference_words': len(word_left), 'wer': word_edits / len(word_left),
            'normalized_exact': left == right}


def percentile(values: list[float], fraction: float) -> float | None:
    """Linear interpolation; small sample p95 is not a stable service SLA."""
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)
