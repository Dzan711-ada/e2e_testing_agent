"""Secret encoding happens once and remains bounded by resource lifetime."""
import json
import unittest
from unittest.mock import patch
from urllib.parse import quote, quote_plus

import e2e_agent.resources as resources
from e2e_agent.resources import Redactor


class RedactorCacheTests(unittest.TestCase):
    def test_repeated_redaction_does_not_reencode_registered_values(self):
        redactor = Redactor()
        secret = 'CachePrivate9 "\\ 中文'
        dumps = json.dumps
        with patch.object(resources, "quote", wraps=quote) as encode_url, \
             patch.object(resources, "quote_plus", wraps=quote_plus) as encode_form, \
             patch.object(resources.json, "dumps", wraps=dumps) as encode_json:
            redactor.register(secret)
            registered = (encode_url.call_count, encode_form.call_count, encode_json.call_count)
            self.assertTrue(all(registered))
            redactor.register(secret)
            for _ in range(100):
                self.assertEqual(redactor.text("before " + secret + " after"), "before [RESOURCE] after")
            self.assertEqual((encode_url.call_count, encode_form.call_count, encode_json.call_count), registered)

    def test_raw_url_python_json_variants_and_longest_values_stay_masked(self):
        secret = 'Private "quote\' \\ 中文 space'
        variants = {secret, quote(secret, safe=""), quote_plus(secret), repr(secret)[1:-1],
                    json.dumps(secret, ensure_ascii=False)[1:-1], json.dumps(secret, ensure_ascii=True)[1:-1]}
        redactor = Redactor()
        redactor.register(secret)
        for variant in variants:
            with self.subTest(variant_kind=len(variant)):
                self.assertEqual(redactor.text("prefix " + variant + " suffix"), "prefix [RESOURCE] suffix")
        redactor.register("overlap")
        redactor.register("overlap-longer")
        self.assertEqual(redactor.text("overlap-longer overlap"), "[RESOURCE] [RESOURCE]")
        self.assertEqual(redactor.data({"nested": [secret]}), {"nested": ["[RESOURCE]"]})

    def test_clear_removes_all_cached_private_variants_before_next_resource(self):
        old, current = "OldCachePrivate3", "NewCachePrivate7"
        redactor = Redactor()
        redactor.register(old)
        self.assertEqual(redactor.text(old), "[RESOURCE]")
        redactor.clear()
        self.assertEqual(redactor.text(old), old)
        self.assertNotIn(old, repr(redactor.__dict__))
        redactor.register(current)
        self.assertEqual(redactor.text(old + " " + current), old + " [RESOURCE]")
        redactor.clear()
        self.assertNotIn(current, repr(redactor.__dict__))
