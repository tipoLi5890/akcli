<div align="center">

<img src="../docs/assets/hero.png" alt="akcli — 人と AI エージェントのための KiCad ネイティブ設計 CLI" width="820">

<p><strong>AI が書いた KiCad 回路図を安心して適用できるようにする検証レイヤー — 依存ゼロ（純 Python 標準ライブラリ）の CLI。</strong></p>

<p>
  <a href="https://github.com/joanbono/awesome-kicad"><img src="https://awesome.re/mentioned-badge.svg" alt="Mentioned in Awesome KiCad"></a>
  <a href="https://github.com/tipoLi5890/akcli/actions/workflows/ci.yml"><img src="https://github.com/tipoLi5890/akcli/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/dependencies-0-brightgreen" alt="ランタイム依存ゼロ">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT">
</p>

<p><a href="../README.md">English</a> · <a href="README.zh-Hant.md">繁體中文</a> · <a href="README.zh-Hans.md">简体中文</a> · <strong>日本語</strong></p>

</div>

---

**akcli** は、**KiCad 上で AI ネイティブな回路図設計**を行うための依存ゼロの Python CLI です。あなた自身でも
**任意の AI エージェント**でも端から端まで駆動できる、スクリプト化された設計ループを提供します（Claude
Code・Codex・OpenCode 向けのプラグイン／skills を同梱していますが、shell さえあれば動きます）。書き込みは機能であり、検証こそが安心して使える理由です —
すべての編集は既定で dry-run、net diff と接続性ゲートを通ります。JSON の
op-list から `.kicad_sch` を生成し、ERC／設計レビュー／BOM／回路図 ↔ PCB のチェックを実行し、ngspice で
シミュレーションし、実際に発注できる JLCPCB/LCSC 部品を検索できます。既存の Altium `.SchDoc` / `.SchLib`
/ `.PcbDoc` / `.PcbLib` 設計は読み取り専用でインポートでき——KiCad フローへの入口として機能します。以降の
開発はすべて KiCad を基盤とします。

## インストール

ランタイム依存はゼロで、**Python ≥ 3.11**（標準ライブラリの `tomllib` のため）だけが必要です。発行名は
`akcli-kicad`、コマンドは `akcli` です：

```bash
pipx install akcli-kicad        # または: pip install akcli-kicad
akcli --version

# あるいは clone から直接実行（インストール不要）
git clone https://github.com/tipoLi5890/akcli
./akcli/bin/akcli --help        # ラッパーが Python ≥ 3.11 を自動選択
```

Claude Code プラグイン（marketplace 名は `akcli`）：

```text
/plugin marketplace add tipoLi5890/akcli
/plugin install akcli@akcli
```

Codex プラグイン（名前は同じく `akcli`）：

```bash
codex plugin marketplace add tipoLi5890/akcli   # または clone 内で `add ./`
codex plugin install akcli@akcli
```

詳細、各エージェントのセットアップ、トラブルシューティングは [INSTALL.md](../INSTALL.md) を参照してください。

## クイックスタート

まず実際の KiCad プロジェクトを（実例の op-list 付きで）スキャフォールドし、そのまま読み込み・チェック・書き込みまで shell から行えます：

```bash
akcli project new demo --example voltage-divider                  # .kicad_pro/.kicad_sch/.kicad_pcb/lib テーブル/ops.json
cd demo
akcli read  demo.kicad_sch --summary                              # 正規化 JSON、コンテキスト予算に収める
akcli check demo.kicad_sch                                        # ERC-lite + power + BOM + 接続性 + 整合性
akcli draw  demo.kicad_sch --ops ops.json                         # dry-run: 変更 + net diff を表示
akcli draw  demo.kicad_sch --ops ops.json --apply --strict-nets   # アトミック書き込み + 検証 + バックアップ
akcli project check                                               # プロジェクト整合性の指摘（引き続き 0 件）
akcli undo  demo.kicad_sch                                        # 直前の書き込みを取り消す
```

書き込みはデフォルトですべて dry run です。`--apply` はアトミックなスナップショット → 一時ファイル →
検証 → 置換のパイプラインを通り、純 Python の接続性ゲートを備えます。`akcli undo` はローテーションされる
バックアップスタックから復元します。オフラインでは Altium ファイルは常に読み取り専用です。

## 主な機能

すべてのコマンドの背後には単一の正規化モデルがあり、各チェック・差分・レポートは KiCad `.kicad_sch` に
対して動作し——インポートした Altium `.SchDoc` に対してもまったく同じように動作します：

| コマンド | 機能 |
|---|---|
| `read` · `net` · `component` · `pins` | KiCad または Altium を単一の正規化 JSON モデルに解析。net（net 全体、またはピン単位の `net --at`／`--same`／`--why`）・部品・ピン座標を照会。 |
| `project` · `new` · `plan` · `draw` · `ops` · `arrange` | ゼロから完全な KiCad プロジェクト（`.kicad_pro`/`.kicad_pcb`/lib テーブル、`--example NAME` も可）を骨組みするか、既存の `.kicad_sch` をプロジェクトに取り込み（`project new`／`project init`）、その拘束内容を調べ（`project info`）、整合性をゲートし（`project check`）、そのうえでバージョン管理された **22 op + 10 マクロ**の JSON op-list から `.kicad_sch` を生成——ルートファイル、子シート、あるいは**1 回のアトミックなトランザクションで複数シートに書き込み**（`sheets` envelope + op ごとの `sheet` ルーティング、run 単位のバックアップと `undo --run` 付き）。**sheet tree 全体**を対象に評価する net-diff の安全ゲートと 1 コマンドの `undo` が保護。`ops from-intent` は設計ドキュメント（宣言した nets + 部品）をそのまま op-list にコンパイル。シンボルは `--symbols`、設定のパス、最後にプロジェクトとグローバルの `sym-lib-table` から解決。 |
| `check` · `verify` · `diff` · `pinmap` | ERC-lite + power + BOM + ドキュメント整合性 + intent／contract チェック、回路図 ↔ PCB の等価性（`verify --library` は基板上の各フットプリントをライブラリの定義とも比較）、net メンバーシップ差分、MCU ピン → net マップ。 |
| `review` | 6 つの検出器ファミリ（signal／validation／pcb／emc／domain／gerber）にわたる、確信度別のアドバイザリ設計レビュー。DNP 指定の部品はどのルールも満たしません。 |
| `sim` | SPICE デッキにレンダリングし、KiCad 同梱の ngspice 上でアサート。コーナースイープ。データシートの点からダイオードモデルをフィッティング。部品値は正規化され、確実に解釈できない値は推測せず拒否します。 |
| `jlc` | JLCPCB/LCSC を検索（在庫・価格・Basic／Extended）。部品をインプロセスで KiCad ライブラリに変換（電気タイプ付きピン、実際の `Value`）。データシートを取得。 |
| `calc` | **60** 個の規格準拠オフライン計算機（E 系列、IPC-2221、インピーダンス、I²C プルアップ、buck／boost……）。各項目が根拠を明示。 |
| `library` · `fab` · `release` | ライブラリワークスペースの監査／修復、ピン表からのシンボル生成（`new-symbol`——ピン表を渡せば akcli が座標を計算）と独立した lint（`lint-symbols`）+ 書き込みを拒否する同一座標ピン検査、バージョン管理された fab プロファイルのチェック（同梱プロファイルは `fab profiles` で id 指定可能。基板の実際の外形・ビア・テンティング・プロジェクトの設計ルールを読み取ります）、コミット済みのライブラリベースライン（`library audit --write-baseline`）、ツール・gerber・プロファイルの証跡付きで追跡可能な manifest を書き出す release preflight（KiCad DRC を含む）。 |
| `gate` | GUI 編集のたびに回す反復用の 4 チェックバンドル（フル `check` セット、intent アサーション、回路図 ↔ PCB `verify`、BOM ロックのドリフト——デフォルトはオフライン）。設定済みプロジェクトなら引数ゼロで実行でき、リリース級のゲートは引き続き `release preflight`。 |
| `export` · `render` · `doc` · `view` | ネットリスト出力（Protel／KiCad／CSV——KiCad 形式は再インポートに必要な UUID sheetpath の同一性を保持）、純標準ライブラリの SVG レンダリング、Markdown のピンアウトブック、localhost の `/calc` + `/live` ダッシュボード。 |
| `capabilities` · `schema` | 自己記述的なインターフェース manifest——全体、セクション単位（`--section`）、またはコマンド単位（`--command draw`）——と、クライアント側検証のためにバイト単位で出力される全 JSON Schema。 |

代表的な 2 例——設計レビューと部品検索：

```bash
akcli review analyze board.kicad_sch --profile deep --pcb board.kicad_pcb   # アドバイザリな指摘 + 根拠
akcli jlc search "0.1uF 0402 X7R"                                           # JLCPCB/LCSC カタログ（ネットワークが必要）
```

## AI エージェントと併用する

`akcli` はただの CLI なので、shell を実行できるエージェントなら何でも駆動できます。コマンドは `--json`
で構造化 JSON（バージョンスタンプ付き——`schema_version`、または `proposals_version`／`facts_version` のようなファミリフィールド）を出力し、各 op-list は `protocol_version` を持ち、`akcli
capabilities` は CLI 全体を単一の JSON ドキュメントで自己記述します——さらにすべてのエラーコードには
機械可読な `remediation`（修復ヒント）が付きます。

- **Claude Code** — 同梱プラグインをインストールすると、8 つの `/akcli:circuit-*` コマンド（review・
  pinmap・draw・diff・parts・sim・datasheet・release）と、設計・レビュー・回路作成・Altium 連携・部品調達・計算機・リリース判定に
  またがる 13 個の skills が使えます。
- **Codex** — 同梱プラグインをインストールするか、skills フォルダを `.agents/skills/` に置けば自動検出
  されます。[docs/codex-plugin.md](../docs/codex-plugin.md) を参照。
- **OpenCode** — 同梱 skills を自動検出します。正確なコマンドは
  [INSTALL.md](../INSTALL.md#use-with-ai-coding-agents) を参照。

## akcli を選ぶ理由

- **ランタイム依存ゼロ。** 標準ライブラリのみ（`tomllib` を含む）——vendor 化・サンドボックス化・CI での
  実行が容易です。
- **KiCad 専用設計。** 中核は反復型の KiCad S 式パーサとバイト安定なライタ。Altium 設計は純標準ライブラリ
  の OLE2/CFBF レコードデコードで読み取り専用インポートされ、そのまま同じ KiCad フローに入ります。
  コンパイル拡張は不要です。
- **バイト単位で同一な再適用。** 決定論的な UUIDv5 + その場置換により、すべての編集が冪等です——同じ
  op-list を再実行すると同じバイト列が生成されます。
- **接続性が唯一のハード書き込みゲート。** すべての `plan`／`draw` は書き込み前後の net diff（分割・結合・
  移動・改名——名前ではなくピンのメンバーシップで照合）を表示します。`--strict-nets` は名前付き net を分割・
  結合・移動する書き込みを拒否します。
- **信頼できる net 推論。** 再構築された net 層はグローバルな同名結合・junction・T 字接続・No-ERC マーカー
  を処理し——古典的な「同名 net が単一ピンの net に分割される」バグを修正します。

## 任意の外部ツール

「依存ゼロ」とは Python **パッケージ**依存がゼロという意味です：`pip install` は第三者パッケージを一切
引き込まず、中核ループ（read／plan／draw／check／diff／calc／render）は標準ライブラリだけで完全に動作
します。一部の機能は Python 以外のものを利用できますが——常に実行時に検出され、中核ループが必須とする
ことはありません：

| 機能 | 使用するもの | 無い場合 |
|---|---|---|
| アドバイザリな ERC セカンドオピニオン、`view live` の SVG | `kicad-cli`（ローカル実行ファイル） | 静かにスキップ——致命的ではなく、結果は元々アドバイザリです。 |
| `sim` の実行（`--deck-only` は不要） | libngspice（KiCad 同梱） | `sim` は `NGSPICE_MISSING` で終了。その他は影響なし。 |
| 標準シンボルのニックネーム（`Device:R`、`power:*`） | `kicad-cli` のインストール場所（`${KICAD*_SYMBOL_DIR}` を導出） | シンボルは明示的な `--symbols`／設定パスからのみ解決されます。 |
| `jlc` の部品検索／データシート取得 | ネットワーク | **唯一**ネットワークを使うコマンド群。他はすべてオフライン。 |

`akcli doctor` はこれらを個別に検査し、OS ごとの修復手順を表示します。

## ドキュメント

| ドキュメント | 内容 |
|---|---|
| [SPEC.md](../docs/SPEC.md) | データモデル、設定テーブル、JSON スキーマ |
| [cli-reference.md](../docs/cli-reference.md) | すべてのコマンドとフラグ |
| [op-list-authoring.md](../docs/op-list-authoring.md) | op-list 作成の決定版ガイド（op・マクロ・group） |
| [design-integrity.md](../docs/design-integrity.md) | contract、fab プロファイル、release preflight |
| [review-rules.md](../docs/review-rules.md) | 設計レビュールールのカタログ |
| [sim.md](../docs/sim.md) | シミュレーションリファレンス |
| [agent-state.md](../docs/agent-state.md) | ワークスペースの状態ファイルとマルチエージェント契約 |
| [codex-plugin.md](../docs/codex-plugin.md) | Codex プラグインの構成と同梱スキル |
| [jlc.md](../docs/jlc.md) | `akcli jlc`：部品検索、BOM の調達可否、データシート取得、JLCPCB 製造データ受け渡し |
| [kicad-format-gotchas.md](../docs/kicad-format-gotchas.md) | 安全に編集するための KiCad ファイル形式のルール |
| [pin-identity.md](../docs/pin-identity.md) | v1.0 に向けたピン識別モデルの監査 |
| [migration-0.21.md](../docs/migration-0.21.md) | 0.21 契約への移行（`protocol_version` 2、schema 2.0） |
| [releasing.md](../docs/releasing.md) | リリース手順書 |
| [recipes/](../docs/recipes/README.md) | CI workflow レシピ（回路図 PR のゲート） |
| [stability.md](../docs/stability.md) | 公開コントラクト面、コントラクト外の範囲、バージョン規則 |
| [ROADMAP.md](../ROADMAP.md) | 受け入れ基準付きの v1.0 ロードマップ |

## ロードマップ

**提供済み（v0.23.0）：** リリースゲートに KiCad DRC、コミット済みのライブラリベースライン、PCB ↔ ライブラリ
チェック、sheet tree 全体の整合性チェックが加わり、`fab check` は基板の実際の外形・ビア・テンティング・
プロジェクトの設計ルールを読み取ります。あわせて、完全な KiCad プロジェクトのスキャフォールド——`project new`／`project init` が
ルートシートの隣に `.kicad_pro`、空の `.kicad_pcb` 基板コンテナ、プロジェクトの lib テーブル、`.gitignore` を
書き出し（`--example NAME` で実例の op-list も同梱）、`project info`／`project check` がそのプロジェクトの
束縛内容を読み取り・ゲートします。22 op + 10 マクロの語彙からの KiCad 書き込み／描画（階層 `add_sheet`、
`"create": true` で同一トランザクション内に子シートを作成可能、既存
階層の子シートへの書き込み、1 回のアトミックなトランザクションでの複数シート書き込み、sheet tree 全体を
対象とする net-diff 安全レール、複数回インスタンス化されたシートのインスタンス別リファレンス、
`new`／多段 `undo`／`undo --run`）、net を保つ `arrange --groups`
の再レイアウト、アドバイザリな `akcli review` エンジン（6 つの検出器ファミリ、電源入口保護を含む。さらに
datasheet facts store、`propose`／`tree`／`validate`）、ERC／power／BOM／diff／pinmap／intent／contract
チェック（waiver と SARIF 付き）、読み取り専用のドキュメント整合性チェック `check --integrity`、
回路図 ↔ PCB `verify`、プロジェクト `library` ワークスペース、バージョン管理された `fab` プロファイル、
`release preflight` ゲート、KiCad 同梱 ngspice 上の `akcli sim`、JLCPCB／LCSC 調達とデータシート取得、
60 個の規格準拠計算機、KiCad `lib_symbols` の実際のシンボル図形を描く純標準ライブラリの SVG
レンダリング、バージョン耐性のある Altium／KiCad リーダー。標準シンボルの nickname は素の KiCad
インストールで解決します——コンパイル時に組み込まれた `${KICAD*_SYMBOL_DIR}` のパスは検出した
`kicad-cli` から導出され、`akcli doctor` がこの解決がこの環境で動作するかを報告します。

**今後（→ v1.0）：** contract freeze 監査。最初の PyPI リリース（`pip install akcli-kicad`）は 0.15.0 で
提供されました。回路図 PR を判定する workflow レシピは [docs/recipes/](../docs/recipes/README.md) に収録されています。
*方針により保留：* パッケージ化された Marketplace Action、`view` の波形パネル、ネイティブ
MCP サーバー（現状はプレーン CLI がエージェントに対応）。完全な計画と受け入れ基準は
[ROADMAP.md](../ROADMAP.md) を参照。

## 謝辞

`akcli jlc` は以下の OSS を基盤としています（完全な出典とライセンスは
[ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) と [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) を
参照）：

- **JLC2KiCadLib**（作者 **TousstNicolas**、MIT）——LCSC → KiCad 変換の中核。vendored で同梱。
- **jlcsearch**（tscircuit、MIT）と **jlcparts**（MIT）——部品検索バックエンド。
- **EasyEDA／LCSC／JLCPCB**——部品データの提供元。

## 連絡先

質問・バグ・機能リクエストは [GitHub issue を作成](https://github.com/tipoLi5890/akcli/issues)して
ください。

## ライセンス

MIT © 2026 Li, ching yu。[LICENSE](../LICENSE) を参照。第三者の出典表示は
[ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) と [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)、
セキュリティモデルは [SECURITY.md](../SECURITY.md) を参照してください。
