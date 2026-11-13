# akiyama-method（クロノス v2）

下落後の反発候補を抽出し、AIが企業・市場の文脈から重大な追加下落リスクを精査する研究・仮想運用システム。
実売買・自動発注は行わない。方針と全体設計は [docs/DESIGN.md](docs/DESIGN.md)。

## 現在できること（P1：データ基盤）

| コマンド | 内容 |
|---|---|
| `python -m akiyama sync-bulk` | J-Quants bulk（月次CSV＋日次live）のうち未取得分を取得。過去分は版付きで保持し上書きしない |
| `python -m akiyama sync-td` | TDnet開示インデックス（5年分bulk＋直近日の`/td/list`） |
| `python -m akiyama normalize` | 生データ→時点付きParquet（`available_at`付き） |
| `python -m akiyama fetch-docs --codes 8174,3388 --from 2024-01-01` | 指定銘柄の開示PDFを取得 |
| `python -m akiyama daily --since 2026-11` | 引け後の定例更新（上3つをまとめて実行） |
| `python -m akiyama status` | 取得済みファイルと各表の期間 |
| `python -m akiyama universe --from 2017-01-04 --to 2026-11-13` | 各営業日の適格銘柄（時点整合、`data/derived/universe/`） |
| `python -m akiyama roster --name forward_120_20261113 --asof 2026-11-13 --n 120 --seed 20261113` | 適格銘柄から業種×規模で層化した固定乱数名簿を `rosters/` に凍結（上書き不可） |

| `python -m akiyama pipeline --from 2016-11-01` | 適格判定→価格条件→Chronos-2予測→C0判定を一括実行（予測はキャッシュ・再開可能） |

## 候補抽出 chronos_v2（P3）

定義は `config/strategy_chronos_v2.yaml` に固定（結果を見る前に決定。変更は別版で比較）。

| 段階 | 内容 |
|---|---|
| 価格条件 | 適格、かつ60営業日騰落率 ≤ −10%、かつ直近20営業日も下落（分割調整済み終値） |
| Chronos-2 | `amazon/chronos-2`（revision固定）、直近512営業日の調整後終値から20営業日先を予測 |
| C0 | 予測リターン（中央値）> max(0.2%, その銘柄の過去52回の週末予測の75%点)。参照は判定日より前のみ、最低20回 |
| 出力 | `data/derived/candidates/`：価格条件成立日すべてに `c0`・`c0_status`・`period`（dev/eval）・`chronos_period`・`is_dev_code` |

Chronos-2は2025-10公開（現revisionの更新は2026-06）。それ以前の期間ではモデルが学習時に見た可能性があるため、Chronosの追加価値は `chronos_period` 別に評価する。

## 適格条件（P2）

判定日Dの18:00（JST）時点で利用可能なデータのみ使用。

| 条件 | 内容 |
|---|---|
| 上場 | 当日の上場銘柄情報にあり、国内普通株（ProdCat 011）、TOKYO PRO・その他市場を除く |
| 時価総額 | 100億円以上（J-QuantsのMktCap。欠損時は直近開示の発行済株式数−自己株式×終値、`mktcap_basis`に記録） |
| 売買代金 | 直近60営業日の中央値1億円以上（60日分必要） |
| 100株金額 | 終値×100 ≤ 100万円。2018-10-01より前は単元株数が取れないため100株仮定（`lot_assumed`） |

名簿は旧研究の8銘柄（開発用）を除外し、選定に将来株価・過去成績を使わない。後に上場廃止となった企業も名簿から外さない。

## Windowsでの初回セットアップ

```powershell
git clone https://github.com/ikarahagisu/akiyama-method
cd akiyama-method
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env   # .env に JQUANTS_API_KEY を記入（gitには入らない）
python -m pytest -q
```

### 初回の全期間ダウンロード（なるべく早く）

Standardプランの提供期間は**毎日1日ずつ古い側から取れなくなる**（株価・指数は約10年、TDnetは5年）。
初回に全期間を取得し、以後はローカルの生データを保持する。

```powershell
python -m akiyama sync-bulk      # 約800MB（圧縮）、ファイル数約1,300
python -m akiyama sync-td
python -m akiyama normalize
python -m akiyama status
```

保存先は既定で `data/`（`config/settings.yaml` の `data_dir` または環境変数 `AKIYAMA_DATA_DIR` で変更可）。

## データの時点整合（available_at）

すべての行に「その情報を判断に使ってよい最も早い時刻（JST）」を付ける。判定締切より後の行はパケットに入れない。

| データ | available_at | basis |
|---|---|---|
| 決算短信サマリー・TDnet開示 | 開示日＋開示時刻 | `exact`（時刻欠損は当日23:59、`assumed_end_of_day`） |
| 株価・指数 | 当日16:30 | `assumed_prices_close` 等 |
| 上場銘柄情報 | 当日07:00 | `assumed_master_time` |
| 決算発表予定・投資部門別 | 公表日23:59 | `assumed_pub_date_time` |
| 信用残 | 公表日23:59、公表日なしは翌営業日23:59 | |
| 業種別空売り比率 | 翌営業日23:59 | `assumed_next_trading_day` |

想定時刻は `config/settings.yaml` の `availability` で変更できる。保守的（遅め）に置いている。

## プランで取れないもの（Standard）

財務詳細（`/fins/details`）、配当（`/fins/dividend`）、売買内訳（`/markets/breakdown`）はPremium限定。
収益は配当を含まない価格リターンを基準とする（旧研究と同じ）。

## 構成

```
src/akiyama/
  jquants.py    J-Quants v2クライアント（x-api-key、ページング、再試行。署名付きURLにはキーを送らない）
  rawstore.py   追記専用の生データ保管（manifest.jsonl に取得時刻・sha256）
  datasets.py   データセット定義と available_at ルール
  ingest.py     bulk / TDnet / 開示PDFの取得
  normalize.py  時点付きParquetの生成
  store.py      DuckDBビュー（prices_adj：分割調整済み価格）
config/settings.yaml
tests/
```
