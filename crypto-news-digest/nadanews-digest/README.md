# 暗号資産ニュース 自動ダイジェスト

暗号資産・Web3の主要4メディアからニュースを自動収集し、AI要約してスマホ対応ページで閲覧できる仕組みです。

## 対応メディア

| メディア | URL |
|----------|-----|
| NADA NEWS | https://www.nadanews.com/ |
| CoinPost | https://coinpost.jp/ |
| あたらしい経済 | https://www.neweconomy.jp/ |
| CoinTelegraph JP | https://cointelegraph.jp/ |

## 主な機能

| 機能 | 内容 |
|------|------|
| 自動取得 | GitHub Actions で **毎朝9:00 JST** に全メディアのRSSを取得 |
| 期間フィルタ | 過去24時間 / 今日 / 今週 / すべて でタブ切替 |
| ソース絞り込み | メディアごとにチップで絞り込み可能 |
| AI要約 | Claude API で日本語要約 + カテゴリ自動判定 |
| タグ表示 | ソース名・カテゴリを各カードに表示 |
| データ蓄積 | `data/news.json` に重複排除して蓄積 |
| スマホ対応 | GitHub Pages でレスポンシブ配信・ダークモード対応 |
| 手動更新 | ページ右上の「更新」ボタンで即時リフレッシュ |

---

## セットアップ手順（所要時間：約15分）

### Step 1 — リポジトリを作成

1. GitHub にログインし、右上の **「+」→「New repository」** をクリック
2. Repository name: `crypto-news-digest`（任意）
3. **Public** を選択（GitHub Pages の無料利用に必要）
4. **「Create repository」** をクリック

### Step 2 — ファイルをアップロード

以下のフォルダ構成のままリポジトリに push します。

```
crypto-news-digest/
├── .github/
│   └── workflows/
│       └── fetch-news.yml     ← GitHub Actions 定義
├── data/
│   └── news.json              ← ニュースDB（自動更新）
├── docs/
│   └── index.html             ← スマホ対応 Web ビューワー
├── scripts/
│   └── fetch_news.py          ← RSS取得・要約スクリプト
└── README.md
```

**Git を使う場合:**
```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/crypto-news-digest.git
git push -u origin main
```

### Step 3 — Anthropic API キーを設定

1. [Anthropic Console](https://console.anthropic.com/) で API キーを取得
2. GitHub リポジトリの **「Settings」→「Secrets and variables」→「Actions」** を開く
3. **「New repository secret」** をクリック
4. Name: `ANTHROPIC_API_KEY`、Value: 取得したAPIキーを貼り付けて **「Add secret」**

### Step 4 — GitHub Pages を有効化

1. リポジトリの **「Settings」→「Pages」** を開く
2. Source: **「Deploy from a branch」** を選択
3. Branch: `main` / Folder: **`/docs`** を選択
4. **「Save」** をクリック
5. 数分後、`https://YOUR_USERNAME.github.io/crypto-news-digest/` が公開されます

### Step 5 — HTMLのリポジトリURLを修正

`docs/index.html` の以下の行を自分のURLに変更してください：

```javascript
const REPO_URL = 'https://github.com/YOUR_USERNAME/crypto-news-digest';
```

### Step 6 — 初回テスト実行

1. リポジトリの **「Actions」** タブを開く
2. 左側の **「Fetch Crypto News Daily Digest」** を選択
3. **「Run workflow」→「Run workflow」** をクリック
4. 約2〜3分後、`data/news.json` が更新されることを確認
5. GitHub Pages の URL にアクセスして記事が表示されれば完成

---

## データ構造（data/news.json）

```json
{
  "articles": [
    {
      "title":        "記事タイトル",
      "link":         "https://記事のURL",
      "pub_date":     "2026-04-05T09:00:00+09:00",
      "pub_date_utc": "2026-04-05T00:00:00+00:00",
      "description":  "リード文（最大600字）",
      "category_raw": "元カテゴリ文字列（RSSから）",
      "category":     "AI判定カテゴリ（日本語）",
      "source_name":  "NADA NEWS",
      "source_url":   "https://www.nadanews.com/",
      "source_color": "#0f6e56",
      "summary":      "AI生成の日本語要約（100〜150字）",
      "fetched_at":   "2026-04-05T09:05:00+09:00"
    }
  ],
  "last_updated": "2026-04-05T09:05:00+09:00",
  "total_count": 128,
  "sources": ["NADA NEWS", "CoinPost", "あたらしい経済", "CoinTelegraph JP"]
}
```

## カテゴリ一覧

AI が自動で以下のいずれかに分類します：

`ビットコイン` / `イーサリアム` / `アルトコイン` / `マーケット` / `DeFi` / `NFT` / `Web3` / `ビジネス` / `テクノロジー` / `規制・法律` / `オピニオン` / `分析` / `イベント` / `プレスリリース` / `学習` / `その他`

---

## カスタマイズ

### 実行時刻の変更（.github/workflows/fetch-news.yml）

```yaml
- cron: '0 0 * * *'  # 00:00 UTC = 09:00 JST
# → 例: 12:00 JST に変更する場合
- cron: '0 3 * * *'  # 03:00 UTC = 12:00 JST
```

### 取得期間の変更（scripts/fetch_news.py）

```python
recent_items = filter_recent(all_items, hours=24)  # 24時間 → 任意の時間数に変更
```

### モデルの変更（scripts/fetch_news.py）

```python
model="claude-haiku-4-5-20251001",   # 高速・低コスト（デフォルト）
model="claude-opus-4-5",             # 高品質
```

---

## コスト目安

- **GitHub Actions**: 無料枠（月2,000分）で十分に運用可能
- **Anthropic API**: 1日あたり20〜50記事として $0.01〜0.05 程度（Haiku使用時）
- **GitHub Pages**: 無料

## 注意事項

- 各メディアの利用規約に従って適切にご利用ください
- API キーは絶対にコードに直接記載せず、GitHub Secrets で管理してください
