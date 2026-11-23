"""Standalone tests for the agent ack card (google_chat/ack.py).

Runs from the repo root (``python -m pytest tests/test_ack_local.py``) or
inside the hermes-agent tree, same resolution as test_thread_pilot_local.
"""
import unittest
import os
import importlib.util
from pathlib import Path

# Works both standalone (repo/ack.py) and inside the hermes-agent tree
# (plugins/platforms/google_chat/ack.py).
_here = Path(__file__).resolve().parent
_candidate = next(
    c for c in (
        _here.parent / 'ack.py',
        _here.parents[1] / 'plugins' / 'platforms' / 'google_chat' / 'ack.py',
    ) if c.exists()
)
spec = importlib.util.spec_from_file_location('ack', _candidate)
assert spec and spec.loader
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

BOT = 'users/bot'


def mention(text, sender='HUMAN'):
    return dict(text=text, sender={'type': sender},
                annotations=[{'type': 'USER_MENTION', 'userMention': {'type': 'MENTION', 'user': {'type': 'BOT', 'name': BOT}}}])


class Flag(unittest.TestCase):
    def test_enabled_only_when_true(self):
        self.assertFalse(a.enabled({}))
        self.assertFalse(a.enabled({'agent_ack_card': False}))
        self.assertTrue(a.enabled({'agent_ack_card': True}))

    def test_mentions_bot(self):
        self.assertTrue(a.mentions_bot(mention('hi'), BOT))
        self.assertFalse(a.mentions_bot(mention('hi'), ''))          # no bot id yet
        self.assertFalse(a.mentions_bot(dict(mention('hi'), sender={'type': 'BOT'}), BOT))
        self.assertFalse(a.mentions_bot(dict(text='hi', sender={'type': 'HUMAN'}, annotations=[]), BOT))


class CardText(unittest.TestCase):
    def test_survey_default_for_questions(self):
        self.assertEqual(a.card_text(mention('恵比寿と成城の問い合わせ数を比較できる？')), a.ACK_SURVEY)
        self.assertEqual(a.card_text(mention('明日の定例のページ作れるか確認して')), a.ACK_SURVEY)
        self.assertEqual(a.card_text(mention('いつできますか？')), a.ACK_SURVEY)

    def test_build_for_creation_requests(self):
        self.assertEqual(a.card_text(mention('成城のアクセスマップ作って')), a.ACK_BUILD)
        self.assertEqual(a.card_text(mention('サムネイルのデザイン生成お願いします')), a.ACK_BUILD)

    def test_skip_for_thanks_and_reports(self):
        self.assertIsNone(a.card_text(mention('ありがとうございます！')))
        self.assertIsNone(a.card_text(mention('修正完了しました。ご確認ください')))
        self.assertIsNone(a.card_text(mention('お疲れ様です！')))

    def test_ask_beats_skip(self):
        # Thanks + request together must still acknowledge.
        self.assertIsNotNone(a.card_text(mention('ありがとうございます！ついでに◯町の地図も確認していただけますか')))

    def test_no_text(self):
        self.assertIsNone(a.card_text(mention('')))
        self.assertIsNone(a.card_text(dict(text='', sender={'type': 'HUMAN'}, annotations=[])))


if __name__ == '__main__':
    unittest.main()
