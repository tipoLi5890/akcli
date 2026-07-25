<div align="center">

<img src="../docs/assets/hero.png" alt="akcli — 人と AI エージェントのための KiCad ネイティブ設計 CLI" width="820">

<p><strong>AI ネイティブな回路図設計、KiCad のために作られた依存ゼロ（純 Python 標準ライブラリ）の CLI。</strong></p>

<p>
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
Code・Codex・OpenCode 向けのプラグイン／skills を同梱していますが、shell さえあれば動きます）。JSON の
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

shell から直接、設計を読み込み・チェックし・書き込めます：

```bash
akcli read  board.kicad_sch --summary                            # 正規化 JSON、コンテキスト予算に収める
akcli check board.kicad_sch                                      # ERC-lite + power + BOM + 接続性 + 整合性
akcli draw  board.kicad_sch --ops ops.json                       # dry-run: 変更 + net diff を表示
akcli draw  board.kicad_sch --ops ops.json --apply --strict-nets # アトミック書き込み + 検証 + バックアップ
akcli undo  board.kicad_sch                                      # 直前の書き込みを取り消す
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
| `new` · `plan` · `draw` · `ops` · `arrange` | バージョン管理された **22 op + 10 マクロ**の JSON op-list から `.kicad_sch`（ルートファイル、または既存階層の子シート）を生成。net-diff の安全ゲートと 1 コマンドの `undo` が保護。シンボルは `--symbols`、設定のパス、最後にプロジェクトとグローバルの `sym-lib-table` から解決。 |
| `check` · `verify` · `diff` · `pinmap` | ERC-lite + power + BOM + ドキュメント整合性 + intent／contract チェック、回路図 ↔ PCB の等価性、net メンバーシップ差分、MCU ピン → net マップ。 |
| `review` | 6 つの検出器ファミリ（signal／validation／pcb／emc／domain／gerber）にわたる、確信度別のアドバイザリ設計レビュー。DNP 指定の部品はどのルールも満たしません。 |
| `sim` | SPICE デッキにレンダリングし、KiCad 同梱の ngspice 上でアサート。コーナースイープ。データシートの点からダイオードモデルをフィッティング。部品値は正規化され、確実に解釈できない値は推測せず拒否します。 |
| `jlc` | JLCPCB/LCSC を検索（在庫・価格・Basic／Extended）。部品をインプロセスで KiCad ライブラリに変換（電気タイプ付きピン、実際の `Value`）。データシートを取得。 |
| `calc` | **60** 個の規格準拠オフライン計算機（E 系列、IPC-2221、インピーダンス、I²C プルアップ、buck／boost……）。各項目が根拠を明示。 |
| `library` · `fab` · `release` | ライブラリワークスペースの監査／修復、バージョン管理された fab プロファイルのチェック、追跡可能な manifest を書き出す release preflight。 |
| `export` · `render` · `doc` · `view` | ネットリスト出力（Protel／KiCad／CSV——KiCad 形式は再インポートに必要な UUID sheetpath の同一性を保持）、純標準ライブラリの SVG レンダリング、Markdown のピンアウトブック、localhost の `/calc` + `/live` ダッシュボード。 |

代表的な 2 例——設計レビューと部品検索：

```bash
akcli review analyze board.kicad_sch --profile deep --pcb board.kicad_pcb   # アドバイザリな指摘 + 根拠
akcli jlc search "0.1uF 0402 X7R"                                           # JLCPCB/LCSC カタログ（ネットワークが必要）
```

## AI エージェントと併用する

`akcli` はただの CLI なので、shell を実行できるエージェントなら何でも駆動できます。コマンドは `--json`
で構造化 JSON（`schema_version` 付き）を出力し、各 op-list は `protocol_version` を持ち、`akcli
capabilities` は CLI 全体を単一の JSON ドキュメントで自己記述します——さらにすべてのエラーコードには
機械可読な `remediation`（修復ヒント）が付きます。

- **Claude Code** — 同梱プラグインをインストールすると、8 つの `/akcli:circuit-*` コマンド（review・
  pinmap・draw・diff・parts・sim・datasheet・release）と、設計・レビュー・回路作成・Altium 連携・部品調達・計算機・リリース判定に
  またがる 12 個の skills が使えます。
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
  改名——名前ではなくピンのメンバーシップで照合）を表示します。`--strict-nets` は名前付き net を分割・
  結合する書き込みを拒否します。
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
| [ROADMAP.md](../ROADMAP.md) | 受け入れ基準付きの v1.0 ロードマップ |

## ロードマップ

**提供済み（v0.16.0）：** 22 op + 10 マクロの語彙からの KiCad 書き込み／描画（階層 `add_sheet`、net-diff
安全レール、`new`／多段 `undo`）、net を保つ `arrange --groups` の再レイアウト、アドバイザリな `akcli
review` エンジン（6 つの検出器ファミリ、電源入口保護を含む。さらに datasheet facts store、`propose`／
`tree`／`validate`）、ERC／power／BOM／diff／pinmap／intent／contract チェック（waiver と SARIF 付き）、
回路図 ↔ PCB `verify`、プロジェクト `library` ワークスペース、バージョン管理された `fab` プロファイル、
`release preflight` ゲート、KiCad 同梱 ngspice 上の `akcli sim`、JLCPCB／LCSC 調達とデータシート取得、
60 個の規格準拠計算機、純標準ライブラリの SVG レンダリング、バージョン耐性のある Altium／KiCad リーダー。

**今後（→ v1.0）：** contract freeze 監査。最初の PyPI リリース（`pip install akcli-kicad`）は 0.15.0 で
提供されました。*方針により保留：* 回路図 PR を判定する GitHub Action、`view` の波形パネル、ネイティブ
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
