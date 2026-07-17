[English](../README.md) · **繁體中文** · [简体中文](README.zh-Hans.md)

# akcli

**akcli**（CLI 指令 `akcli`，匯入套件名 `akcli`）是一套零相依的
**KiCad 原生 AI 設計代理**——Python 工具包與 Claude Code 外掛，讓 AI 代理能在**未安裝 Altium 或
KiCad** 的情況下：從 JSON op-list **繪製與編輯** `.kicad_sch`（含 net-diff 安全防護欄與一鍵 undo）、
執行 ERC／設計／**intent／contract**／BOM 檢查、**驗證原理圖 ↔ PCB 等價性**、**稽核並修復專案
料庫工作區**、**依版本化的 fab profile 把關製造**、**在 KiCad 自帶的 ngspice 上模擬**、查找實體料件
與抓取規格書，並**匯入 Altium `.SchDoc` / `.SchLib` / `.PcbDoc` / `.PcbLib`**。

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
  `akcli capabilities` 會在單一 JSON 文件中自我描述整套 CLI 介面（含事先公開的硬性
  op 詞彙限制）；`read`／`nets` 支援 `--match`／`--limit`（另有 `read --summary`），
  讓大板子的輸出能控制在代理的 context 預算內；工作區的寫入日誌（`akcli log`）讓多步驟
  session 具備狀態；**每一個**錯誤碼都帶有機器可讀的 `remediation` 修復提示，搭配 `--json`
  時每一條失敗路徑都仍會輸出可解析的 JSON；`akcli render` 則能畫出免安裝的 SVG，讓多模態代理
  「看得到」自己剛放置的內容。
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
- **端到端設計完整性。** 超越 ERC：設計**契約**（require/forbid pin-net 與 pin-pair 拓撲規則，
  附帶 datasheet 佐證）、原理圖 ↔ PCB **等價驗證**、專案**料庫工作區**稽核／修復（過去得靠手動
  `sed` 才能處理的 footprint-nickname 與 3D 路徑陷阱）、版本化的 **fab profile**（free-via
  envelope、tenting、成本門檻），以及會把關每一項檢查並寫出可追溯 manifest 的
  **release preflight**——見 [docs/design-integrity.md](../docs/design-integrity.md)。
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

有兩種編輯在結構上就是**net 保留**的：`move_component` 可以連同符號的 net label 與
wire 端點一起搬移（`carry_labels`／`carry_wires`），而 `arrange` 就建立在這個原語之上——
`arrange board.kicad_sch --apply` 會微調自由（未接線）的符號，直到互不重疊；
`arrange --groups`（可帶 `group-name → [refdes]` 對照表，或不帶檔案直接從圖面的
`Group` 屬性推導）則會把整個功能區塊當成剛性整體搬動，`--frames` 打包後順帶更新模組邊框。

**模組化繪圖是一級公民**：op-list 可宣告功能群組
（`"groups": {"POWER": {"origin": [1000, 1000], "title": "電源模組"}}`）並在 op 上標
`"group"` — 座標即為群組內相對座標（搬整個模組＝只改一個 origin），巨集自動繼承標籤，
成員關係以隱藏的 `Group` 屬性存進圖面。`akcli groups board.kicad_sch` 列出所有模組；
`--frame --apply` 為每個群組畫出可自我更新的邊框＋標題。相對擺放
（`"anchor": "U1.VCC"` ＋ `offset_mil`）、`place_array` 陣列、`route_net` 避開 pin 的
L/Z 自動走線、`akcli bbox` 佔位查詢，以及 `plan --render preview.svg`
（**apply 前先看圖**，含世界座標網格）補齊整條流程。

`akcli library check-lock hardware/kicad/board` 會回報哪些檔案
正被 KiCad GUI 開啟中（有的話 exit 6），讓外部自動化能在寫入前先把關。

## 執行檢查（ERC、power、pinmap、BOM、diff）

不必開啟任何 EDA 工具，即可執行電氣規則檢查（ERC）與其他設計檢查：

```bash
akcli check  main.SchDoc                          # ERC-lite + power + BOM + 連通性衛生檢查
akcli check  board.kicad_sch --intent intent.json # 斷言設計意圖 netlist 快照
akcli check  board.kicad_sch --contract board.contract.toml  # require/forbid 拓撲規則
akcli verify board.kicad_sch board.kicad_pcb      # 原理圖 <-> PCB 等價
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

## 設計審查（advisory）

`akcli review` 是建立在同一套正規化模型上的 advisory（僅供參考、不強制阻擋）工程設計審查
引擎，因此它審查 Altium `.SchDoc` 與審查 `.kicad_sch` 一樣容易。`review analyze` 會執行六個
偵測家族——**signal**（分壓器、回授 Vref 合理性、RC 轉角、晶振負載、運放增益、connector
ESD）、**validation**（I²C 上拉窗、跨電壓域訊號、浮接 enable）、**pcb**（以 union-find 找出
未繞線銅箔、去耦電容距離、散熱過孔、IPC-2221 走線安培容量）、**emc**（預先合規風險：電源／
接地平面、stitching、edge／clock 繞線、差動對 skew、TVS 放置）、**domain**（USB-C CC
終端）、以及 **gerber**（fab 輸出的完整性／對位／過期檢查）——並輸出**依信心分級**的
findings（`deterministic`／`heuristic`／`datasheet_backed`／`llm_reviewed`），附帶已發布為
`findings.schema.json` 的證據封裝。

```bash
akcli review analyze board.kicad_sch --profile deep --pcb board.kicad_pcb --gerbers fab/  # advisory：預設 exit 0，除非設定 --fail-on
akcli review explain REVIEW_FB_DIVIDER_VREF_MISMATCH    # 規則的規格、公式與引用來源
akcli review facts add TPS61023 --pdf datasheets/tps61023.pdf --set vref=0.6V@5   # 附稽核軌跡的 datasheet 事實
akcli review tree board.kicad_sch                       # power tree：電源軌 -> 穩壓器 -> 負載
akcli review propose review.findings.json --out proposals.json   # findings -> op-list／contract／sim 草稿
```

它**預設為 advisory**（無論找到什麼都 exit 0）；`--fail-on warning|error|critical` 可讓 CI 選擇
把關。倚賴 datasheet 數值的 findings 會附上該 PDF 的 sha256 與頁碼（**facts store**），
`review propose` 會把修正方案（依 E-series 取整）重新計算成 op-list 草稿，並照常經過
`plan → draw` 的安全護欄——絕不直接改動檔案。`review validate` 會用四項確定性檢查
（schema／anchor 是否存在／datasheet 佐證／規則偽裝）把關 LLM 產生的候選，把不合格的隔離。
review finding 唯一能阻擋 release 的路徑，是明確、經過校準的 `release preflight
--review-policy` 允許清單。完整規則目錄與擷取／深度審查／把關 skills 見
[docs/review-rules.md](../docs/review-rules.md)。

## 設計完整性：料庫、契約、fab、release

在單檔 ERC 之外，`akcli` 把整個設計視為一個可稽核的整體——料庫工作區、原理圖 ↔ PCB 的關係、
以 datasheet 為依據的拓撲規則，以及製造政策：

```bash
akcli library audit hardware/kicad/board
akcli library repair hardware/kicad/board --rename-footprint-lib footprint=proj_jlc --apply
akcli library import-altium vendor.PcbLib --out vendor.pretty --courtyard 0.25 --apply
akcli check   board.kicad_sch --contract board.contract.toml
akcli fab     check board.kicad_pcb --profile jlc-4l-1oz.toml --order order.toml
akcli release preflight --sch board.kicad_sch --pcb board.kicad_pcb --fab-profile jlc-4l-1oz.toml --gerbers fab/ --out manifest.json
```

`library audit`／`repair` 會抓出並修復過去得靠手動 `sed` 處理的 footprint-nickname 與 3D 路徑
陷阱；**契約（contract）** 能表達 ERC 表達不了的 datasheet 規則，並支援附 owner 與到期日的核准
例外；**fab profile** 是版本化、附來源引用的供應商政策（free-via envelope、tenting、via-in-pad、
成本門檻），並會依宣告的訂單 manifest 驗證，而非從 PCB 猜測；**`release preflight`** 會執行每一項
把關（check／intent／contract／library／sch-pcb／fab／order／**review-policy**／**gerber**／git），
並寫出一份綁定輸入雜湊、git 版本與各項把關發現的 manifest；`--review-policy` TOML 允許清單是
advisory review finding 唯一能阻擋 release 的途徑，`--gerbers` 則會加上 fab 輸出的完整性／
對位／過期檢查。當 KiCad GUI 開著檔案時，KiCad 寫入會拒絕並回報
`TARGET_LOCKED`（可用 `--allow-open` 覆寫，之後在 KiCad 執行 File→Revert），`akcli library
check-lock <dir>` 則讓外部自動化能查詢同一把鎖。完整指南見
[docs/design-integrity.md](../docs/design-integrity.md)。

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
而不會誤解析），以及 **`.PcbLib`**（封裝庫——每個 footprint 的 pad 會被解碼進
`FootprintDef` 模型；未解碼的圖形／文字／3D 會以 `UNSUPPORTED_PRIMITIVE` 警告呈現，絕不會被丟棄）。
格式偵測採**快速失敗（fail-loud）**：無法辨識的 OLE2 容器會依其儲存結構分類並以 exit `5`
結束，而不會被誤讀成一份空的電路圖；`read --strict` 則會把 `EMPTY_IMPORT`（來源非空卻正規化為空）
轉為 exit `1`。所有 Altium *檔案*存取皆為**唯讀**（選用的 Windows live bridge 驅動的是*執行中的*
Altium 實例）。

## 搭配 AI 程式代理使用

`akcli` 就是一個普通 CLI，只要它在 PATH 上，任何能執行 shell 指令的代理都能驅動它。指令以 `--json`
輸出結構化 JSON（`read` 與各項檢查帶有 `schema_version`；`net` 為陣列），op-list 帶有
`protocol_version`，因此輸出可保持機器可驗證且冪等。管線（`akcli … | head`）下 shell 回報的是管線的
exit code 而非 akcli 的——若要據此判斷請加 `set -o pipefail`。

- **Claude Code** — 安裝隨附的外掛（見下方），即可取得 `/akcli:circuit-review`、
  `circuit-pinmap`、`circuit-draw`、`circuit-diff`、`circuit-parts` 指令與十二個 skills：`akcli-circuit-design`（讀取／分析／
  繪製基礎）、`akcli-circuit-debug`（連線與工具除錯）、`akcli-schematic-review`（依嚴重度分級的設計審查）、
  `akcli-schematic-authoring`（用 op-list 從零設計電路）、`akcli-altium-interop`（與 Altium Designer 互通）、
  `akcli-parts-sourcing`（JLC／LCSC 零件選型）、`akcli-jlcpcb-capabilities`（JLCPCB 製程能力參考）、
  `akcli-design-calc`（`akcli calc` 的 60 個附標準引用的工程計算器）、`akcli-setup`（環境探測與修復）、`akcli-datasheet-facts`（資料表事實擷取）、`akcli-deep-review`（LLM 候選經 review validate 把關）、`akcli-release-gating`（preflight 與校準過的放行政策）。
- **Codex** — 安裝隨附的外掛（見下方）：內含全部十二個 skills 與 session hook；或把 skills 資料夾放進
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

目前已提供（v0.10.x）：KiCad 寫入／繪製（22 種 op + 10 種巨集，含階層 `add_sheet`、net-diff
安全護欄、`new`／多層 `undo`，輸出經 KiCad 自身 netlister 仲裁）、net 保留的 **`arrange
--groups`**／`move_component` carry re-layout、advisory 的 **`akcli review`** 引擎
（跨 signal／validation／pcb／emc／domain／gerber 偵測家族的 analyze、datasheet **facts**
store、`propose`／`diff`／`tree`、`validate`，以及 `release --review-policy` 把關）、
ERC/power/BOM/diff/pinmap/**intent**／**contract** 檢查（含 waiver 與 SARIF）、原理圖 ↔ PCB
**`verify`**、專案 **`library`** 工作區（audit／repair／import-altium／**check-lock**——
Altium `.PcbLib` footprint 匯入 + 深度 `.kicad_pcb` + **gerber** 讀取）、版本化的 **`fab`**
profile，以及 **`release preflight`** 把關
（見 [docs/design-integrity.md](../docs/design-integrity.md)）、**`akcli sim`**（KiCad 自帶
ngspice 上的 SPICE deck、斷言、角點掃描、規格書擬合模型）、JLCPCB／LCSC 零件搜尋 + BOM 可購性 +
**規格書抓取**、60 個附標準引用的計算器、`view` 儀表板，以及版本容忍的 Altium／KiCad 讀取器
（KiCad 階層、Altium 多 sheet + 二進位銅箔）。前瞻計畫（v0.8 → v1.0，各里程碑附驗收條件）見
**[ROADMAP.md](../ROADMAP.md)**。重點待開發項目：

- `check`／`diff`／`pinmap` findings 的正式 JSON Schema；查無結果的機器可判別化。
- 完整 **ERC pin 型別衝突矩陣**（schematic-vs-PCB 同步檢查現已以 `akcli verify` 形式提供）。
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
