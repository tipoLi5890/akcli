[English](../README.md) · **繁體中文** · [简体中文](README.zh-Hans.md)

# altium-kicad-cli

**altium-kicad-cli**（CLI 指令 `akcli`，匯入套件名 `altium_kicad_cli`）是一套零相依的
Python 工具包與 Claude Code 外掛，能在**未安裝 Altium 或 KiCad** 的情況下讀取 **Altium 二進位
`.SchDoc` / `.SchLib` / `.PcbDoc`** **以及** **KiCad `.kicad_sch` / `.kicad_sym` / `.kicad_pcb`**，
並從命令列執行 ERC／電源／pinmap／BOM／diff 檢查，再從 JSON op-list 繪製 KiCad 電路圖。
它是為 AI 程式代理（AI coding agents）打造的。

它將兩種格式都讀入同一套正規化模型，並對其進行*分析*——解析、檢查、比對、繪製——
讓你擁有一套可腳本化、免安裝的工作流程，能由自動化管線或 LLM 代理來驅動。

[![CI](https://github.com/tipoLi5890/altium-kicad-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/tipoLi5890/altium-kicad-cli/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 重點特色

- **兩種格式，一套模型。** Altium 二進位 `.SchDoc` 與 KiCad `.kicad_sch` 都會正規化為同一套
  `Schematic`／`Pcb`／`Library` 模型，因此每一項檢查、比對與報表都與格式無關。
- **免安裝 EDA。** 純標準函式庫的 OLE2/CFBF 與 Altium 記錄解碼，加上一個迭代式的 KiCad
  S-expression 解析器。不需要 Altium、不需要 KiCad、不需要任何編譯的擴充——只要 Python ≥ 3.11。
- **零執行期相依。** 僅使用標準函式庫（包含 `tomllib`）。易於 vendoring、沙箱化或在 CI 中執行。
- **可信賴的網路（net）推論。** 重建的 net 層能處理全域同名合併、junction、T 型接點與 No-ERC 標記
  ——修正了經典的「同名 net 被拆成單腳 net」的 bug。
- **對 Altium 唯讀、對 KiCad 安全寫入。** 離線時絕不修改 Altium 檔案；KiCad 的寫入會經過
  atomic 快照 → 暫存 → 驗證 → 取代的管線，並搭配純 Python 的連通性閘門。
- **AI 代理原生支援。** 以 Claude Code 外掛形式發布，內含 skills／commands，輸出帶有
  `schema_version` 的結構化 JSON，並接受帶版本的 op-list 以進行確定性、冪等的編輯。

## 讀取 Altium 檔案

`akcli` 可直接開啟 Altium 二進位檔。它內含一個強化過的 OLE2/CFBF（Compound File Binary
Format）容器讀取器與 Altium 記錄解碼器——不需要 Altium Designer、不需要 Windows、不需要授權。

```bash
akcli read   main.SchDoc        # 將 .SchDoc 解析為正規化 JSON
akcli net    main.SchDoc         # 擷取 netlist（net -> pins）
akcli component main.SchDoc U10    # 單一元件的腳位 -> net（需給 designator）
```

支援的 Altium 輸入：`.SchDoc`（電路圖）、`.SchLib`（符號庫——文字記錄符號；含二進位符號記錄的庫會以 exit 5「不支援」拒絕）、`.PcbDoc`（電路板——目前支援
ASCII 的 `Nets6`／`Components6`／`Classes6`／`Rules6` 區段；二進位的 pad/track 區段會明確報錯拒絕，
而不會誤解析）。所有 Altium 存取皆為**唯讀**。

## 讀取 KiCad 檔案

同一套 CLI 以一個顯式堆疊（非遞迴）的 tokenizer 解析 KiCad 的 S-expression 格式，
該 tokenizer 對深度、atom 與節點數量都有界限——因此格式錯誤或惡意的檔案無法撐爆堆疊。

```bash
akcli read board.kicad_sch              # .kicad_sch -> 正規化 JSON
akcli net  board.kicad_sch              # net 成員關係，共用的 net 引擎
```

KiCad 的 pin 電氣型別會在讀取時從 `lib_symbols` 解析出來（實例 pin 不帶型別），
因此 ERC 擁有所需的資料。S-expression 讀取器與版本無關——KiCad 7／8 有測試 fixture 覆蓋，
較新的格式（9／10）也走同一條解析路徑。

## 執行檢查（ERC、power、pinmap、BOM、diff）

不必開啟任何 EDA 工具，即可執行電氣規則檢查（ERC）與其他設計檢查：

```bash
akcli check  main.SchDoc                          # ERC-lite + power + BOM 衛生檢查
akcli pinmap main.SchDoc -C altium-kicad-cli.toml # MCU pin -> net（+ 選用的預期對照表）
akcli diff   v1.SchDoc v2.SchDoc                   # 以 net 成員關係比對，而非以名稱比對
```

電源／接地偵測是**以 net 名稱 + power port 為基礎**，而非純粹以電氣型別判斷，因為
真實電路板大多是 `Passive` pin——只看型別的天真 ERC 會產生空洞的通過結果。每份報表都會
印出一段中繼資料標頭（passive-pin 比例、被抑制的 No-ERC 數量、未命名 net 數量、是否含有小數座標），
因此乾淨的結果絕不會被誤認為空結果。

## 從 op-list 寫入 KiCad 電路圖

`akcli` 會從帶版本的 JSON **op-list**（放置元件、wire、junction、label、power port、文字……）
寫出 KiCad 電路圖。寫入是精準且冪等的（確定性 UUIDv5），由純 Python 的連通性驗證器把關，
並需要明確的 `--apply`（預設為 dry run）。

```bash
akcli plan  ops.json --target board.kicad_sch     # 驗證 op-list，顯示將會變更的內容
akcli draw  ops.json --target board.kicad_sch     # 預設為 dry-run（不寫入檔案）
akcli draw  ops.json --target board.kicad_sch --apply   # atomic 寫入 + 驗證 + 備份
```

Altium 的*寫入／繪製*僅能透過選用的 Windows 即時驅動（需執行中的 Altium 22+）；
離線時，Altium 僅供分析。

## 尋找 JLCPCB／LCSC 零件

`akcli jlc` 可搜尋 JLCPCB／LCSC 零件庫（庫存、價格階梯、Basic／Extended 狀態），並可將零件**行程內**轉換為 KiCad 庫（內嵌 MIT 授權的 [JLC2KiCadLib](https://github.com/TousstNicolas/JLC2KiCad_lib) 核心——無需安裝外部工具；見[致謝](#致謝)）。

```bash
akcli jlc search "0.1uF 0402 X7R"     # 關鍵字／MPN／分類搜尋（需網路）
akcli jlc show   C7593                 # 以 LCSC C-number 查單一零件
akcli jlc add    C2040 --3d            # LCSC 零件 → KiCad 符號＋封裝＋STEP
```

## 搭配 AI 程式代理使用

`akcli` 就是一個普通 CLI，只要它在 PATH 上，任何能執行 shell 指令的代理都能驅動它。指令以 `--json`
輸出結構化 JSON（`read` 與各項檢查帶有 `schema_version`；`net` 為陣列），op-list 帶有
`protocol_version`，因此輸出可保持機器可驗證且冪等。管線（`akcli … | head`）下 shell 回報的是管線的
exit code 而非 akcli 的——若要據此判斷請加 `set -o pipefail`。

- **Claude Code** — 安裝隨附的外掛（見下方），即可取得 `/altium-kicad:circuit-review`、
  `circuit-pinmap`、`circuit-draw`、`circuit-diff` 指令與六個 skills：`circuit-design`（讀取／分析／
  繪製基礎）、`circuit-debug`（連線與工具除錯）、`schematic-review`（依嚴重度分級的設計審查）、
  `schematic-authoring`（用 op-list 從零設計電路）、`altium-interop`（與 Altium Designer 互通）、
  `parts-sourcing`（JLC／LCSC 零件選型）。
- **Codex** — 安裝隨附的外掛（見下方）：內含全部六個 skills 與 session hook；或把 skills 資料夾放進
  `.agents/skills/` 讓其自動探索。見 [docs/codex-plugin.md](../docs/codex-plugin.md)。
- **OpenCode** — 會自動探索隨附的 skills；把它們放進各自的 skills 目錄，
  並讓代理透過 shell 呼叫 `akcli`。指令與一鍵設定 prompt 見 [INSTALL.md](../INSTALL.md#use-with-ai-coding-agents)。

原生 MCP server 仍在[路線圖](#路線圖)中。

## 安裝

尚未發佈到 PyPI——請從原始碼安裝。零執行期相依，需要 **Python ≥ 3.11**（用於標準函式庫 `tomllib`）：

```bash
# 從 clone 直接執行，免安裝
git clone https://github.com/tipoLi5890/altium-kicad-cli
./altium-kicad-cli/bin/akcli --help        # wrapper 會自動選用 Python ≥ 3.11

# 或以 pipx 把 CLI 裝到 PATH 上
pipx install git+https://github.com/tipoLi5890/altium-kicad-cli
akcli --version
```

Claude Code 外掛（marketplace 名稱為 `altium-kicad`）：

```text
/plugin marketplace add tipoLi5890/altium-kicad-cli
/plugin install altium-kicad@altium-kicad
```

Codex 外掛（名稱同為 `altium-kicad`）：

```bash
codex plugin marketplace add tipoLi5890/altium-kicad-cli   # 或在 clone 內用 `add ./`
codex plugin install altium-kicad@altium-kicad
```

完整細節、各代理設定與疑難排解請見 [INSTALL.md](../INSTALL.md)。

## 路線圖

目前已提供：Altium `.SchDoc`／`.SchLib` 與 KiCad `.kicad_sch` 讀取（與版本無關，KiCad **含階層
sheet**）、net 推論、ERC/power/BOM/diff/pinmap 檢查、KiCad 寫入／繪製（16 種 op,含 delete／move
與多單元放置,輸出經 KiCad 自身 ERC 驗證）,以及 JLCPCB／LCSC 零件搜尋。完整里程碑規劃
（v0.2 → v1.0，各里程碑附驗收條件）見 **[ROADMAP.md](../ROADMAP.md)**。重點待開發項目：

- Altium `.PcbDoc` **二進位**區段（pad/track/via/arc/fill/region）——目前可讀 ASCII 區段。
- **離線 Altium 寫入**與以 Altium 為權威的 ERC/netlist（目前需即時驅動）。
- **階層／多 sheet** 的 KiCad *寫入*（writer 仍為單層 flat；reader 已支援階層讀取）。
- 針對 Windows + Altium 22+ 的 Altium **即時驅動**（DelphiScript 部分仍為待驗證的 scaffold）。
- 原生 **MCP server**。

---

## 致謝

`akcli jlc` 建構於以下開源專案之上（完整出處與授權條款見
[ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) 與 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)）：

- **JLC2KiCadLib**，作者 **TousstNicolas**（MIT）——LCSC → KiCad 轉換核心，以 vendored 方式內嵌（見 THIRD_PARTY_NOTICES）。
- **jlcsearch**（tscircuit，MIT）與 **jlcparts**（MIT）——零件搜尋後端。
- **EasyEDA／LCSC／JLCPCB**——元件資料來源。

---

## 聯絡方式

問題、bug 或功能請求：請[開一個 GitHub issue](https://github.com/tipoLi5890/altium-kicad-cli/issues)。

---

## 授權

MIT © 2026 Li, ching yu。詳見 [LICENSE](../LICENSE)；第三方出處標註見
[ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) 與 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)，
安全模型見 [SECURITY.md](../SECURITY.md)。
