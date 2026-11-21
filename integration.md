# gateway への組み込み方法（最小差分）

Hermes Agent の `gateway/run_inbound.py` に、メッセージ処理の入口で以下のブロックを
追加するだけです。**これ以外の本体変更は不要**です。

## 挿入位置

受信メッセージをエージェントターンに渡す直前（`is_internal` 判定の直後）。
既存コードに合わせて `event` / `source` / `self.adapters` の名前だけ読み替えてください。

## 追加コード

```python
        if not is_internal and source.platform.value == "google_chat":
            from gateway.session_ingress import executing_admission
            if not executing_admission.get():
                from plugins.platforms.google_chat import thread_pilot
                _pilot_adapter = self.adapters.get(source.platform)
                if (_pilot_adapter is not None
                        and source.chat_id == thread_pilot.SPACE
                        and _pilot_adapter.config.extra.get("thread_read_pilot") is True):
                    from gateway import run_inbound as _ri_mod
                    _ri_mod.logger.warning("[PilotE2E] mention detected chat=%s thread=%s",
                                           source.chat_id, source.thread_id)
                    _pilot_event = await self._pilot_context(event, source, _pilot_adapter)
                    if _pilot_event is None:
                        return None
                    event = _pilot_event
```

## `_pilot_context`（GatewayRunner のメソッドとして追加）

```python
    async def _pilot_context(self, event, source, adapter):
        """B案: スレッド履歴＋添付をイベントに載せてエージェントターンへ。

        eligible（@メンション・HUMAN・パイロットスペース・グループ）以外は None
        を返して素通し。トランスクリプトは「推測で補わず、この内容を踏まえて
        答えてください」のガイド文付きで event.text に追記する。
        """
        msg = getattr(event, "raw_message", None) or {}
        creds = getattr(adapter, "_dwd_credentials", None) or getattr(adapter, "_credentials", None)
        transcript = thread_pilot.answer(msg, creds)
        if transcript is None:
            return None
        urls, types = thread_pilot.fetch_attachments(msg, creds,
                                                     event.media_urls, event.media_types)
        text = (event.text or "") + "\n---\n[" + transcript
        + "\n—— 推測で補わず、この内容を踏まえて答えてください]"
        event.text = text
        event.media_urls = urls
        event.media_types = types
        return event
```

## 設定

`config.yaml` の google_chat プラットフォーム設定に:

```yaml
platforms:
  google_chat:
    thread_read_pilot: true
```

## 重要な実装上の注意

1. **drain 二重実行ガード**: 永続キューの実行機（drain）がターンを再実行する際、
   この分岐が再度走るとリストア後のイベント（`raw_message` 空）を非メンションと
   誤判定して **ターン全体が破棄** されます。必ず `executing_admission` フラグで
   実行中をスキップしてください（これが「(no reply)」沈黙の原因でした）。
2. **直接代入**: `event.text` / `media_urls` は dataclasses.replace ではなく
   直接代入してください（SimpleNamespace 形式のテストダブル対策）。
3. **失敗は素通し**: 取得に失敗しても返信パスは壊さない（デグレード動作）。
