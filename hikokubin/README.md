# 秘匿便

CoCの秘匿HOをDiscordの個別DMへ配布する、KP向けWebアプリです。利用時にBotトークンを入力する必要はありません。

## 主な機能

- Discord OAuth2ログイン
- 秘匿HOの作成・編集・削除
- DiscordユーザーIDを指定して個別DM送信
- 送信前の宛先確認
- 送信済み／受領済み管理
- KPごとにHOを分離
- スマートフォン対応

## 1. Discord側の準備

1. [Discord Developer Portal](https://discord.com/developers/applications)でNew Applicationを押す
2. OAuth2でRedirectsに `https://あなたのRailwayドメイン/callback` を登録
3. BotページでBotを作成し、トークンを控える
4. OAuth2 → URL Generatorで `bot` を選び、Botを使用するサーバーへ招待

Botに管理者権限は不要です。DMを送る相手とBotが同じサーバーに参加している必要があります。

## 2. GitHubへ置く

このフォルダーの中身を新しいGitHubリポジトリへアップロードします。`.env`やBotトークンは絶対にGitHubへ置かないでください。

## 3. Railwayへ公開

1. RailwayでNew Project → Deploy from GitHub repo
2. 秘匿便のリポジトリを選択
3. Variablesへ次を登録

| 変数 | 内容 |
|---|---|
| `DISCORD_CLIENT_ID` | Discord Application ID |
| `DISCORD_CLIENT_SECRET` | OAuth2 Client Secret |
| `DISCORD_BOT_TOKEN` | Bot Token |
| `SESSION_SECRET` | 長いランダム文字列 |
| `BASE_URL` | `https://～.up.railway.app`（末尾 `/` なし） |
| `DATA_DIR` | `/data` |

4. RailwayでVolumeを追加し、Mount Pathを `/data` にする
5. Settings → Networkingで公開ドメインを発行
6. 発行されたURLを`BASE_URL`へ設定
7. Discord Developer PortalのRedirectsへ同じURL＋`/callback`を登録

## ローカル確認

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt
copy .env.example .env
```

環境変数を設定して `python app.py` を実行します。

## 大切な注意

- BotトークンはRailway Variablesだけに保存します。
- RailwayのVolumeを付けない場合、再デプロイでHOデータが消える可能性があります。
- HO本文はRailway上のSQLiteへ保存されます。運営者は秘密情報を扱う責任があります。
- DiscordユーザーIDは、Discordの開発者モードをONにして相手を右クリックするとコピーできます。
