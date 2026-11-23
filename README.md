# Google Chat スレッド読み取りパイロット（Hermes Agent 用）

Google Chat アプリボットが **@メンションされたスレッドの会話履歴と添付ファイルを読んだ上で自然に返信する** ようにする、Hermes Agent 用のモジュール＋統合パッチです。

 Qiita 記事「Google Chat の AIボットを「スレッド全部読めるアシスタント」にする実装記（Hermes Agent）」の完全再現用リポジトリです。

## できること

- @メンション時に **同じスレッドの人間の投稿履歴** を取得して文脈にする
- 添付 **画像・ファイルを実ダウンロード** してモデルに見せる（画像は vision で読む）
- **増分読み込み**: 初回のみ直近100件・最大8000字、2回目以降は新着差分のみ（新着なしなら1行）
- 引用返信対応（Chat が引用文を本文に埋め込むため、古い発言の引用でも内容が届く）
- **引用返信の送信**（2026-11-22 実測）: Chat API の `quotedMessageMetadata`（`quoteType=REPLY`・引用元メッセージの `name` と `lastUpdateTime` 必須。未編集メッセージは `createTime` で代用可）で、bot が特定メッセージを引用バブル付きで返信できる。ルートメッセージを引用すると新ルートになる、同スレッド内のメッセージを引用すればそのスレッドに返信
- **テキストの部分引用**: bot の返信本文は Markdown 対応で `> 引用文` のブロック引用が装飾表示されるため、元メッセージの一部の文章だけを引用表示できる（thread_pilot.py は履歴コンテキストにこの表示メモを同梱する）
- bot 同士のメンションは無視（無限ループ防止: 送信者 HUMAN のみ処理）

## 構成

```
thread_pilot.py      パイロット本体（スレッド取得・増分・添付DL）— 自己完結
dwd.py               Domain-wide Delegation のユーザー代理トークン処理
integration.md       gateway への組み込み方法（最小限の差分）
tests/               自己完結テスト
```

## セットアップ概要

詳細手順は README ではなく記事にスクショ付きで載せています。ここでは必要事項だけ:

1. GCP プロジェクトで Chat アプリ用 Service Account を作成
2. Chat アプリを登録（Pub/Sub で `MESSAGE` イベントを受ける構成）
3. Admin Console の ドメイン全体の委任 で DWD クライアント登録（スコープ: `chat.messages.readonly`, `chat.spaces.readonly`）
4. 以下を環境変数（.env）に設定:

```ini
# Chat アプリの基本設定（Hermes 側の既存キー）
GOOGLE_CHAT_PROJECT_ID=<your-gcp-project>
GOOGLE_CHAT_SUBSCRIPTION_NAME=projects/<project>/subscriptions/<name>
GOOGLE_CHAT_SERVICE_ACCOUNT_JSON=/path/to/sa-key.json
GOOGLE_CHAT_ALLOWED_USERS=<allowed@example.com>
GOOGLE_CHAT_IMPERSONATE_USER=<delegated-user@example.com>

# パイロットのバインディング（自分の環境の値に置き換える）
GCHAT_PILOT_SPACE=spaces/XXXX          # 対象スペース
GCHAT_PILOT_BOT_USER=users/XXXX        # Chat アプリの user id
GCHAT_PILOT_APP_SA=<bot-sa>@<project>.iam.gserviceaccount.com
```

5. `integration.md` に従って gateway に統合（本体への変更は最小限）
6. gateway 再起動

## 追加改修: 引用返信の自動化（adapter patch）

`thread_pilot.py` 単体では返信は通常の送信になります。人間のメンションに応答するときに
返信へ自動で引用気泡（`quotedMessageMetadata`）を付けたい場合、本体の
`plugins/platforms/google_chat/adapter.py` にパッチを当てます:

```bash
# hermes-agent の作業コピーで
git apply patches/0001-quote-reply-automation-and-fallback.patch
```

内容:

- 人間からのメンション受信時、トリガーメッセージの name/lastUpdateTime（未編集なら createTime）をスレッド単位で退避
- そのターン内の実質的な返信（ステータスバブル `⏳/💭/💾/·` を除く messages.create）すべてに `quotedMessageMetadata` を自動付与。ターン終了で退避をクリア
- 引用元が実在しない（削除済み・合成イベント）などで create が 400 になった場合、引用を外して1回だけ再送するフォールバック付き — 返信がエラーで落ちない
- 注意: 引用先は Chat 上に実在するメッセージである必要があります（Pub/Sub 経由の合成メッセージは実体がないため引用できず、フォールバック経路に落ちます）

## 追加改修: エージェント受領カード（adapter patch、pilot とは独立）

メンション直後に「調べています／作成しています」を受領カードとして即投稿する機能
（2026-11-23 追加）。スレッド読み取りパイロット（`thread_pilot.py`）とは**独立**で、
フラグ・モジュールとも別です。

```bash
# hermes-agent の作業コピーで
git apply patches/0002-agent-ack-card.patch
```

構成:

- `plugins/platforms/google_chat/ack.py`（新規・純粋関数のみ）: 文言判定の本体
- `adapter.py` への配線: 受信時に文言を決定 → `send_typing` が通常のタイピングカードの代わりに受領カードを出す（`send()` が PATCH で実返信に置き換わるため投稿が増えない）

スイッチ（config.yaml）:

```yaml
platforms:
  google_chat:
    agent_ack_card: false   # ← これで止める（コード削除・再インストール不要）
```

文言ルール（ルールベース・モデルを待たない。`ack.card_text`）:

- 調査系（質問・比較・確認）→ `📊 調べています。完了したらこのスレッドに返信します。`
- 作成系（作って・生成・デザインして）→ `✍️ 作成しています。完了したらこのスレッドに返信します。`
- 完了報告・お礼のみのメンション → カードなし（同一ターンで直接返信）
- 人によるトーン変更はしない（bot は 1 つの声）。実データ準拠: 本人の返信中央値 50 文字・カジュアル

## テスト

```bash
python -m pytest tests/ -v
```

## 注意事項

- **DWD・SA鍵は読み取り専用**（`chat.messages.readonly`）です。ボットの代理投稿は行いません
- **履歴はスレッド単位のみ**。スペース全体や他スペースは読みません
- **@なし投稿には反応しません**（アプリの「その他のメッセージ」権限は別途）
- **受信添付の一時ダウンロードは7日（`GCHAT_PILOT_TMP_TTL_HOURS`で変更可）で自動削除**。再DLされるので実害なし（2026-11-22）。Drive側の送信物は自動削除しない（リンク切れ防止）
- **bot 同士のメンションは無視**されます（ループ防止）
- 添付のダウンロード先はテンポラリ（`tempfile.gettempdir()/gchat_pilot`）

## License

MIT
