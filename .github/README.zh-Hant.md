<div align="center">

<img src="../docs/assets/hero.png" alt="akcli — 給人與 AI 代理的 KiCad 原生設計 CLI" width="820">

<p><strong>AI 原生電路圖設計，針對 KiCad 專項打造 — 零相依（純 Python 標準庫）的命令列工具。</strong></p>

<p>
  <a href="https://github.com/tipoLi5890/akcli/actions/workflows/ci.yml"><img src="https://github.com/tipoLi5890/akcli/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/dependencies-0-brightgreen" alt="零執行期相依">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT">
</p>

<p><a href="../README.md">English</a> · <strong>繁體中文</strong> · <a href="README.zh-Hans.md">简体中文</a> · <a href="README.ja.md">日本語</a></p>

</div>

---

**akcli** 是一套零相依的 Python CLI，專注於**在 KiCad 上進行 AI 原生電路圖設計**——一條可腳本化的
設計循環，由你或**任何 AI 代理**端到端驅動（隨附 Claude Code、Codex、OpenCode 的外掛／skills，
但其實只要有 shell 就能用）。從 JSON op-list 繪製 `.kicad_sch`、執行 ERC／設計審查／BOM／
原理圖 ↔ PCB 檢查、在 ngspice 上模擬、查找真實可訂購的 JLCPCB/LCSC 料件。既有的 Altium
`.SchDoc` / `.SchLib` / `.PcbDoc` / `.PcbLib` 設計可唯讀匯入——作為進入 KiCad 流程的入口，
所有後續發展都以 KiCad 為路線。

## 安裝

零執行期相依，需要 **Python ≥ 3.11**（用於標準函式庫 `tomllib`）。發行名為 `akcli-kicad`，指令仍是 `akcli`：

```bash
# 從 clone 直接執行，免安裝
git clone https://github.com/tipoLi5890/akcli
./akcli/bin/akcli --help        # 包裝腳本會自動選用 Python ≥ 3.11

# 或從 PyPI 裝到 PATH 上
pipx install akcli-kicad        # 或：pip install akcli-kicad
akcli --version
```

Claude Code 外掛（marketplace 名稱為 `akcli`）：

```text
/plugin marketplace add tipoLi5890/akcli
/plugin install akcli@akcli
```

Codex 外掛（名稱同為 `akcli`）：

```bash
codex plugin marketplace add tipoLi5890/akcli   # 或在 clone 內用 `add ./`
codex plugin install akcli@akcli
```

完整細節、各代理設定與疑難排解請見 [INSTALL.md](../INSTALL.md)。

## 快速上手

直接在 shell 裡讀取設計、檢查、並繪製進去：

```bash
akcli read  board.kicad_sch --summary                            # 正規化 JSON，控制在上下文預算內
akcli check board.kicad_sch                                      # ERC-lite + power + BOM + 連通性 + 文件完整性
akcli draw  board.kicad_sch --ops ops.json                       # dry-run：顯示變更 + net diff
akcli draw  board.kicad_sch --ops ops.json --apply --strict-nets # 原子寫入 + 驗證 + 備份
akcli undo  board.kicad_sch                                      # 回復上一次寫入
```

每次寫入預設都是 dry run；`--apply` 會經過原子式的快照 → 暫存 → 驗證 → 取代管線，並搭配純 Python
的連通性閘門，`akcli undo` 則從輪替備份堆疊回復。離線時 Altium 檔案一律唯讀。

## 功能一覽

每個指令背後都是同一套正規化模型，因此每一項檢查、比對與報表都作用於 KiCad `.kicad_sch`——
對匯入的 Altium `.SchDoc` 也一體適用：

| 指令 | 功能 |
|---|---|
| `read` · `net` · `component` · `pins` | 把 KiCad 或 Altium 解析成同一套正規化 JSON 模型；查詢 net（整條 net，或腳位層級的 `net --at`／`--same`／`--why`）、元件與腳位座標。 |
| `new` · `plan` · `draw` · `ops` · `arrange` | 從帶版本的 **22 種 op + 10 種巨集** JSON op-list 繪製 `.kicad_sch`（根檔案，或既有階層中的子圖頁），並由 net-diff 安全閘門與一鍵 `undo` 把關。符號來源依序為 `--symbols`、設定檔路徑，最後是專案與全域的 `sym-lib-table`。 |
| `check` · `verify` · `diff` · `pinmap` | ERC-lite + power + BOM + 文件完整性 + intent／contract 檢查、原理圖 ↔ PCB 等價、net 成員關係比對、MCU pin → net 對照。 |
| `review` | 建議性（advisory）、依信心分級的設計審查，橫跨六大偵測家族（signal／validation／pcb／emc／domain／gerber）；標記 DNP 的零件不會滿足任何規則。 |
| `sim` | 轉成 SPICE deck，在 KiCad 自帶 ngspice 上斷言；角點掃描；用 datasheet 點擬合二極體模型。元件值會先正規化，無法確定解讀的一律拒絕而不猜測。 |
| `jlc` | 搜尋 JLCPCB/LCSC（庫存、價格、Basic／Extended），行程內把零件轉成 KiCad 庫（帶電氣型別的腳位、真實 `Value`），抓取規格書。 |
| `calc` | **60** 個附標準引用的離線計算器（E 系列、IPC-2221、阻抗、I²C 上拉、buck／boost……），每筆都附引用。 |
| `library` · `fab` · `release` | 零件庫工作區稽核／修復、版本化 fab profile 檢查，以及寫出可追溯 manifest 的 release preflight。 |
| `export` · `render` · `doc` · `view` | netlist 匯出（Protel／KiCad／CSV——KiCad 格式帶有重新匯入所需的 UUID sheetpath 身分）、純標準庫 SVG 渲染、Markdown pinout book，以及 localhost 的 `/calc` + `/live` 儀表板。 |

兩個代表性範例——一次設計審查與一次料件搜尋：

```bash
akcli review analyze board.kicad_sch --profile deep --pcb board.kicad_pcb   # 建議性審查發現 + 證據
akcli jlc search "0.1uF 0402 X7R"                                           # JLCPCB/LCSC 零件庫（需網路）
```

## 搭配 AI 代理使用

`akcli` 就是一個普通 CLI，只要它在 PATH 上，任何能執行 shell 指令的代理都能驅動它。指令以 `--json`
輸出結構化 JSON（帶有 `schema_version`），op-list 帶有 `protocol_version`，`akcli capabilities`
會在單一 JSON 文件中自我描述整套 CLI 介面——而且每一個錯誤碼都帶有機器可讀的 `remediation` 修復提示。

- **Claude Code** — 安裝隨附外掛，即可取得八個 `/akcli:circuit-*` 指令（review、pinmap、draw、
  diff、parts、sim、datasheet、release）與十二個 skills，涵蓋設計、審查、電路撰寫、Altium 互通、料件選型、計算器與 release
  把關。
- **Codex** — 安裝隨附外掛，或把 skills 資料夾放進 `.agents/skills/` 讓其自動探索。見
  [docs/codex-plugin.md](../docs/codex-plugin.md)。
- **OpenCode** — 會自動探索隨附的 skills；指令見
  [INSTALL.md](../INSTALL.md#use-with-ai-coding-agents)。

## 為什麼選 akcli

- **零執行期相依。** 僅使用標準函式庫（含 `tomllib`）——易於內嵌（vendor）、沙箱化或在 CI 中執行。
- **針對 KiCad 專項打造。** 核心是迭代式的 KiCad S-expression 解析器與位元組穩定的寫入器；
  Altium 設計經純標準函式庫的 OLE2/CFBF 記錄解碼唯讀匯入，直接接進同一條 KiCad 流程。
  不需要任何編譯的擴充。
- **位元組完全一致的重複套用。** 確定性 UUIDv5 + 就地取代，讓每次編輯都冪等——重跑同一份 op-list
  會產生完全相同的位元組。
- **連通性是唯一的硬性寫入閘門。** 每次 `plan`／`draw` 都印出寫入前後的 net diff（拆分、合併、
  改名——以 pin 成員關係比對，絕不以名稱比對）；`--strict-nets` 會拒絕任何拆分或合併具名 net 的寫入。
- **可信賴的 net 推論。** 重建的 net 層能處理全域同名合併、junction、T 型接點與 No-ERC 標記——
  修正了經典的「同名 net 被拆成單腳 net」bug。

## 選用的外部工具

「零相依」指的是 **Python 套件**相依為零：`pip install` 不會拉進任何第三方套件，核心流程
（read／plan／draw／check／diff／calc／render）只靠標準函式庫就能完整運作。少數功能可以借助
Python 之外的東西——一律在執行期偵測，核心流程永遠不需要它們：

| 功能 | 使用 | 缺少時 |
|---|---|---|
| 建議性 ERC 第二意見、`view live` 的 SVG | `kicad-cli`（本機執行檔） | 直接跳過——不會導致失敗，結果本來就只是建議性的。 |
| `sim` 執行（`--deck-only` 不需要） | libngspice（KiCad 自帶） | `sim` 以 `NGSPICE_MISSING` 結束；其餘功能不受影響。 |
| `jlc` 料件搜尋／規格書抓取 | 網路 | **唯一**連網的指令家族；其餘全部離線。 |

`akcli doctor` 會逐項探測上述三者，並印出各作業系統的修復建議。

## 文件

| 文件 | 內容 |
|---|---|
| [SPEC.md](../docs/SPEC.md) | 資料模型、設定表、JSON schema |
| [cli-reference.md](../docs/cli-reference.md) | 每個指令與旗標 |
| [op-list-authoring.md](../docs/op-list-authoring.md) | op-list 撰寫權威指南（op、macro、group） |
| [design-integrity.md](../docs/design-integrity.md) | 契約、fab profile、release preflight |
| [review-rules.md](../docs/review-rules.md) | 設計審查規則目錄 |
| [sim.md](../docs/sim.md) | 模擬參考 |
| [ROADMAP.md](../ROADMAP.md) | 通往 v1.0 的路線圖與驗收條件 |

## 路線圖

**目前已提供（v0.18.0）：** KiCad 寫入／繪製（22 種 op + 10 種巨集，含階層 `add_sheet` 與寫入既有
階層中的子圖頁、net-diff 安全護欄、`new`／多層 `undo`）、保持 net 不變的 `arrange --groups` 重排、
建議性的 `akcli review` 引擎（六大偵測家族，現已納入電源入口保護偵測、datasheet facts store、
`propose`／`tree`／`validate`）、ERC／power／BOM／diff／pinmap／intent／contract 檢查（含 waiver 與
SARIF）、唯讀的文件完整性檢查 `check --integrity`、原理圖 ↔ PCB `verify`、專案 `library` 工作區、
版本化 `fab` profile、`release preflight` 把關、KiCad 自帶 ngspice 上的 `akcli sim`、JLCPCB／LCSC
料件搜尋與規格書抓取、60 個附標準引用的計算器、能畫出 KiCad `lib_symbols` 真實符號圖形的純標準函式庫
SVG 渲染，以及版本容忍的 Altium／KiCad 讀取器。標準符號 nickname 在原廠 KiCad 安裝上即可解析——那些
編譯內建的 `${KICAD*_SYMBOL_DIR}` 路徑會從找到的 `kicad-cli` 推導，`akcli doctor` 則會回報這條解析
路徑在本機是否可用。

**前瞻（→ v1.0）：** 契約凍結稽核（contract freeze audit）。第一個 PyPI 版本
（`pip install akcli-kicad`）已於 0.15.0 發佈。*依決策暫緩：* 為電路圖 PR 把關的 GitHub Action、`view` 波形面板，
以及原生 MCP server（目前代理直接驅動 CLI）。完整計畫與驗收條件見 [ROADMAP.md](../ROADMAP.md)。

## 致謝

`akcli jlc` 建構於以下開源專案之上（完整出處與授權條款見
[ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) 與 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)）：

- **JLC2KiCadLib**，作者 **TousstNicolas**（MIT）——LCSC → KiCad 轉換核心，以 vendored 方式內嵌。
- **jlcsearch**（tscircuit，MIT）與 **jlcparts**（MIT）——零件搜尋後端。
- **EasyEDA／LCSC／JLCPCB**——元件資料來源。

## 聯絡方式

問題、bug 或功能請求：請[開一個 GitHub issue](https://github.com/tipoLi5890/akcli/issues)。

## 授權

MIT © 2026 Li, ching yu。詳見 [LICENSE](../LICENSE)；第三方出處標註見
[ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) 與 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)，
安全模型見 [SECURITY.md](../SECURITY.md)。
