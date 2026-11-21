import unittest
import os
from unittest.mock import Mock
import importlib.util
from pathlib import Path

os.environ.setdefault('GCHAT_PILOT_SPACE', 'spaces/pilot_test_space')
os.environ.setdefault('GCHAT_PILOT_BOT_USER', 'users/pilot_test_bot')
os.environ.setdefault('GCHAT_PILOT_APP_SA', 'pilot-test-bot@example-iam.iam.gserviceaccount.com')

# Works both standalone (repo/thread_pilot.py) and inside the hermes-agent tree
# (plugins/platforms/google_chat/thread_pilot.py).
_here = Path(__file__).resolve().parent
_candidate = next(
    c for c in (
        _here.parent / 'thread_pilot.py',
        _here.parents[1] / 'plugins' / 'platforms' / 'google_chat' / 'thread_pilot.py',
    ) if c.exists()
)
spec = importlib.util.spec_from_file_location('thread_pilot', _candidate)
assert spec and spec.loader
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

T = p.SPACE + '/threads/test'
M = p.SPACE + '/messages/test.q'


def question():
    return dict(name=M, text='納期は？', sender={'type': 'HUMAN'}, thread={'name': T}, annotations=[{'type': 'USER_MENTION', 'userMention': {'type': 'MENTION', 'user': {'type': 'BOT', 'name': p.BOT}}}])


def row(id, text, thread=T, sender='HUMAN'):
    return dict(name=p.SPACE+'/messages/test.'+id, text=text, thread={'name':thread}, sender={'type':sender})


class Pilot(unittest.TestCase):
    def setUp(self):
        # Incremental-context marker is module state; isolate per test.
        p._LAST_SEEN.clear()
    def test_only_self_mention(self):
        m = question()
        self.assertTrue(p.eligible(m))
        for changed in [dict(annotations=[]), dict(sender={'type':'BOT'}), dict(thread={'name':'spaces/else/threads/x'}), dict(thread={'name':T+'" OR x'}), dict(slashCommand={'commandId':'1'})]:
            self.assertFalse(p.eligible(m | changed))
        m['annotations'][0]['userMention']['user']['name'] = 'users/other'
        self.assertFalse(p.eligible(m))

    def test_two_pages(self):
        f = Mock(side_effect=[{'messages':[row('a','納期は金曜日'),row('b','別スレ',T+'2'),row('c','BOT発言',sender='BOT'),row('d','私信') | {'privateMessageViewer':{}}], 'nextPageToken':'p2'}, {'messages':[row('e','確認済み'),question()]}])
        answer = p.extract(question() | {'argumentText':'まとめて'}, f)
        for want in ['納期は金曜日','確認済み']: self.assertIn(want, answer)
        for other in ['別スレ','BOT発言','私信','納期は？']: self.assertNotIn(other, answer)
        self.assertEqual([c.args for c in f.call_args_list], [(T,None),(T,'p2')])

    def test_full_thread_is_default(self):
        result = p.extract(question(), lambda *_: {'messages':[row('a','納期は金曜日'),row('b','昼食はカレー')]})
        self.assertIn('金曜日', result)
        self.assertIn('カレー', result)

    def test_denial_discards_partial(self):
        f = Mock(side_effect=[{'messages':[row('a','推測禁止')], 'nextPageToken':'p'}, PermissionError()])
        self.assertEqual(p.extract(question(), f), p.DENIED)

    def test_no_mention_no_read(self):
        f = Mock(); self.assertIsNone(p.extract(question() | {'annotations':[]}, f)); f.assert_not_called()

    def test_wrong_credentials(self):
        self.assertEqual(p.answer(question(), object()), p.NO_IDENTITY)
