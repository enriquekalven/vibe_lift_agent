"""Versioned reference data: schema, the 30-day freshness tripwire, effective-dated prices and mirrors."""

from __future__ import annotations

import copy
import datetime
import re
import unittest

from finops_dashboard import billing_export, finops, reference_data, telemetry
from finops_dashboard.ui import template as ui_template


class ReferenceDataTest(unittest.TestCase):

  def test_reference_file_validates(self) -> None:
    reference_data.validate(reference_data.load())
    self.assertRegex(reference_data.version(), r'^\d{4}-\d{2}-\d{2}\.\d+$')

  def test_every_section_was_checked_within_max_age(self) -> None:
    # Tripwire: when this fails, re-verify the section against its source page, update the values,
    # then bump the section's "checked" date and the file "version".
    data = reference_data.load()
    today = datetime.datetime.now(datetime.UTC).date()
    for name, checked in reference_data.checked_dates().items():
      with self.subTest(section=name):
        sec = data['sections'][name]
        src = sec.get('source') or sec.get('price_source')
        self.assertLessEqual(checked, today + datetime.timedelta(days=1), f'{name}: checked date is in the future')
        age = (today - checked).days
        self.assertLessEqual(
            age, data['max_age_days'],
            f'{name} was last checked {checked} ({age} days ago). Re-verify it against {src}, '
            "then bump its 'checked' date and the file 'version'.")

  def test_validation_rejects_malformed_data(self) -> None:
    good = reference_data.load()
    mutations = {
        'negative price': lambda d: d['sections']['model_list_prices']['models']['gemini-2.5-pro'].update(input=-1),
        'bad checked date': lambda d: d['sections']['model_lifecycle'].update(checked='16/10/2026'),
        'unsorted schedule': lambda d: d['sections']['model_list_prices']['models']['gemini-3.8-flash']['schedule'].append(
            {'effective_from': '2026-01-01', 'input': 1, 'cached_read': 1, 'cache_write': 1, 'output': 1}),
        'empty replacements': lambda d: d['sections']['model_lifecycle']['models']['gemini-2.5-pro'].update(replacements=[]),
        'bad credit type': lambda d: d['sections']['billing_credit_types'].update(promotional=['promotion']),
        'zero GSU throughput': lambda d: d['sections']['provisioned_throughput']['models']['gemini-2.5-flash'].update(
            tokens_per_second_per_gsu=0),
    }
    for label, mutate in mutations.items():
      with self.subTest(label):
        bad = copy.deepcopy(good)
        mutate(bad)
        with self.assertRaises(ValueError):
          reference_data.validate(bad)

  def test_effective_dated_price_change(self) -> None:
    before = reference_data.list_prices(datetime.date(2026, 12, 31))['gemini-3.8-flash']
    after = reference_data.list_prices(datetime.date(2027, 1, 1))['gemini-3.8-flash']
    self.assertEqual(before, {'input': 0.75, 'cached_read': 0.075, 'cache_write': 0.75, 'output': 3.75})
    self.assertEqual(after, {'input': 1.5, 'cached_read': 0.15, 'cache_write': 1.5, 'output': 7.5})

  def test_consumers_read_the_reference_file(self) -> None:
    prices = reference_data.list_prices()
    self.assertEqual(set(telemetry.RATE_CARDS), set(prices))
    for name, card in telemetry.RATE_CARDS.items():
      self.assertEqual((card.input_per_million_usd, card.output_per_million_usd), (prices[name]['input'], prices[name]['output']))
    self.assertEqual(finops.PT_GSU_SPECS, reference_data.pt_specs())
    self.assertEqual(finops.MODEL_LIFECYCLE, reference_data.model_lifecycle())
    self.assertEqual((finops.GSU_MONTHLY_1MO_USD, finops.GSU_MONTHLY_1YR_USD), reference_data.gsu_monthly_prices())
    self.assertEqual(billing_export.COMMITMENT_CREDIT_TYPES, reference_data.credit_types('commitment'))
    self.assertEqual(billing_export.PROMOTIONAL_CREDIT_TYPES, reference_data.credit_types('promotional'))

  def test_js_rate_card_mirror_matches_reference(self) -> None:
    js = (ui_template._STATIC_DIR / ui_template.ENHANCEMENTS_JS_FILE).read_text(encoding='utf-8')  # pylint: disable=protected-access
    block = re.search(r'var DEFAULT_RATE_CARDS = \{(.*?)\n  \};', js, flags=re.S)
    self.assertIsNotNone(block)
    rows = re.findall(r"'([\w.\-]+)': \{input: ([\d.]+), output: ([\d.]+), cached_read: ([\d.]+), cache_write: ([\d.]+)\}",
                      block.group(1))
    self.assertGreaterEqual(len(rows), 5)
    prices = reference_data.list_prices()
    for model, inp, out, cached, write in rows:
      with self.subTest(model=model):
        self.assertIn(model, prices)
        self.assertEqual((float(inp), float(out), float(cached), float(write)),
                         (prices[model]['input'], prices[model]['output'], prices[model]['cached_read'],
                          prices[model]['cache_write']))

  def test_current_replacements_are_priced(self) -> None:
    # The retirement radar prices the same tokens on the replacement; an unpriced replacement shows no delta.
    prices = reference_data.list_prices()
    for model, (_, replacements) in reference_data.model_lifecycle().items():
      for rep in replacements:
        with self.subTest(model=model, replacement=rep):
          self.assertIn(rep, prices)

  def test_model_key_normalizes_resource_names(self) -> None:
    self.assertEqual(reference_data.model_key('publishers/google/models/gemini-2.0-flash-001'), 'gemini-2.0-flash')
    self.assertEqual(reference_data.model_key('Gemini-2.5-Pro'), 'gemini-2.5-pro')
    self.assertEqual(reference_data.model_key(None), '')

  def test_burndown_tokens(self) -> None:
    # gemini-2.5-flash: output and reasoning burn 9x, cache reads count as input (no published rate).
    self.assertEqual(reference_data.burndown_tokens('gemini-2.5-flash', input_tokens=100, output_tokens=10,
                                                    reasoning_tokens=1, cache_read_tokens=50), 100 + 99 + 50)
    self.assertAlmostEqual(reference_data.burndown_tokens('gemini-3.8-flash', cache_read_tokens=1000), 100.0)
    self.assertIsNone(reference_data.burndown_tokens('claude-sonnet-4-6', input_tokens=10))


if __name__ == '__main__':
  unittest.main()
