"""Agent acknowledge card (feature flag: ``agent_ack_card``).

An instant, rule-based "working on it" bubble posted the moment a mention
arrives, before any model sees the turn. Content-shaped wording only
(survey / build / skip) — one bot voice for every sender (owner decision
2026-11-23). Tuned against the owner's real reply history: short, casual,
no emoji spam (median 50 chars per reply).

This is deliberately separate from the K8-PLUS thread-read pilot
(``thread_pilot.py``): different feature flag, no shared state. Turning
``agent_ack_card`` off must not change pilot behavior, and vice versa.
"""

# Wording. Kept short; send() PATCHes the card in place with the real reply.
ACK_SURVEY = '📊 調べています。完了したらこのスレッドに返信します。'
ACK_BUILD = '✍️ 作成しています。完了したらこのスレッドに返信します。'

_ASK_HINTS = ('?', '？', 'できますか', '教えて', '確認', '調べ', '比較', '見せて',
              'お願いします', 'お願いしたい', 'ヒアリング', 'どうでしょう', 'いかが')
_BUILD_HINTS = ('作って', '作成', '生成', '作り', 'デザインして', '書いて', '作れますか')
# Thanks only: ack is skipped unless the text also carries a new ask.
_THANKS_HINTS = ('ありがとう', 'お疲れ様です', 'おつかれさま', 'おつかれです', '助かりました')
# Status reports: no ack even when they end in "please confirm" — the turn is
# a notification, and a "working on it" bubble would read as confusion.
_REPORT_HINTS = ('完了しました', '対応しました', '修正しました', '修正完了', '対応完了',
                 'アップしました', 'アップロードしました')


def enabled(extra: dict) -> bool:
    """Feature flag: ``config.extra['agent_ack_card'] is True``."""
    return extra.get("agent_ack_card") is True


def mentions_bot(m: dict, bot_user_name: str) -> bool:
    """True when a HUMAN post carries a USER_MENTION of the bot."""
    return bool(bot_user_name
                and (m.get('sender') or {}).get('type') == 'HUMAN'
                and any(a.get('type') == 'USER_MENTION'
                        and (a.get('userMention') or {}).get('user', {}).get('name') == bot_user_name
                        for a in m.get('annotations', [])))


def card_text(m: dict) -> "str | None":
    """Wording for the ack card, or None to skip the card for this turn.

    Rules (checked in order):
    1. No text → None.
    2. No ask/request AND a thanks/report marker → None (substantive reply
       follows in the same turn; an extra bubble would be noise).
    3. Build keywords → ACK_BUILD, else → ACK_SURVEY (default: anything
       unanswered reads as a question worth acknowledging).
    """
    text = (m.get('argumentText') or m.get('text') or '').strip()
    if not text:
        return None
    # Status reports first: notifications beat trailing "please confirm".
    if any(h in text for h in _REPORT_HINTS):
        return None
    if not any(h in text for h in _ASK_HINTS) and any(h in text for h in _THANKS_HINTS):
        return None
    if any(h in text for h in _BUILD_HINTS):
        return ACK_BUILD
    return ACK_SURVEY
