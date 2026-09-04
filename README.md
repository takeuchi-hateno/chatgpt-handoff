# ChatGPT Handoff

Gitリポジトリの作業差分を、ChatGPTへ渡しやすい軽量ZIPにまとめるCodex Skillです。レビューに必要な差分、変更ファイル、テスト結果などを毎回同じ場所へ生成します。

このSkillはローカル成果物の生成だけを行います。commit、push、deploy、migration、DB接続、外部サービスへのuploadは行いません。

## 生成されるもの

実行すると、次のファイルを作成します。

```text
~/ChatGPT-Handoff/<project>/latest/
├── README_FOR_CHATGPT.md
├── git-diff-all.txt
└── review.zip

~/ChatGPT-Handoff/LATEST_REVIEW.zip
```

`review.zip`には、レビューに必要なテキストだけを収録します。

- ChatGPT向けREADME
- staged / unstaged diff
- 安全に読み取れるuntracked text
- 変更されたソースコード、テスト、Markdown、docs
- `handoff`または`HANDOFF.md`

依存関係、build成果物、画像、動画、PDF、ZIP、DB dump、秘密情報と判断したパスは除外します。

## 必要な環境

- Python 3.10以上
- Git
- macOSまたはLinux

macOSで動作確認しています。Windowsは未検証です。

## インストール

CodexのSkillディレクトリへcloneします。

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
git clone https://github.com/takeuchi-hateno/chatgpt-handoff.git \
  "${CODEX_HOME:-$HOME/.codex}/skills/chatgpt-handoff"
```

すでに同名ディレクトリがある場合は、内容を確認してから更新してください。

## 使い方

Codexでは次のように依頼します。

```text
$chatgpt-handoff を使って、このリポジトリのレビュー成果物を作成してください。
```

スクリプトを直接実行する場合は、対象のGitリポジトリで次を実行します。

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/chatgpt-handoff/scripts/generate_handoff.py"
```

別のリポジトリを指定する場合:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/chatgpt-handoff/scripts/generate_handoff.py" \
  --repo /path/to/repository
```

書き込み前に対象範囲を確認する場合:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/chatgpt-handoff/scripts/generate_handoff.py" \
  --repo /path/to/repository \
  --dry-run
```

出力先を分ける場合:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/chatgpt-handoff/scripts/generate_handoff.py" \
  --output-root /path/to/output
```

## Agent sessionの情報を渡す

現在のsessionで確認できた事実だけをREADMEへ渡せます。

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/chatgpt-handoff/scripts/generate_handoff.py" \
  --purpose "今回レビューしてほしい変更" \
  --tests "pytest: PASS" \
  --blockers "明示されたblockerなし"
```

値を渡さなかった項目には、推測を避けるため既定文が入ります。

## 安全性について

このSkillは、次の処理を重ねて秘密情報の混入を防ぎます。

1. `.env`、秘密鍵、credential、DB dumpなどのパスを除外する
2. binary、画像、動画、PDF、ZIP、依存関係、build成果物を除外する
3. diffとコピー対象テキストから既知のtoken・password形式をマスキングする
4. ZIP作成前に生成済みテキストを再検査する

secret検出は既知パターンに基づく防御です。未知の形式まで完全に検出できる保証はありません。外部へ共有する前に、`README_FOR_CHATGPT.md`と`git-diff-all.txt`を確認してください。

ZIPの既定上限は50 MiBです。上限を超えた場合は既存の`latest`を置き換えずに停止します。

## archive

既存の`latest`は、次の場所へ退避します。

```text
~/ChatGPT-Handoff/<project>/archive/YYYYMMDD-HHMMSS/
```

保存するのは最新10世代です。

## テスト

```bash
python3 -m unittest discover -s tests -v
```

テストは一時Gitリポジトリを作り、本体リポジトリを変更せずにsecret除外、差分収集、archive、サイズ制限などを確認します。

## License

[MIT License](LICENSE)
