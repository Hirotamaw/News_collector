"""
暗号資産ニュース 自動取得・要約スクリプト v13

企業名抽出の大幅改善:
  1. 単語境界マッチング導入
     - "IREN" が "Infura" にマッチしない
     - "ADA" が "Canada/Cardano" にマッチしない
     - 英数字は前後に単語境界(\b)を要求
     - 日本語キーワードは前後の文脈を考慮
  2. タイトル優先ロジック強化
     - タイトルに登場した企業は必ずmain_entitiesに
     - 本文のみの企業はrelated_entitiesに
  3. 辞書大幅拡充
     - ThorChain, BitTrade, MARA, IREN等の欠落企業を追加
     - 誤マッチしやすいキーワード(ADA, ARK等)を境界付きに変更
"""

import html
import json
import os
import re
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

# ── 定数 ─────────────────────────────────────────────────────────────────
JST           = timezone(timedelta(hours=9))
DATA_FILE     = Path(__file__).parent.parent / "docs" / "data" / "news.json"
ENTITIES_FILE = Path(__file__).parent.parent / "docs" / "data" / "entities.json"
MAX_RETRIES   = 3
RETRY_DELAY   = 5
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

# ── 取得対象ソース ────────────────────────────────────────────────────────
SOURCES = [
    {"name": "NADA NEWS",        "top_url": "https://www.nadanews.com/",  "rss_url": "https://www.nadanews.com/feed/",      "color": "#0f6e56"},
    {"name": "CoinPost",         "top_url": "https://coinpost.jp/",       "rss_url": "https://coinpost.jp/?feed=rss2",      "color": "#1d4ed8"},
    {"name": "あたらしい経済",   "top_url": "https://www.neweconomy.jp/", "rss_url": "https://www.neweconomy.jp/feed/",     "color": "#7c3aed"},
    {"name": "CoinTelegraph JP", "top_url": "https://cointelegraph.jp/",  "rss_url": "https://cointelegraph.jp/rss",        "color": "#b45309"},
]

# ── カテゴリ定義 ──────────────────────────────────────────────────────────
CATEGORIES = [
    "Blockchain", "DeFi", "障害・攻撃", "分析・レポート",
    "Stablecoin", "NFT", "Tokenized Deposit", "Security Token",
    "暗号資産ETF", "ビジネス", "マーケット", "規制・法律", "イベント・人事",
]

KEYWORD_RULES = [
    ("障害・攻撃",       ["ハック","ハッキング","流出","被害","エクスプロイト","exploit","攻撃","詐欺","フィッシング","障害","盗難","不正アクセス","凍結","drain","breach","hack","stolen","scam","vulnerability","脆弱性"]),
    ("Blockchain",       ["EIP","BIP","ハードフォーク","ソフトフォーク","アップグレード","プロトコル更新","コンセンサス","バリデータ","merge","upgrade","fork","consensus","validator","ロールアップ","rollup"]),
    ("DeFi",             ["DeFi","defi","分散型金融","Uniswap","Aave","Compound","Curve","MakerDAO","流動性プール","AMM","DEX","レンディング","TVL"]),
    ("Stablecoin",       ["ステーブルコイン","stablecoin","USDT","USDC","JPYC","CBDC","デジタル円","安定通貨","Tether","EURC","PYUSD","RLUSD"]),
    ("NFT",              ["NFT","nft","デジタルアート","メタバース","OpenSea","Blur","コレクション"]),
    ("Tokenized Deposit",["トークン化預金","預金トークン","Tokenized Deposit","tokenized deposit","デジタル預金"]),
    ("Security Token",   ["セキュリティトークン","Security Token","STO","RWA","トークン化株式","トークン化国債","トークン化MMF","現実資産","株式トークン","国債トークン"]),
    ("暗号資産ETF",      ["ETF","ビットコインETF","イーサリアムETF","上場投資信託","IBIT","FBTC","GBTC","現物ETF","資金流入"]),
    ("分析・レポート",   ["IMF","BIS","世界銀行","金融庁","FSB","IOSCO","FRB","ECB","レポート","報告書","声明","統計","オンチェーン分析"]),
    ("規制・法律",       ["規制","法案","法律","ライセンス","当局","SEC","CFTC","財務省","訴訟","逮捕","摘発","禁止","regulation","compliance","AML","KYC"]),
    ("マーケット",       ["価格","相場","急騰","急落","上昇","下落","高値","安値","ビットコイン価格","ATH","最高値","bull","bear"]),
    ("イベント・人事",   ["カンファレンス","イベント","展示会","ハッカソン","人事","CEO","退任","就任","conference","summit"]),
    ("ビジネス",         ["提携","資金調達","シリーズ","ラウンド","買収","上場","サービス開始","ローンチ","パートナー","取引所","ウォレット","決済","融資"]),
]


def keyword_classify(title: str, description: str) -> str:
    text = (title + " " + description).lower()
    for category, keywords in KEYWORD_RULES:
        for kw in keywords:
            if kw.lower() in text:
                return category
    return "ビジネス"


# ── 企業名辞書（v13: 単語境界付き・辞書拡充）────────────────────────────
#
# キーワードの形式:
#   通常文字列    → 部分一致（日本語企業名はこちら）
#   r"\bXXX\b"   → 単語境界付き（英数字で誤マッチしやすいものはこちら）
#
# 重要度:
#   1 = 主要企業候補（タイトルに出れば必ずmain）
#   2 = 関連企業（relatedのみ）

ENTITY_DICT = [
    # ── 取引所・ブローカー ──
    ("Coinbase",             [r"\bCoinbase\b","コインベース"],                                      1),
    ("Binance",              [r"\bBinance\b","バイナンス"],                                         1),
    ("Kraken",               [r"\bKraken\b","クラーケン"],                                          1),
    ("OKX",                  [r"\bOKX\b",r"\bOKEx\b"],                                             1),
    ("Bybit",                [r"\bBybit\b","バイビット"],                                           1),
    ("bitFlyer",             [r"\bbitFlyer\b","ビットフライヤー"],                                  1),
    ("GMOコイン",            ["GMOコイン",r"\bGMO Coin\b"],                                         1),
    ("SBI VC Trade",         [r"\bSBI VC\b","SBIVCトレード"],                                       1),
    ("楽天ウォレット",       ["楽天ウォレット",r"\bRakuten Wallet\b"],                              1),
    ("マネックスグループ",   ["マネックス",r"\bMonex\b"],                                           1),
    ("bitbank",              [r"\bbitbank\b","ビットバンク"],                                       1),
    ("BitTrade",             [r"\bBitTrade\b","ビットトレード"],                                    1),
    ("HTX",                  [r"\bHTX\b",r"\bHuobi\b","フォビ"],                                   1),
    ("KuCoin",               [r"\bKuCoin\b","クーコイン"],                                         1),
    ("Upbit",                [r"\bUpbit\b","アップビット"],                                        1),
    ("Bitstamp",             [r"\bBitstamp\b","ビットスタンプ"],                                   2),
    ("Gate.io",              [r"\bGate\.io\b","ゲートアイオー"],                                   2),
    ("MEXC",                 [r"\bMEXC\b"],                                                        2),

    # ── マイニング・インフラ企業 ──
    ("MARA Holdings",        [r"\bMARA\b","MARA Holdings","マラ"],                                 1),
    ("Riot Platforms",       [r"\bRiot\b","Riot Platforms","ライオット"],                          1),
    ("CleanSpark",           [r"\bCleanSpark\b"],                                                   1),
    ("IREN",                 [r"\bIREN\b"],                                                        1),  # 単語境界でInfuraと区別
    ("Core Scientific",      [r"\bCore Scientific\b","コアサイエンティフィック"],                  1),

    # ── ブロックチェーン・プロトコル ──
    ("Ethereum Foundation",  [r"\bEthereum Foundation\b","イーサリアム財団"],                      1),
    ("Solana",               [r"\bSolana\b","ソラナ"],                                             1),
    ("Polygon",              [r"\bPolygon\b","ポリゴン",r"\bMATIC\b"],                             1),
    ("Ripple",               [r"\bRipple\b","リップル",r"\bXRP\b"],                                1),
    ("Avalanche",            [r"\bAvalanche\b","アバランチ",r"\bAVAX\b"],                          1),
    ("Cardano",              [r"\bCardano\b","カルダノ"],                                          2),  # ADAは除外（誤マッチ多発）
    ("Polkadot",             [r"\bPolkadot\b","ポルカドット",r"\bDOT\b"],                          2),
    ("Cosmos",               [r"\bCosmos\b","コスモス",r"\bATOM\b"],                              2),
    ("Near Protocol",        [r"\bNEAR\b","Near Protocol"],                                       2),
    ("Sui",                  [r"\bSui\b","スイ"],                                                  2),
    ("Aptos",                [r"\bAptos\b","アプトス"],                                            2),
    ("Toncoin",              [r"\bTON\b","Toncoin","トンコイン"],                                  2),
    ("Arbitrum",             [r"\bArbitrum\b","アービトラム"],                                    2),
    ("Optimism",             [r"\bOptimism\b","オプティミズム"],                                  2),
    ("Base",                 [r"\bBase\b（Coinbase）"],                                            2),  # "Base"単体は誤マッチするため限定

    # ── DeFiプロトコル ──
    ("Uniswap",              [r"\bUniswap\b","ユニスワップ"],                                     1),
    ("Aave",                 [r"\bAave\b","アーベ"],                                               1),
    ("MakerDAO",             [r"\bMakerDAO\b",r"\bMaker\b","Sky Protocol"],                       1),
    ("Curve Finance",        [r"\bCurve\b","カーブ"],                                             2),
    ("Lido",                 [r"\bLido\b","リド"],                                                 1),
    ("Hyperliquid",          [r"\bHyperliquid\b","ハイパーリキッド"],                             1),
    ("dYdX",                 [r"\bdYdX\b"],                                                        2),
    ("Jupiter",              [r"\bJupiter\b（DeFi）"],                                            2),
    ("ThorChain",            [r"\bThorChain\b",r"\bTHOR\b","ソアチェーン","トールチェーン"],       1),
    ("EigenLayer",           [r"\bEigenLayer\b","アイゲンレイヤー"],                              2),

    # ── ステーブルコイン発行体 ──
    ("Tether",               [r"\bTether\b","テザー",r"\bUSDT\b"],                                1),
    ("Circle",               [r"\bCircle\b","サークル",r"\bUSDC\b"],                              1),
    ("JPYC",                 [r"\bJPYC\b"],                                                        1),
    ("Paxos",                [r"\bPaxos\b","パクソス"],                                           2),

    # ── ETF・資産運用 ──
    ("BlackRock",            [r"\bBlackRock\b","ブラックロック"],                                 1),
    ("Fidelity",             [r"\bFidelity\b","フィデリティ"],                                    1),
    ("Grayscale",            [r"\bGrayscale\b","グレースケール",r"\bGBTC\b"],                     1),
    ("Franklin Templeton",   [r"\bFranklin Templeton\b","フランクリン・テンプルトン"],            2),
    ("VanEck",               [r"\bVanEck\b","バンエック"],                                        2),
    ("ARK Invest",           [r"\bARK Invest\b","ARKインベスト"],                                 2),  # "\bARK\b"は除外（誤マッチ）

    # ── 銀行・金融機関 ──
    ("JPMorgan",             [r"\bJPMorgan\b","JPモルガン","J.P.Morgan"],                         1),
    ("Goldman Sachs",        ["Goldman Sachs","ゴールドマン・サックス"],                          1),
    ("Morgan Stanley",       ["Morgan Stanley","モルガン・スタンレー"],                           1),
    ("Visa",                 [r"\bVisa\b","ビザ"],                                                 2),
    ("Mastercard",           [r"\bMastercard\b","マスターカード"],                                2),
    ("PayPal",               [r"\bPayPal\b","ペイパル"],                                          1),
    ("三菱UFJ銀行",          ["三菱UFJ","MUFG","三菱UFJ銀行"],                                   1),
    ("みずほ銀行",           ["みずほ銀行","Mizuho"],                                             1),
    ("三井住友銀行",         ["三井住友","SMBC"],                                                  1),
    ("SBI Holdings",         ["SBIホールディングス","SBI Holdings","SBIグループ"],                1),
    ("野村ホールディングス", ["野村ホールディングス","野村証券",r"\bNomura\b"],                   2),
    ("HSBC",                 [r"\bHSBC\b"],                                                        2),
    ("Standard Chartered",   ["Standard Chartered","スタンダードチャータード"],                   2),
    ("Deutsche Bank",        ["Deutsche Bank","ドイツ銀行"],                                      2),
    ("BNY Mellon",           ["BNY Mellon","バンクオブニューヨーク"],                             2),
    ("State Street",         ["State Street","ステートストリート"],                               2),

    # ── テック・事業会社 ──
    ("MicroStrategy",        [r"\bMicroStrategy\b", r"\bStrategy\b", "マイクロストラテジー"],               1),
    ("Tesla",                [r"\bTesla\b","テスラ"],                                             1),
    ("Microsoft",            [r"\bMicrosoft\b","マイクロソフト"],                                 2),
    ("Google",               [r"\bGoogle\b",r"\bAlphabet\b","グーグル"],                          2),
    ("Amazon",               [r"\bAmazon\b","アマゾン",r"\bAWS\b"],                               2),
    ("Meta",                 [r"\bMeta\b",r"\bFacebook\b","メタ"],                                2),
    ("Apple",                [r"\bApple\b","アップル"],                                           2),
    ("NVIDIA",               [r"\bNVIDIA\b","エヌビディア"],                                      2),

    # ── 規制当局・政府 ──
    ("SEC",                  [r"\bSEC\b","証券取引委員会"],                                       1),
    ("CFTC",                 [r"\bCFTC\b","商品先物取引委員会"],                                  1),
    ("金融庁",               ["金融庁",r"\bFSA\b"],                                               1),
    ("財務省",               ["財務省（日本）"],                                                  1),
    ("米財務省",             ["米財務省","U.S. Treasury","財務省（米国）"],                       1),
    ("FRB",                  [r"\bFRB\b","Federal Reserve","連邦準備"],                           1),
    ("ECB",                  [r"\bECB\b","欧州中央銀行"],                                         1),
    ("IMF",                  [r"\bIMF\b","国際通貨基金"],                                         1),
    ("BIS",                  [r"\bBIS\b","国際決済銀行"],                                         1),
    ("世界銀行",             ["世界銀行","World Bank"],                                           1),
    ("FSB",                  [r"\bFSB\b","金融安定理事会"],                                       1),
    ("ホワイトハウス",       ["White House","ホワイトハウス"],                                    1),

    # ── VC・投資ファンド ──
    ("a16z",                 [r"\ba16z\b","Andreessen Horowitz"],                                 2),
    ("Paradigm",             [r"\bParadigm\b","パラダイム"],                                      2),
    ("Pantera Capital",      ["Pantera Capital","パンテラ"],                                      2),
    ("Galaxy Digital",       ["Galaxy Digital","ギャラクシーデジタル"],                           2),

    # ── インフラ・ツール ──
    ("Fireblocks",           [r"\bFireblocks\b","ファイアブロックス"],                            2),
    ("Chainalysis",          [r"\bChainalysis\b","チェイナリシス"],                               2),
    ("Ledger",               [r"\bLedger\b","レジャー"],                                          2),
    ("MetaMask",             [r"\bMetaMask\b","メタマスク"],                                      2),
    ("Infura",               [r"\bInfura\b","インフラ（Infura）"],                                2),  # 単語境界でIRENと区別

    # ── NFT・ゲーム ──
    ("OpenSea",              [r"\bOpenSea\b","オープンシー"],                                     1),
    ("Blur",                 [r"\bBlur\b","ブラー"],                                              2),
    ("Animoca Brands",       ["Animoca","アニモカ"],                                              2),

    # ── その他注目企業 ──
    ("Ripple Labs",          ["Ripple Labs"],                                                     1),
    ("ConsenSys",            [r"\bConsenSys\b","コンセンシス"],                                   2),
    ("CoinDesk",             [r"\bCoinDesk\b","コインデスク"],                                   2),
    ("Bakkt",                [r"\bBakkt\b","バックト"],                                           2),
    ("Gemini Exchange",      [r"\bGemini\b（取引所）"],                                           1),
    ("Kraken",               [r"\bKraken\b"],                                                     1),
]


def _make_pattern(keyword: str) -> re.Pattern:
    """
    キーワードからマッチパターンを生成する。
    r"\b...\b" 形式の場合はそのまま正規表現として使用。
    通常文字列の場合はescape後に部分一致。
    """
    # r"\b...\b"形式かチェック
    if keyword.startswith(r"\b") or keyword.startswith("\\b"):
        return re.compile(keyword, re.IGNORECASE)
    else:
        return re.compile(re.escape(keyword), re.IGNORECASE)


def extract_entities_by_keyword(title: str, description: str) -> tuple[list, list]:
    """
    辞書マッチングで企業名を抽出。
    - タイトルにマッチした企業 → main_entities 優先
    - 本文のみにマッチした企業 → related_entities
    """
    # ── タイトルでマッチした企業を収集 ──
    title_matched: list[tuple[str, int]] = []  # (name, importance)
    for display_name, keywords, importance in ENTITY_DICT:
        for kw in keywords:
            pat = _make_pattern(kw)
            if pat.search(title):
                title_matched.append((display_name, importance))
                break

    # ── 本文全体（タイトル+description）でマッチした企業を収集 ──
    full_text = title + " " + description
    body_matched: list[tuple[str, int, int]] = []  # (name, importance, count)
    for display_name, keywords, importance in ENTITY_DICT:
        count = sum(len(_make_pattern(kw).findall(full_text)) for kw in keywords)
        if count > 0:
            body_matched.append((display_name, importance, count))

    # ── main_entities の決定 ──
    # 優先度1: タイトルにマッチした企業（重要度順）
    main_from_title = [n for n, imp in title_matched if imp == 1]
    # 優先度2: タイトルにマッチした重要度2の企業（他にmainがなければ）
    main_from_title_2 = [n for n, imp in title_matched if imp == 2]
    # 優先度3: 本文で2回以上登場した重要度1の企業
    main_from_body = [n for n, imp, cnt in body_matched if imp == 1 and cnt >= 2 and n not in main_from_title]

    # mainを確定（最大3件）
    main_candidates = list(dict.fromkeys(main_from_title + main_from_body))
    if not main_candidates:
        main_candidates = list(dict.fromkeys(main_from_title_2))[:1]
    if not main_candidates and body_matched:
        top = sorted(body_matched, key=lambda x: (-x[1], -x[2]))
        main_candidates = [top[0][0]]

    main_set = set(main_candidates[:3])

    # ── related_entities の決定 ──
    all_found = list(dict.fromkeys([n for n, _, _ in body_matched]))
    related = [n for n in all_found if n not in main_set]

    return list(dict.fromkeys(main_candidates[:3])), list(dict.fromkeys(related[:10]))


def normalize_category(raw: str) -> str:
    raw = raw.strip()
    if raw in CATEGORIES:
        return raw
    rl = raw.lower()
    for cat in CATEGORIES:
        if cat.lower() == rl or cat.lower() in rl or rl in cat.lower():
            return cat
    return ""


# ── Gemini API 呼び出し ───────────────────────────────────────────────────
GEMINI_PROMPT = """\
以下の暗号資産ニュース記事を分析し、JSONのみを返してください（コードブロック・説明文は不要）。

タイトル: {title}
本文: {description}

カテゴリ選択肢（1つ選びcategoryにそのまま入れること）:
- Blockchain / DeFi / 障害・攻撃 / 分析・レポート / Stablecoin / NFT
- Tokenized Deposit / Security Token / 暗号資産ETF / ビジネス
- マーケット / 規制・法律 / イベント・人事

出力JSON:
{{"summary": "200〜400字で記事の要約。何が起きたか・誰が主体か・数値や固有名詞を含め具体的に。省略記号(…)禁止。完結した文章で。", "category": "カテゴリ名をそのまま", "main_entities": ["タイトルで主役として取り上げられている企業・団体名（1〜3件、個人名除外）"], "related_entities": ["本文中に登場するその他の企業・団体・プロトコル名（最大8件、個人名除外、main_entitiesと重複なし）"]}}

重要: main_entitiesには必ずタイトルに登場する企業・団体を優先して入れること。"""


def call_gemini(api_key: str, title: str, description: str) -> dict | None:
    content = description if len(description) >= 30 else title
    prompt  = GEMINI_PROMPT.format(title=title, description=content)
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1024, "topP": 0.8}
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}

    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(GEMINI_ENDPOINT, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            raw  = result["candidates"][0]["content"]["parts"][0]["text"].strip()
            raw  = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.IGNORECASE)
            raw  = re.sub(r"\n?```\s*$", "", raw).strip()
            data = json.loads(raw)

            summary = re.sub(r"…+|\.{3,}", "", (data.get("summary") or "")).strip()
            if len(summary) < 50:
                return None

            cat      = normalize_category(str(data.get("category", ""))) or keyword_classify(title, content)
            main_e   = [str(e).strip() for e in (data.get("main_entities") or [])    if str(e).strip()]
            related_e= [str(e).strip() for e in (data.get("related_entities") or []) if str(e).strip()]

            # Gemini結果でもタイトル企業を補完
            kw_main, _ = extract_entities_by_keyword(title, "")
            for name in kw_main:
                if name not in main_e:
                    main_e.insert(0, name)

            related_e = [e for e in related_e if e not in set(main_e[:3])]
            return {"summary": summary, "category": cat,
                    "main_entities": main_e[:3], "related_entities": related_e[:8]}

        except urllib.error.HTTPError as e:
            print(f"    Gemini HTTP {e.code} (attempt {attempt+1})")
            if e.code in (400, 403): return None
            time.sleep(RETRY_DELAY * (attempt + 1))
        except Exception:
            print(f"    Gemini エラー (attempt {attempt+1})")
            if attempt == MAX_RETRIES - 1: return None
            time.sleep(RETRY_DELAY)
    return None


def analyze_article(api_key: str | None, title: str, description: str) -> dict:
    content = description if len(description) >= 30 else title
    if api_key:
        result = call_gemini(api_key, title, description)
        if result:
            print(f"       [Gemini] {result['category']} / {len(result['summary'])}字 / 主体:{result['main_entities']}")
            return result
        print("       [Gemini→Fallback]")
    main_e, related_e = extract_entities_by_keyword(title, description)
    cat = keyword_classify(title, description)
    print(f"       [Keyword] {cat} / 主体:{main_e}")
    return {"summary": content[:400], "category": cat,
            "main_entities": main_e, "related_entities": related_e}


# ── 企業名マスタDB ────────────────────────────────────────────────────────
def load_entities_db() -> dict:
    if ENTITIES_FILE.exists():
        with open(ENTITIES_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"entities": {}, "last_updated": None, "total_count": 0}


def update_entities_db(entities_db: dict, articles: list[dict], now_jst: datetime) -> dict:
    ents = entities_db.get("entities", {})
    for article in articles:
        pub, title, link = article.get("pub_date",""), article.get("title",""), article.get("link","")
        pairs = [(n,True) for n in (article.get("main_entities") or [])] + \
                [(n,False) for n in (article.get("related_entities") or [])]
        for name, is_main in pairs:
            if name not in ents:
                ents[name] = {"name":name,"article_count":0,"as_main_count":0,
                              "as_related_count":0,"recent_articles":[],"first_seen":pub,"last_seen":pub}
            e = ents[name]
            if link not in [a["link"] for a in e["recent_articles"]]:
                e["article_count"] += 1
                if is_main: e["as_main_count"] += 1
                else:       e["as_related_count"] += 1
                e["last_seen"] = max(e["last_seen"], pub) if e["last_seen"] else pub
                e["recent_articles"].insert(0, {"title":title,"link":link,"pub_date":pub})
                e["recent_articles"] = e["recent_articles"][:5]
    entities_db.update({"entities":ents,"last_updated":now_jst.isoformat(),"total_count":len(ents)})
    return entities_db


def save_entities_db(db: dict) -> None:
    ENTITIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ENTITIES_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


# ── テキストクリーニング ──────────────────────────────────────────────────
def clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    text = re.sub(r"\[…\]|\[&#8230;\]|\[&hellip;\]|\[\.{3}\]|…+|\.{3,}", "", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# ── RSS 取得 ──────────────────────────────────────────────────────────────
def fetch_rss(source: dict) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CryptoNewsBot/13.0)"}
    resp = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(source["rss_url"], headers=headers, timeout=30)
            resp.raise_for_status()
            break
        except requests.RequestException:
            if attempt == MAX_RETRIES - 1:
                print(f"  ✗ [{source['name']}] RSS取得失敗")
                return []
            time.sleep(RETRY_DELAY)
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return []

    items = []
    for item in root.findall(".//item"):
        title        = clean_text(item.findtext("title") or "")
        link         = (item.findtext("link") or "").strip()
        pub_date_str = (item.findtext("pubDate") or "").strip()
        description  = clean_text(item.findtext("description") or "")[:1200]
        cats         = [el.text.strip() for el in item.findall("category") if el.text]
        pub_date_jst = None
        if pub_date_str:
            try: pub_date_jst = parsedate_to_datetime(pub_date_str).astimezone(JST)
            except: pass
        if not title or not link: continue
        items.append({
            "title":title,"link":link,
            "pub_date":     pub_date_jst.isoformat() if pub_date_jst else None,
            "pub_date_utc": pub_date_jst.astimezone(timezone.utc).isoformat() if pub_date_jst else None,
            "description":description,"category_raw":cats[0] if cats else "",
            "source_name":source["name"],"source_url":source["top_url"],"source_color":source["color"],
        })
    return items


def filter_recent(items: list[dict], hours: int = 24) -> list[dict]:
    now = datetime.now(JST)
    cutoff = now - timedelta(hours=hours)
    return [it for it in items if it["pub_date"] and datetime.fromisoformat(it["pub_date"]) >= cutoff]


def load_db() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f: return json.load(f)
    return {"articles":[],"last_updated":None,"total_count":0,"sources":[s["name"] for s in SOURCES]}


def save_db(db: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def merge_articles(db: dict, new_articles: list[dict]) -> int:
    existing = {a["link"] for a in db["articles"]}
    added = 0
    for art in new_articles:
        if art["link"] not in existing:
            db["articles"].append(art)
            existing.add(art["link"])
            added += 1
    db["articles"].sort(key=lambda a: a.get("pub_date") or "1970-01-01", reverse=True)
    return added


# ── メイン ────────────────────────────────────────────────────────────────
def main():
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    print("✓ Gemini API 有効" if gemini_api_key else "⚠ キーなし → キーワードフォールバック")

    now_jst = datetime.now(JST)
    print(f"=== 取得開始 ({now_jst.strftime('%Y-%m-%d %H:%M JST')}) ===\n")

    print("[1/5] RSS取得中...")
    all_items: list[dict] = []
    for source in SOURCES:
        items = fetch_rss(source)
        print(f"  ✓ {source['name']}: {len(items)} 件")
        all_items.extend(items)
    print(f"  合計: {len(all_items)} 件\n")

    print("[2/5] 24時間フィルタ...")
    recent = filter_recent(all_items, hours=24)
    print(f"  対象: {len(recent)} 件\n")

    print("[3/5] DB確認...")
    db             = load_db()
    existing_links = {a["link"] for a in db["articles"]}
    new_items      = [it for it in recent if it["link"] not in existing_links]
    print(f"  新規: {len(new_items)} 件 / スキップ: {len(recent)-len(new_items)} 件\n")

    if new_items:
        print(f"[4/5] 記事分析中（{len(new_items)} 件）...")
        for i, item in enumerate(new_items, 1):
            print(f"  [{i:>2}/{len(new_items)}] {item['title'][:50]}...")
            result = analyze_article(gemini_api_key, item["title"], item["description"])
            item.update({"summary":result["summary"],"category":result["category"],
                         "main_entities":result["main_entities"],"related_entities":result["related_entities"],
                         "manually_edited":False,"fetched_at":now_jst.isoformat()})
            time.sleep(0.5)
        print()

    added = merge_articles(db, new_items)
    db.update({"last_updated":now_jst.isoformat(),"total_count":len(db["articles"]),
               "sources":[s["name"] for s in SOURCES]})
    save_db(db)

    print("[5/5] 企業名マスタ更新...")
    entities_db = update_entities_db(load_entities_db(), db["articles"], now_jst)
    save_entities_db(entities_db)
    print(f"  企業名マスタ: {entities_db['total_count']} 社\n")
    print(f"=== 完了: 新規{added}件 / 累計{db['total_count']}件 ===")


if __name__ == "__main__":
    main()
