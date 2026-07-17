[English](../README.md) · [繁體中文](README.zh-Hant.md) · **简体中文**

# akcli

**akcli**（CLI 命令 `akcli`，导入包 `akcli`）是一套零依赖的
**KiCad 原生 AI 设计代理**——Python 工具包与 Claude Code 插件，让 AI 代理能在**未安装 Altium 或
KiCad** 的情况下：依据 JSON 操作列表（op-list）**绘制与编辑** `.kicad_sch`（含 net-diff 安全护栏与一键
undo）、运行 ERC／设计／**intent／contract**／BOM 检查、**验证原理图 ↔ PCB 等价性**、**审计并修复
项目库工作区**、**依版本化的 fab profile 把关制造**、**在 KiCad 自带的 ngspice 上仿真**、查找实体
料件与抓取规格书，并**导入 Altium `.SchDoc` / `.SchLib` / `.PcbDoc` / `.PcbLib`**。

**KiCad 是可写入的目标**；Altium 文件则被*导入*同一个归一化模型以供分析（可选的 Windows
*live bridge* 也能驱动运行中的 Altium 实例）。成果是一套可脚本化、免安装的设计闭环——从导入的
既有原理图或一张空白图纸起步，一路走到经过仿真、选好料件、可下单的板子——由自动化流水线或 AI 代理端到端驱动。

[![CI](https://github.com/tipoLi5890/akcli/actions/workflows/ci.yml/badge.svg)](https://github.com/tipoLi5890/akcli/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 亮点

- **AI 代理原生。** 以 Claude Code 插件形式发布，附带 skills/commands，输出带有
  `schema_version` 的结构化 JSON，并接受带版本号的 op-list 以实现确定性、幂等的编辑。
  `akcli capabilities` 用一份 JSON 文档自描述整个 CLI 能力面（包含预先公开的硬性 op 词汇约束）；
  `read`/`nets` 支持 `--match`/`--limit`（以及 `read --summary`），把大型电路板的输出压缩在
  代理的上下文预算之内；工作区写入日志（`akcli log`）让多步会话可追溯状态；**每个**错误码都
  携带机器可读的 `remediation` 提示，且加 `--json` 后每条失败路径都仍会输出可解析的 JSON；
  `akcli render` 可绘制免安装的 SVG，让多模态代理能「看到」自己刚放置的内容。
- **net-diff 安全护栏。** 每次 `plan`/`draw` 都会打印写入前后的 **net 连通性差异**
  （拆分、合并、改名——按引脚成员关系匹配，绝不按名称匹配）；`draw --apply --strict-nets`
  会拒绝任何拆分或合并具名 net 的写入，`akcli check --intent` 则可在任何编辑后
  断言一份设计意图（design-intent）网表快照。
- **仿真并断言。** `akcli sim` 将原理图转成 SPICE deck，通过 KiCad 的 libngspice 在崩溃隔离的
  子进程中运行，把 `.meas` 结果转为可在 CI 中把关的 pass/fail 发现——若未安装引擎，可用
  `--deck-only` 只输出 deck。
- **附标准引用的计算器。** `akcli calc` 回答 60 种设计计算（E 系列、IPC-2221、过孔寄生参数、
  I²C 上拉、buck/boost……），每条结果都打印正式的引用来源。
- **一个归一化模型。** KiCad `.kicad_sch` 与 Altium 二进制 `.SchDoc` 都会解析为同一个
  `Schematic`/`Pcb`/`Library` 模型，因此每一项检查、比对和报告都与格式无关——KiCad 是可写入目标，Altium 为导入。
- **端到端设计完整性。** 超越 ERC：设计**契约（contract）**（require/forbid 引脚-网络与
  引脚对拓扑规则，附带 datasheet 佐证）、原理图 ↔ PCB **等价验证**、项目**库工作区**审计／修复
  （过去得靠手动 `sed` 才能处理的 footprint-nickname 与 3D 路径陷阱）、版本化的 **fab profile**
  （free-via envelope、tenting、成本阈值），以及会把关每一项检查并写出可追溯 manifest 的
  **release preflight**——见 [docs/design-integrity.md](../docs/design-integrity.md)。
- **值得信赖的网络（net）推断。** 重建的 net 层可处理全局同名合并、连接点（junction）、
  T 型连接点和 No-ERC 标记——修复了经典的「同名 net 被拆成单引脚 net」缺陷。
- **对 Altium 只读、对 KiCad 安全写入。** Altium 文件在离线状态下永不被修改；KiCad 写入会经过
  原子化的 快照 → 临时文件 → 校验 → 替换 流水线，并由纯 Python 的连通性闸门把关。
- **无需安装 EDA。** 纯标准库实现的 OLE2/CFBF + Altium 记录解码，以及一个迭代式的 KiCad
  S-expression 解析器。无需 Altium、无需 KiCad、无需编译扩展——只要 Python ≥ 3.11。
- **零运行时依赖。** 仅使用标准库（包括 `tomllib`）。易于内嵌（vendor）、沙箱化或在 CI 中运行。

## 依据 op-list 写入 KiCad 原理图

`akcli` 依据带版本号的 JSON **op-list**（放置元件、导线、连接点、标签、power 端口、文本、
层次化 `add_sheet`、改名/删除……；`connect_and_label`、`place_pwr_flag` 等连通性宏会展开为核心 op）
写入 KiCad 原理图。`akcli new` 可先创建一张空白图纸供绘制。写入是精准且幂等的（确定性 UUIDv5），
由纯 Python 连通性校验器 **加上写入前后的 net diff** 把关，并要求显式 `--apply`（默认为 dry run）。
`akcli undo` 可从轮替备份栈回退上一次写入（`undo --list`／`--steps N`）。

```bash
akcli plan board.kicad_sch --ops ops.json         # 校验 op-list，显示改动内容 + net diff
akcli draw board.kicad_sch --ops ops.json         # 默认 dry-run（不写入文件）
akcli draw board.kicad_sch --ops ops.json --apply --strict-nets  # 原子写入 + 校验 + 备份；
                                                  # 拒绝拆分/合并具名 net 的写入
```

`akcli relink-symbols board.kicad_sch` 可从新版 `.kicad_sym` 库刷新过时的内嵌
`lib_symbols`，并由 net 等价安全闸门把关。Altium 的*写入/绘制*仅通过可选的
Windows live driver（需运行 Altium 22+）提供；离线状态下，Altium 仅支持分析。

有两类编辑在结构上**天然保持 net 不变**：`move_component` 可以在移动一个符号时一并携带它的
net 标签与导线端点（`carry_labels`/`carry_wires`）；`arrange` 就建立在这个原语之上——
`arrange board.kicad_sch --apply` 会把未接线的自由符号相互推开直到不再重叠，
`arrange --groups`（可带 `group-name → [refdes]` 映射文件，或不带文件直接从图面的
`Group` 属性推导）则会把整个功能模块当成刚性整体搬动，`--frames` 打包后顺带刷新模块边框。

**模块化绘图是一等公民**：op-list 可声明功能分组
（`"groups": {"POWER": {"origin": [1000, 1000], "title": "电源模块"}}`）并在 op 上标
`"group"` — 坐标即为组内相对坐标（搬整个模块＝只改一个 origin），宏自动继承标签，
成员关系以隐藏的 `Group` 属性存进图面。`akcli groups board.kicad_sch` 列出所有模块；
`--frame --apply` 为每个分组画出可自我刷新的边框＋标题。相对放置
（`"anchor": "U1.VCC"` ＋ `offset_mil`）、`place_array` 阵列、`route_net` 避开引脚的
L/Z 自动走线、`akcli bbox` 占位查询，以及 `plan --render preview.svg`
（**apply 前先看图**，含世界坐标网格）补齐整条流程。

`akcli library check-lock hardware/kicad/board` 会报告哪些文件正被 KiCad GUI 打开
（有的话 exit 6），让外部自动化能在写入前把关。

## 运行检查（ERC、power、pinmap、BOM、diff）

无需打开任何 EDA 工具即可运行电气规则检查（ERC）及其他设计检查：

```bash
akcli check  main.SchDoc                          # ERC-lite + power + BOM + 连通性卫生检查
akcli check  board.kicad_sch --intent intent.json # 断言设计意图网表快照
akcli check  board.kicad_sch --contract board.contract.toml  # require/forbid 拓扑规则
akcli verify board.kicad_sch board.kicad_pcb      # 原理图 <-> PCB 等价
akcli pinmap main.SchDoc -C akcli.toml # MCU 引脚 -> net（+ 可选的预期表）
akcli diff   v1.SchDoc v2.SchDoc                   # 基于 net 成员关系的 diff，而非基于名称
```

power/ground 检测是**基于 net 名称 + power 端口（power-port）**的，而非纯粹基于电气类型，
因为真实电路板上以 `Passive` 引脚为主——一个仅看类型的朴素 ERC 会产生空洞的「通过」结果。
每份报告都会打印一个元数据头（passive 引脚占比、被抑制的 No-ERC 数量、未命名 net 数量、
分数坐标的存在情况），因此一个干净的结果永远不会被误认为空结果。`--fail-on` 可调整以何种严重度
作为非零退出的门槛（`never` 始终退出 0），与检查器无关的 `[[waiver]]` 配置表可按 code／refs
丢弃或降级 findings（数量会显示在元数据头中）。设计意图文件支持逐 net 模式与 `fnmatch` 通配符成员；
已定位的 findings 会在 JSON／SARIF 中携带 `pos`／`anchors`。

## 设计评审（advisory）

`akcli review` 是构建在同一套归一化模型上的顾问式工程设计评审引擎，因此它评审
Altium `.SchDoc` 与评审 `.kicad_sch` 一样顺畅。`review analyze` 会运行六大检测族——
**signal**（分压器、反馈 Vref 合理性、RC 转折点、晶振负载、运放增益、连接器 ESD）、
**validation**（I²C 上拉窗口、跨电压域信号、悬空使能）、**pcb**（基于并查集的未布线铜箔、
去耦电容距离、散热过孔、IPC-2221 载流量）、**emc**（预合规风险：地平面、接地缝合、
边缘/时钟走线、差分对偏斜、TVS 位置）、**domain**（USB-C CC 端接）以及 **gerber**
（fab 输出包的完整性/对齐/陈旧度）——并输出**分级置信度**的 findings
（`deterministic`/`heuristic`/`datasheet_backed`/`llm_reviewed`），附带一份发布为
`findings.schema.json` 的证据信封（evidence envelope）。

```bash
akcli review analyze board.kicad_sch --profile deep --pcb board.kicad_pcb --gerbers fab/  # advisory：除非 --fail-on，否则退出 0
akcli review explain REVIEW_FB_DIVIDER_VREF_MISMATCH    # 一条规则的规格、公式与引用来源
akcli review facts add TPS61023 --pdf datasheets/tps61023.pdf --set vref=0.6V@5   # 经审计的 datasheet 事实
akcli review tree board.kicad_sch                       # 电源树：rails -> 稳压器 -> 负载
akcli review propose review.findings.json --out proposals.json   # findings -> op-list/contract/sim 草案
```

它**默认为 advisory**（无论发现什么都退出 0）；`--fail-on warning|error|critical` 可让某个
CI job 选择把关。依赖 datasheet 数值的 findings 会引用该 PDF 的 sha256 与页码（**事实存储**，
facts store）；`review propose` 会把修复方案（经 E 系列吸附）重新计算为 op-list 草案，
再走回正常的 `plan → draw` 安全护栏——绝不直接改文件。`review validate` 会用四道确定性检查
（schema／anchor 是否存在／datasheet 佐证／规则冒充）把关 LLM 生成的候选项，未通过者会被隔离。
评审发现能够阻塞发布的**唯一**路径，是显式、经过校准的 `release preflight --review-policy`
白名单。完整规则目录以及提取/深度评审/放行 skills 见
[docs/review-rules.md](../docs/review-rules.md)。

## 设计完整性：库、契约、fab、release

在单文件 ERC 之外，`akcli` 把整个设计当作一个可审计的整体——库工作区、原理图 ↔ PCB 的关系、
基于 datasheet 的拓扑规则，以及制造策略：

```bash
akcli library audit hardware/kicad/board
akcli library repair hardware/kicad/board --rename-footprint-lib footprint=proj_jlc --apply
akcli library import-altium vendor.PcbLib --out vendor.pretty --courtyard 0.25 --apply
akcli check   board.kicad_sch --contract board.contract.toml
akcli fab     check board.kicad_pcb --profile jlc-4l-1oz.toml --order order.toml
akcli release preflight --sch board.kicad_sch --pcb board.kicad_pcb --fab-profile jlc-4l-1oz.toml --gerbers fab/ --out manifest.json
```

`library audit`/`repair` 会找出并修复过去只能靠手动 `sed` 处理的 footprint-nickname 与 3D 路径
陷阱；**契约（contract）** 能表达 ERC 表达不了的 datasheet 规则，并支持带 owner 与到期日的批准
例外；**fab profile** 是版本化、附来源引用的供应商策略（free-via envelope、tenting、via-in-pad、
成本阈值），并会依据声明的订单 manifest 校验，而不是从 PCB 猜测；**`release preflight`** 会运行
每一道关卡（check/intent/contract/library/sch-pcb/fab/order/**review-policy**/**gerber**/git），
并写出一份绑定输入哈希、git 版本以及各关卡结果的 manifest。`--review-policy` TOML 白名单是唯一
能让顾问式评审发现阻塞发布的方式；`--gerbers` 会加上 fab 输出的完整性/对位/陈旧度检查。当 KiCad
GUI 打开着文件时，KiCad 写入会拒绝并报 `TARGET_LOCKED`（可用 `--allow-open` 覆盖，之后在 KiCad
里执行 File→Revert），`akcli library check-lock <dir>` 则让外部自动化查询同一份锁状态。完整指南见
[docs/design-integrity.md](../docs/design-integrity.md)。

## 仿真并断言

`akcli sim` 把原理图转成 SPICE deck，通过 KiCad 内置的 **libngspice**（在崩溃与超时均隔离的
子进程中）运行，并将 `.meas` 结果与你在 `sim.json` 中声明的 pass/fail 界限比对——断言失败即为
可在 CI 中把关的非零退出码。元件通过先命中为准的阶梯解析成 SPICE 元件（`Sim.*` KiCad 字段 →
`models` 覆盖 → R/C/L 启发式；无法建模的元件明确标为 `unmodeled`，绝不臆测）。未安装 ngspice？
`--deck-only` 仍可输出 deck。

```bash
akcli sim board.kicad_sch --deck-only                  # 只输出 SPICE deck，无需引擎
akcli sim board.kicad_sch --sim board.sim.json         # 运行并断言，失败返回 1
akcli sim board.kicad_sch --sim board.sim.json --sweep temp=0,25,60   # 角点矩阵
akcli sim fit-diode --point 0.37@20m --name DBAT       # datasheet 正向点 -> .model
```

引擎会自动发现（macOS/Linux/Windows 的 KiCad，或用 `AKCLI_NGSPICE` 指定）；`sim.json` 的界限
支持工程记号（`25m`、`4.7k`），单个条目同时给下界与上界即形成双边窗口；`--sweep` 会在角点矩阵上
重跑断言；`--wave` 输出整齐的 CSV；浮动节点会用 `.option rshunt` 自动修正。`akcli sim fit-diode`
可从 datasheet 正向电压点拟合出二极管 `.model`，并可写回原理图（`--apply --write`），与
`jlc datasheet` 一起闭合“datasheet → model”回环。完整说明见 [docs/sim.md](../docs/sim.md)。

## 查找 JLCPCB / LCSC 元件

`akcli jlc` 可搜索 JLCPCB / LCSC 元件库（库存、价格阶梯、Basic/Extended 状态），并可将元件**进程内**转换为 KiCad 库（内嵌 MIT 许可的 [JLC2KiCadLib](https://github.com/TousstNicolas/JLC2KiCad_lib) 核心——无需安装外部工具；见[致谢](#致谢)）。

```bash
akcli jlc search "0.1uF 0402 X7R"     # 关键字 / MPN / 分类搜索（需联网）
akcli jlc show   C7593                 # 按 LCSC C-number 查单个元件
akcli jlc add    C2040 --3d            # LCSC 元件 → KiCad 符号＋封装＋STEP
akcli jlc bom board.kicad_sch --qty 10 --csv order.csv   # 库存/价格检查 + JLCPCB 上传用 CSV
akcli jlc datasheet board.kicad_sch --fetch              # 整份 BOM 的规格书 PDF 下载
```

## 工程计算器

`akcli calc` 内置 **60 个离线计算器**——E 系列取值与电阻组合搜索（IEC 60063）、分压器、
LM317/FB 稳压最坏情况、IPC-2221 走线宽度与电气间距、过孔寄生参数、熔断电流、AWG 线规、
微带线/带状线阻抗、RF 衰减器、buck/boost 功率级、LDO 裕量、NE555、运放增益、比较器迟滞、
包络检波器、I²C 上拉、晶振负载电容、热设计、电池寿命、电阻标记码、电偶腐蚀兼容性。**每条结果都打印正式引用来源**（公式出自的
标准、datasheet 或教科书），数值并在测试中与 KiCad pcb_calculator 读数及已发表手册数据
交叉验证。

```bash
akcli calc list                                  # 全部计算器（分组、含引用）
akcli calc rcombo target=1k series=E24           # 用现货 E24 值合成 1 kΩ
akcli calc trackwidth i=2 dtemp=10               # IPC-2221：2 A 所需线宽
akcli calc i2c-pullup vdd=3.3 cb=100p mode=fast  # NXP UM10204 上拉电阻窗
```

输入支持工程记号（`4k7`、`100n`、`2M2`）；`--json` 返回
`{calc, inputs, results, reference}`、`--md` 输出可直接粘贴的表格、`calc batch`
跑 JSON 作业清单、`--ops` 把设计结果（分压器、稳压反馈、滤波器……）直接转成
`place_component` op-list。`akcli view` 以单一服务器同时提供 `/calc`
（即时运算表单、实体样式 SVG 图示、可分享链接、op-list 导出）与 `/live`
（监看 `.kicad_sch` 的绘制时间轴，含逐步 ERC 发现、差异叠图、SSE 推送），
仅绑定 localhost、零依赖。

## 读取 KiCad 文件

同一个 CLI 用一个显式栈（非递归）的分词器解析 KiCad 的 S-expression 格式，
该分词器对深度、原子（atom）和节点都设有上限——因此格式错误或恶意构造的文件无法撑爆调用栈。

```bash
akcli read board.kicad_sch              # .kicad_sch -> 归一化 JSON
akcli net  board.kicad_sch              # net 成员关系，共用 net 引擎
```

KiCad 的引脚电气类型在读取时从 `lib_symbols` 解析得到（实例引脚不携带类型），
因此 ERC 拥有所需数据。S-expression 读取器与版本无关——KiCad 7/8 有测试 fixture 覆盖，
较新格式（9/10）也走同一解析路径。

## 导入 Altium 设计

`akcli` 直接打开 Altium 二进制文件。它内含一个加固的 OLE2/CFBF（复合文件二进制格式，
Compound File Binary Format）容器读取器以及一个 Altium 记录解码器——无需 Altium Designer、
无需 Windows、无需许可证。

```bash
akcli read   main.SchDoc        # 将 .SchDoc 解析为归一化 JSON
akcli net    main.SchDoc         # 提取网表（net -> pins）
akcli component main.SchDoc U10    # 单个元件的引脚 -> net（需给 designator）
```

支持的 Altium 输入：`.SchDoc`（原理图）、`.SchLib`（符号库——文本记录符号；含二进制符号记录的库会以 exit 5「不支持」拒绝）、`.PcbDoc`（电路板——
支持 ASCII 的 `Nets6`/`Components6`/`Classes6`/`Rules6` 段，**外加二进制铜箔段**
`Tracks6`/`Vias6`/`Arcs6`/`Pads6`；`Fills6`/`Regions6`/`Texts6`/`Polygons6` 会被跳过，
而非误解析），以及 **`.PcbLib`**（封装库——每个 footprint 的焊盘会被解码进
`FootprintDef` 模型；未解码的图形/文本/3D 会以 `UNSUPPORTED_PRIMITIVE` 警告呈现，绝不会被丢弃）。
格式检测采用**快速失败（fail-loud）**：无法识别的 OLE2 容器会依其存储结构分类并以 exit `5`
退出，而不会被误读成一份空的原理图；`read --strict` 则会把 `EMPTY_IMPORT`（源文件非空但归一化
后为空）转为 exit `1`。所有 Altium *文件*访问均为**只读**（可选的 Windows live bridge 驱动的是
*运行中的* Altium 实例）。

## 与 AI 编码代理一起使用

`akcli` 就是一个普通 CLI，只要它在 PATH 上，任何能运行 shell 命令的代理都能驱动它。命令以 `--json`
输出结构化 JSON（`read` 与各项检查带有 `schema_version`；`net` 为数组），op-list 携带
`protocol_version`，因此输出始终可被机器校验且幂等。管道（`akcli … | head`）下 shell 报告的是管道的
exit code 而非 akcli 的——若要据此判断请加 `set -o pipefail`。

- **Claude Code** — 安装随附的插件（见下方），即可获得 `/akcli:circuit-review`、
  `circuit-pinmap`、`circuit-draw`、`circuit-diff`、`circuit-parts` 命令与十二个 skills：`akcli-circuit-design`（读取/分析/
  绘制基础）、`akcli-circuit-debug`（连接与工具排障）、`akcli-schematic-review`（按严重度分级的设计评审）、
  `akcli-schematic-authoring`（用 op-list 从零设计电路）、`akcli-altium-interop`（与 Altium Designer 互通）、
  `akcli-parts-sourcing`（JLC/LCSC 元件选型）、`akcli-jlcpcb-capabilities`（JLCPCB 制程能力参考）、
  `akcli-design-calc`（`akcli calc` 的 60 个附标准引用的工程计算器）、`akcli-setup`（环境探测与修复）、`akcli-datasheet-facts`（资料表事实提取）、`akcli-deep-review`（LLM 候选经 review validate 把关）、`akcli-release-gating`（preflight 与校准过的放行策略）。
- **Codex** — 安装随附的插件（见下方）：内含全部十二个 skills 与 session hook；或把 skills 文件夹放进
  `.agents/skills/` 让其自动发现。见 [docs/codex-plugin.md](../docs/codex-plugin.md)。
- **OpenCode** — 会自动发现随附的 skills；把它们放进各自的 skills 目录，
  并让代理通过 shell 调用 `akcli`。命令与一键设置 prompt 见 [INSTALL.md](../INSTALL.md#use-with-ai-coding-agents)。

原生 MCP 服务器仍在[路线图](#路线图)中。

## 安装

尚未发布到 PyPI——请从源码安装。零运行时依赖，需要 **Python ≥ 3.11**（用于标准库 `tomllib`）：

```bash
# 从 clone 直接运行，无需安装
git clone https://github.com/tipoLi5890/akcli
./akcli/bin/akcli --help        # 包装器会自动选择 Python ≥ 3.11

# 或用 pipx 把 CLI 装到 PATH 上
pipx install git+https://github.com/tipoLi5890/akcli
akcli --version
```

Claude Code 插件（marketplace 名称为 `akcli`）：

```text
/plugin marketplace add tipoLi5890/akcli
/plugin install akcli@akcli
```

Codex 插件（名称同为 `akcli`）：

```bash
codex plugin marketplace add tipoLi5890/akcli   # 或在 clone 内用 `add ./`
codex plugin install akcli@akcli
```

完整细节、各代理配置与故障排查见 [INSTALL.md](../INSTALL.md)。

## 路线图

当前已提供（v0.10.x）：KiCad 写入/绘制（22 种 op + 10 种宏，含层次 `add_sheet`、net-diff
安全护栏、`new`/多级 `undo`，输出经 KiCad 自身 netlister 仲裁）、net 不变的 **`arrange --groups`**/
`move_component` carry 式重新布局、一个顾问式的 **`akcli review`** 评审引擎（跨
signal/validation/pcb/emc/domain/gerber 检测族分析，一份 datasheet **facts** 存储，
`propose`/`diff`/`tree`、`validate`，以及 `release --review-policy` 把关）、
ERC/power/BOM/diff/pinmap/**intent**/**contract** 检查（含 waiver 与 SARIF）、原理图 ↔ PCB
**`verify`**、项目 **`library`** 工作区（audit/repair/import-altium/**check-lock**——Altium
`.PcbLib` footprint 导入 + 深度 `.kicad_pcb` + **gerber** 读取）、版本化的 **`fab`** profile，
以及 **`release preflight`** 把关（见 [docs/design-integrity.md](../docs/design-integrity.md)）、
**`akcli sim`**（KiCad 自带 ngspice 上的 SPICE deck、断言、角点扫描、规格书拟合模型）、
JLCPCB/LCSC 元件搜索 + BOM 可购性 + **规格书抓取**、60 个附标准引用的计算器、`view` 仪表板，
以及版本容忍的 Altium/KiCad 读取器（KiCad 层级、Altium 多图纸 + 二进制铜箔）。前瞻计划
（v0.8 → v1.0，各里程碑附验收条件）见 **[ROADMAP.md](../ROADMAP.md)**。重点待开发项目：

- `check`/`diff`/`pinmap` findings 的正式 JSON Schema；查无结果的机器可判别化。
- 完整 **ERC 引脚类型冲突矩阵**（schematic-vs-PCB 同步检查现已以 `akcli verify` 形式提供）。
- 纯标准库的 **SVG 原理图渲染**与自动生成的 pinout book。
- 为原理图 PR 把关的 GitHub **Action**（check + diff + intent + sim 断言）。
- *可选、按需推进：* Altium 轨道——二进制 `.SchLib` 解码器、其余 `.PcbDoc` 段、
  Windows **即时驱动**（scaffold 待验证）。
- 原生 **MCP 服务器**（按决策暂缓；目前代理直接驱动 CLI）。

---

## 致谢

`akcli jlc` 构建于以下开源项目之上（完整署名与许可证文本见
[ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) 与 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)）：

- **JLC2KiCadLib**，作者 **TousstNicolas**（MIT）——LCSC → KiCad 转换核心，以 vendored 方式内嵌（见 THIRD_PARTY_NOTICES）。
- **jlcsearch**（tscircuit，MIT）与 **jlcparts**（MIT）——元件搜索后端。
- **EasyEDA / LCSC / JLCPCB**——元件数据来源。

---

## 联系方式

如有疑问、缺陷或功能请求：请[开一个 GitHub issue](https://github.com/tipoLi5890/akcli/issues)。

---

## 许可证

MIT © 2026 Li, ching yu。见 [LICENSE](../LICENSE)；第三方署名见
[ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) 与 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)，
安全模型见 [SECURITY.md](../SECURITY.md)。
