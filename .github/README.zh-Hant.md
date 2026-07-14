[English](../README.md) · **繁體中文** · [简体中文](README.zh-Hans.md)

# akcli

**akcli**（CLI 指令 `akcli`，匯入套件名 `akcli`）是一套零相依的
**KiCad 原生 AI 設計代理**——Python 工具包與 Claude Code 外掛，讓 AI 代理能在**未安裝 Altium 或
KiCad** 的情況下：從 JSON op-list **繪製與編輯** `.kicad_sch`（含 net-diff 安全防護欄與一鍵 undo）、
執行 ERC／設計／intent／BOM 檢查、**在 KiCad 自帶的 ngspice 上模擬**、查找實體料件與抓取規格書，
並**匯入 Altium `.SchDoc` / `.SchLib` / `.PcbDoc`**。

**KiCad 是可寫入的目標**；Altium 檔案則被*匯入*同一套正規化模型以供分析（選用的 Windows
*live bridge* 也能驅動執行中的 Altium 實例）。成果是一套可腳本化、免安裝的設計迴圈——從匯入的
既有電路圖或一張空白圖紙起步，一路走到經過模擬、選好料件、可下單的板子——由自動化管線或 AI 代理端到端驅動。

[![CI](https://github.com/tipoLi5890/akcli/actions/workflows/ci.yml/badge.svg)](https://github.com/tipoLi5890/akcli/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 重點特色

- **AI 代理原生支援。** 以 Claude Code 外掛形式發布，內含 skills／commands，輸出帶有
  `schema_version` 的結構化 JSON，並接受帶版本的 op-list 以進行確定性、冪等的編輯。
- **net-diff 安全護欄。** 每次 `plan`／`draw` 都會印出寫入前後的 **net 連通性差異**
  （拆分、合併、改名——以 pin 成員關係比對，絕不以名稱比對）；`draw --apply --strict-nets`
  會拒絕任何拆分或合併具名 net 的寫入，`akcli check --intent` 則可在任何編輯後
  斷言一份設計意圖（design-intent）netlist 快照。
- **模擬並斷言。** `akcli sim` 將電路圖轉成 SPICE deck，透過 KiCad 的 libngspice 在崩潰隔離的
  子行程中執行，把 `.meas` 結果轉為可在 CI 中把關的 pass／fail 發現——若未安裝引擎，可用
  `--deck-only` 只輸出 deck。
- **附標準引用的計算器。** `akcli calc` 回答 60 種設計計算（E 系列、IPC-2221、貫孔寄生參數、
  I²C 上拉、buck／boost……），每筆結果都印出正式的引用來源。
- **一套正規化模型。** KiCad `.kicad_sch` 與 Altium 二進位 `.SchDoc` 都會解析為同一套
  `Schematic`／`Pcb`／`Library` 模型，因此每一項檢查、比對與報表都與格式無關——KiCad 是可寫入目標，Altium 為匯入。
- **可信賴的網路（net）推論。** 重建的 net 層能處理全域同名合併、junction、T 型接點與 No-ERC 標記
  ——修正了經典的「同名 net 被拆成單腳 net」的 bug。
- **對 Altium 唯讀、對 KiCad 安全寫入。** 離線時絕不修改 Altium 檔案；KiCad 的寫入會經過
  atomic 快照 → 暫存 → 驗證 → 取代的管線，並搭配純 Python 的連通性閘門。
- **免安裝 EDA。** 純標準函式庫的 OLE2/CFBF 與 Altium 記錄解碼，加上一個迭代式的 KiCad
  S-expression 解析器。不需要 Altium、不需要 KiCad、不需要任何編譯的擴充——只要 Python ≥ 3.11。
- **零執行期相依。** 僅使用標準函式庫（包含 `tomllib`）。易於 vendoring、沙箱化或在 CI 中執行。

## 從 op-list 寫入 KiCad 電路圖

`akcli` 會從帶版本的 JSON **op-list**（放置元件、wire、junction、label、power port、文字、
階層式 `add_sheet`、改名／刪除……；`connect_and_label`、`place_pwr_flag` 等連通性巨集會展開為核心 op）
寫出 KiCad 電路圖。`akcli new` 可先建立一張空白圖紙供繪製。寫入是精準且冪等的（確定性 UUIDv5），
由純 Python 的連通性驗證器 **加上寫入前後的 net diff** 把關，並需要明確的 `--apply`（預設為 dry run）。
`akcli undo` 可從輪替備份堆疊回復上一次寫入（`undo --list`／`--steps N`）。

```bash
akcli plan board.kicad_sch --ops ops.json         # 驗證 op-list，顯示變更內容 + net diff
akcli draw board.kicad_sch --ops ops.json         # 預設為 dry-run（不寫入檔案）
akcli draw board.kicad_sch --ops ops.json --apply --strict-nets  # atomic 寫入 + 驗證 + 備份；
                                                  # 拒絕拆分／合併具名 net 的寫入
```

`akcli relink-symbols board.kicad_sch` 可從新版 `.kicad_sym` 庫刷新過時的內嵌
`lib_symbols`，並以 net 等價安全閘門把關。Altium 的*寫入／繪製*僅能透過選用的
Windows 即時驅動（需執行中的 Altium 22+）；離線時，Altium 僅供分析。

## 執行檢查（ERC、power、pinmap、BOM、diff）

不必開啟任何 EDA 工具，即可執行電氣規則檢查（ERC）與其他設計檢查：

```bash
akcli check  main.SchDoc                          # ERC-lite + power + BOM + 連通性衛生檢查
akcli check  board.kicad_sch --intent intent.json # 斷言設計意圖 netlist 快照
akcli pinmap main.SchDoc -C akcli.toml # MCU pin -> net（+ 選用的預期對照表）
akcli diff   v1.SchDoc v2.SchDoc                   # 以 net 成員關係比對，而非以名稱比對
```

電源／接地偵測是**以 net 名稱 + power port 為基礎**，而非純粹以電氣型別判斷，因為
真實電路板大多是 `Passive` pin——只看型別的天真 ERC 會產生空洞的通過結果。每份報表都會
印出一段中繼資料標頭（passive-pin 比例、被抑制的 No-ERC 數量、未命名 net 數量、是否含有小數座標），
因此乾淨的結果絕不會被誤認為空結果。`--fail-on` 可調整以何種嚴重度作為非零退出的門檻
（`never` 一律退出 0），與檢查器無關的 `[[waiver]]` 設定表可依 code／refs 丟棄或降級 findings
（數量會顯示在標頭中）。設計意圖檔支援逐 net 模式與 `fnmatch` 萬用字元成員；已定位的 findings
會在 JSON／SARIF 中帶有 `pos`／`anchors`。

## 模擬並斷言

`akcli sim` 把電路圖轉成 SPICE deck，透過 KiCad 內建的 **libngspice**（在崩潰與逾時皆隔離的
子行程中）執行，並將 `.meas` 結果與你在 `sim.json` 裡宣告的 pass／fail 界限比對——斷言失敗即為
可在 CI 中把關的非零離開碼。元件透過先命中為準的階梯解析成 SPICE 元件（`Sim.*` KiCad 欄位 →
`models` 覆寫 → R／C／L 啟發式；無法建模的零件明確標為 `unmodeled`，絕不臆測）。未安裝 ngspice？
`--deck-only` 仍可輸出 deck。

```bash
akcli sim board.kicad_sch --deck-only                  # 只輸出 SPICE deck，不需引擎
akcli sim board.kicad_sch --sim board.sim.json         # 執行並斷言，失敗回傳 1
akcli sim board.kicad_sch --sim board.sim.json --sweep temp=0,25,60   # 角點矩陣
akcli sim fit-diode --point 0.37@20m --name DBAT       # datasheet 順向點 -> .model
```

引擎會自動探索（macOS／Linux／Windows 的 KiCad，或以 `AKCLI_NGSPICE` 指定）；`sim.json` 的界限
支援工程記號（`25m`、`4.7k`），單一條目同時給下界與上界即形成雙邊視窗；`--sweep` 會在角點矩陣上
重跑斷言；`--wave` 輸出整齊的 CSV；浮動節點會以 `.option rshunt` 自動修正。`akcli sim fit-diode`
可從 datasheet 順向電壓點擬合出二極體 `.model`，並可寫回電路圖（`--apply --write`），與
`jlc datasheet` 一起閉合「datasheet → model」迴圈。完整說明見 [docs/sim.md](../docs/sim.md)。

## 尋找 JLCPCB／LCSC 零件

`akcli jlc` 可搜尋 JLCPCB／LCSC 零件庫（庫存、價格階梯、Basic／Extended 狀態），並可將零件**行程內**轉換為 KiCad 庫（內嵌 MIT 授權的 [JLC2KiCadLib](https://github.com/TousstNicolas/JLC2KiCad_lib) 核心——無需安裝外部工具；見[致謝](#致謝)）。

```bash
akcli jlc search "0.1uF 0402 X7R"     # 關鍵字／MPN／分類搜尋（需網路）
akcli jlc show   C7593                 # 以 LCSC C-number 查單一零件
akcli jlc add    C2040 --3d            # LCSC 零件 → KiCad 符號＋封裝＋STEP
akcli jlc bom board.kicad_sch --qty 10 --csv order.csv   # 庫存／價格檢查 + JLCPCB 上傳用 CSV
akcli jlc datasheet board.kicad_sch --fetch              # 整份 BOM 的規格書 PDF 下載
```

## 工程計算器

`akcli calc` 內建 **60 個離線計算器**——E 系列取值與電阻組合搜尋（IEC 60063）、分壓器、
LM317／FB 穩壓最壞情況、IPC-2221 走線寬度與電氣間距、貫孔寄生參數、熔斷電流、AWG 線規、
微帶線／帶狀線阻抗、RF 衰減器、buck／boost 功率級、LDO 裕度、NE555、運算放大器增益、
比較器遲滯、包絡檢波器、I²C 上拉、晶振負載電容、熱設計、電池壽命、電阻標示碼、
電偶腐蝕相容性。**每筆結果都印出正式引用來源**
（公式出自的標準、datasheet 或教科書），數值並在測試中與 KiCad pcb_calculator 讀值及
已發表手冊數據交叉驗證。

```bash
akcli calc list                                  # 全部計算器（分組、含引用）
akcli calc rcombo target=1k series=E24           # 用現貨 E24 值合成 1 kΩ
akcli calc trackwidth i=2 dtemp=10               # IPC-2221：2 A 所需線寬
akcli calc i2c-pullup vdd=3.3 cb=100p mode=fast  # NXP UM10204 上拉電阻窗
```

輸入支援工程記號（`4k7`、`100n`、`2M2`）；`--json` 回傳
`{calc, inputs, results, reference}`、`--md` 輸出可直接貼的表格、`calc batch`
跑 JSON 工作清單、`--ops` 把設計結果（分壓器、穩壓回授、濾波器……）直接轉成
`place_component` op-list。`akcli view` 以單一伺服器同時提供 `/calc`
（即時運算表單、實體樣式 SVG 圖示、可分享連結、op-list 匯出）與 `/live`
（監看 `.kicad_sch` 的繪製時間軸，含逐步 ERC 發現、差異疊圖、SSE 推播），
僅綁定 localhost、零依賴。

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

## 匯入 Altium 設計

`akcli` 可直接開啟 Altium 二進位檔。它內含一個強化過的 OLE2/CFBF（Compound File Binary
Format）容器讀取器與 Altium 記錄解碼器——不需要 Altium Designer、不需要 Windows、不需要授權。

```bash
akcli read   main.SchDoc        # 將 .SchDoc 解析為正規化 JSON
akcli net    main.SchDoc         # 擷取 netlist（net -> pins）
akcli component main.SchDoc U10    # 單一元件的腳位 -> net（需給 designator）
```

支援的 Altium 輸入：`.SchDoc`（電路圖）、`.SchLib`（符號庫——文字記錄符號；含二進位符號記錄的庫會以 exit 5「不支援」拒絕）、`.PcbDoc`（電路板——支援
ASCII 的 `Nets6`／`Components6`／`Classes6`／`Rules6` 區段，**外加二進位銅箔區段**
`Tracks6`／`Vias6`／`Arcs6`／`Pads6`；`Fills6`／`Regions6`／`Texts6`／`Polygons6` 會被略過，
而不會誤解析）。所有 Altium *檔案*存取皆為**唯讀**（選用的 Windows live bridge 驅動的是*執行中的* Altium 實例）。

## 搭配 AI 程式代理使用

`akcli` 就是一個普通 CLI，只要它在 PATH 上，任何能執行 shell 指令的代理都能驅動它。指令以 `--json`
輸出結構化 JSON（`read` 與各項檢查帶有 `schema_version`；`net` 為陣列），op-list 帶有
`protocol_version`，因此輸出可保持機器可驗證且冪等。管線（`akcli … | head`）下 shell 回報的是管線的
exit code 而非 akcli 的——若要據此判斷請加 `set -o pipefail`。

- **Claude Code** — 安裝隨附的外掛（見下方），即可取得 `/akcli:circuit-review`、
  `circuit-pinmap`、`circuit-draw`、`circuit-diff` 指令與九個 skills：`akcli-circuit-design`（讀取／分析／
  繪製基礎）、`akcli-circuit-debug`（連線與工具除錯）、`akcli-schematic-review`（依嚴重度分級的設計審查）、
  `akcli-schematic-authoring`（用 op-list 從零設計電路）、`akcli-altium-interop`（與 Altium Designer 互通）、
  `akcli-parts-sourcing`（JLC／LCSC 零件選型）、`akcli-jlcpcb-capabilities`（JLCPCB 製程能力參考）、
  `akcli-design-calc`（`akcli calc` 的 60 個附標準引用的工程計算器）。
- **Codex** — 安裝隨附的外掛（見下方）：內含全部九個 skills 與 session hook；或把 skills 資料夾放進
  `.agents/skills/` 讓其自動探索。見 [docs/codex-plugin.md](../docs/codex-plugin.md)。
- **OpenCode** — 會自動探索隨附的 skills；把它們放進各自的 skills 目錄，
  並讓代理透過 shell 呼叫 `akcli`。指令與一鍵設定 prompt 見 [INSTALL.md](../INSTALL.md#use-with-ai-coding-agents)。

原生 MCP server 仍在[路線圖](#路線圖)中。

## 安裝

尚未發佈到 PyPI——請從原始碼安裝。零執行期相依，需要 **Python ≥ 3.11**（用於標準函式庫 `tomllib`）：

```bash
# 從 clone 直接執行，免安裝
git clone https://github.com/tipoLi5890/akcli
./akcli/bin/akcli --help        # wrapper 會自動選用 Python ≥ 3.11

# 或以 pipx 把 CLI 裝到 PATH 上
pipx install git+https://github.com/tipoLi5890/akcli
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

## 路線圖

目前已提供（v0.6.x）：KiCad 寫入／繪製（18 種 op + 9 種巨集，含階層 `add_sheet`、net-diff
安全護欄、`new`／多層 `undo`，輸出經 KiCad 自身 netlister 仲裁）、ERC/power/BOM/diff/pinmap/
**intent** 檢查（含 waiver 與 SARIF）、**`akcli sim`**（KiCad 自帶 ngspice 上的 SPICE deck、
斷言、角點掃描、規格書擬合模型）、JLCPCB／LCSC 零件搜尋 + BOM 可購性 + **規格書抓取**、
60 個附標準引用的計算器、`view` 儀表板，以及版本容忍的 Altium／KiCad 讀取器（KiCad 階層、
Altium 多 sheet + 二進位銅箔）。前瞻計畫（v0.8 → v1.0，各里程碑附驗收條件）見
**[ROADMAP.md](../ROADMAP.md)**。重點待開發項目：

- `check`／`diff`／`pinmap` findings 的正式 JSON Schema；查無結果的機器可判別化。
- 完整 **ERC pin 型別衝突矩陣**與 schematic-vs-PCB 同步檢查。
- 純標準函式庫的 **SVG 電路圖渲染**與自動產生的 pinout book。
- 為電路圖 PR 把關的 GitHub **Action**（check + diff + intent + sim 斷言）。
- *選配、依需求推進：* Altium 軌道——二進位 `.SchLib` 解碼器、其餘 `.PcbDoc` 區段、
  Windows **即時驅動**（scaffold 待驗證）。
- 原生 **MCP server**（依決策暫緩；目前代理直接驅動 CLI）。

---

## 致謝

`akcli jlc` 建構於以下開源專案之上（完整出處與授權條款見
[ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) 與 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)）：

- **JLC2KiCadLib**，作者 **TousstNicolas**（MIT）——LCSC → KiCad 轉換核心，以 vendored 方式內嵌（見 THIRD_PARTY_NOTICES）。
- **jlcsearch**（tscircuit，MIT）與 **jlcparts**（MIT）——零件搜尋後端。
- **EasyEDA／LCSC／JLCPCB**——元件資料來源。

---

## 聯絡方式

問題、bug 或功能請求：請[開一個 GitHub issue](https://github.com/tipoLi5890/akcli/issues)。

---

## 授權

MIT © 2026 Li, ching yu。詳見 [LICENSE](../LICENSE)；第三方出處標註見
[ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) 與 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)，
安全模型見 [SECURITY.md](../SECURITY.md)。
