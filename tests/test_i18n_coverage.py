"""Every string passed to a translator must exist in every non-default language.

Missing keys fall back to English, which is silent at render time — the page just
shows a stray English label. This scans the call sites instead so a new
``t('...')`` without an Indonesian entry fails the suite.
"""
import re
from pathlib import Path

import i18n

ROOT = Path(__file__).resolve().parent.parent

# Files that call a translator: t('...') in Python/Jinja/JS, plus _err('...') in app.py.
SOURCES = [
    'app.py', 'telegram_bot.py', 'services.py', 'reports.py',
    'static/js/app.js',
    *(str(p.relative_to(ROOT)) for p in sorted((ROOT / 'templates').glob('*.html'))),
]

# t('literal' or "literal" ... — not preceded by a word char or dot, so `print(`,
# `.format(`, `parse_cost(` and friends don't match. Dynamic calls such as
# t(order['status']) have no leading quote and are skipped.
CALL = re.compile(r"""(?<![\w.])(?:t|_err)\(\s*(['"])(.*?)\1""")


def _sources_used():
    found = {}
    for rel in SOURCES:
        text = (ROOT / rel).read_text(encoding='utf-8')
        for _, literal in CALL.findall(text):
            found.setdefault(literal, rel)
    return found


def test_every_translated_literal_has_an_indonesian_entry():
    table = i18n.TRANSLATIONS['id']
    missing = sorted((lit, rel) for lit, rel in _sources_used().items() if lit not in table)
    assert not missing, 'no Indonesian translation for: ' + ', '.join(
        f'{lit!r} ({rel})' for lit, rel in missing)


def test_scanner_actually_finds_call_sites():
    # Guards the regex itself: a silent zero-match scan would make the test above vacuous.
    used = _sources_used()
    assert len(used) > 50
    assert 'Dashboard' in used


def test_placeholders_match_their_translation():
    # A translation that drops or renames a {token} raises KeyError/IndexError at
    # render time, long after the typo was introduced.
    for src, dst in i18n.TRANSLATIONS['id'].items():
        assert set(re.findall(r'\{(\w+)\}', src)) == set(re.findall(r'\{(\w+)\}', dst)), src
