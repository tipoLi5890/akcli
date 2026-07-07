[English](../README.md) · [繁體中文](README.zh-Hant.md) · **简体中文**

# altium-kicad-cli

**altium-kicad-cli**（CLI 命令 `akcli`，导入包 `altium_kicad_cli`）是一套零依赖的
Python 工具包与 Claude Code 插件，可在**未安装 Altium 或 KiCad** 的情况下读取
**Altium 二进制 `.SchDoc` / `.SchLib` / `.PcbDoc`** **以及** **KiCad `.kicad_sch` / `.kicad_sym` / `.kicad_pcb`**，
并从命令行运行 ERC / power / pinmap / BOM / diff 等检查，以及依据 JSON 操作列表（op-list）绘制 KiCad 原理图。
它专为 AI 编码代理（coding agent）而打造。

它将两种格式读入同一个归一化模型并对其进行*分析*——解析、检查、比对（diff）、绘制——
为你提供可脚本化、免安装的工作流，让自动化流水线或 LLM 代理都能驱动它。

[![CI](https://github.com/tipoLi5890/altium-kicad-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/tipoLi5890/altium-kicad-cli/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 亮点

- **两种格式，一个模型。** Altium 二进制 `.SchDoc` 与 KiCad `.kicad_sch` 都会归一化到同一个
  `Schematic`/`Pcb`/`Library` 模型，因此每一项检查、比对和报告都与格式无关。
- **无需安装 EDA。** 纯标准库实现的 OLE2/CFBF + Altium 记录解码，以及一个迭代式的 KiCad
  S-expression 解析器。无需 Altium、无需 KiCad、无需编译扩展——只要 Python ≥ 3.11。
- **零运行时依赖。** 仅使用标准库（包括 `tomllib`）。易于内嵌（vendor）、沙箱化或在 CI 中运行。
- **值得信赖的网络（net）推断。** 重建的 net 层可处理全局同名合并、连接点（junction）、
  T 型连接点和 No-ERC 标记——修复了经典的「同名 net 被拆成单引脚 net」缺陷。
- **对 Altium 只读、对 KiCad 安全写入。** Altium 文件在离线状态下永不被修改；KiCad 写入会经过
  原子化的 快照 → 临时文件 → 校验 → 替换 流水线，并由纯 Python 的连通性闸门把关。
- **AI 代理原生。** 以 Claude Code 插件形式发布，附带 skills/commands，输出带有
  `schema_version` 的结构化 JSON，并接受带版本号的 op-list 以实现确定性、幂等的编辑。

## 读取 Altium 文件

`akcli` 直接打开 Altium 二进制文件。它内含一个加固的 OLE2/CFBF（复合文件二进制格式，
Compound File Binary Format）容器读取器以及一个 Altium 记录解码器——无需 Altium Designer、
无需 Windows、无需许可证。

```bash
akcli read   main.SchDoc        # 将 .SchDoc 解析为归一化 JSON
akcli net    main.SchDoc         # 提取网表（net -> pins）
akcli component main.SchDoc U10    # 单个元件的引脚 -> net（需给 designator）
```

支持的 Altium 输入：`.SchDoc`（原理图）、`.SchLib`（符号库——文本记录符号；含二进制符号记录的库会以 exit 5「不支持」拒绝）、`.PcbDoc`（电路板——
目前支持 ASCII 的 `Nets6`/`Components6`/`Classes6`/`Rules6` 段；二进制的焊盘/走线段会被
明确拒绝而非误解析）。所有 Altium 访问均为**只读**。

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

## 运行检查（ERC、power、pinmap、BOM、diff）

无需打开任何 EDA 工具即可运行电气规则检查（ERC）及其他设计检查：

```bash
akcli check  main.SchDoc                          # ERC-lite + power + BOM 卫生检查
akcli pinmap main.SchDoc -C altium-kicad-cli.toml # MCU 引脚 -> net（+ 可选的预期表）
akcli diff   v1.SchDoc v2.SchDoc                   # 基于 net 成员关系的 diff，而非基于名称
```

power/ground 检测是**基于 net 名称 + power 端口（power-port）**的，而非纯粹基于电气类型，
因为真实电路板上以 `Passive` 引脚为主——一个仅看类型的朴素 ERC 会产生空洞的「通过」结果。
每份报告都会打印一个元数据头（passive 引脚占比、被抑制的 No-ERC 数量、未命名 net 数量、
分数坐标的存在情况），因此一个干净的结果永远不会被误认为空结果。

## 依据 op-list 写入 KiCad 原理图

`akcli` 依据带版本号的 JSON **op-list**（放置元件、导线、连接点、标签、power 端口、文本……）
写入 KiCad 原理图。写入是精准且幂等的（确定性 UUIDv5），由纯 Python 连通性校验器把关，
并要求显式 `--apply`（默认为 dry run）。

```bash
akcli plan  ops.json --target board.kicad_sch     # 校验 op-list，显示将会改动的内容
akcli draw  ops.json --target board.kicad_sch     # 默认 dry-run（不写入文件）
akcli draw  ops.json --target board.kicad_sch --apply   # 原子写入 + 校验 + 备份
```

Altium 的*写入/绘制*仅通过可选的 Windows live driver（需运行 Altium 22+）提供；
离线状态下，Altium 仅支持分析。

## 查找 JLCPCB / LCSC 元件

`akcli jlc` 可搜索 JLCPCB / LCSC 元件库（库存、价格阶梯、Basic/Extended 状态），并可将元件**进程内**转换为 KiCad 库（内嵌 MIT 许可的 [JLC2KiCadLib](https://github.com/TousstNicolas/JLC2KiCad_lib) 核心——无需安装外部工具；见[致谢](#致谢)）。

```bash
akcli jlc search "0.1uF 0402 X7R"     # 关键字 / MPN / 分类搜索（需联网）
akcli jlc show   C7593                 # 按 LCSC C-number 查单个元件
akcli jlc add    C2040 --3d            # LCSC 元件 → KiCad 符号＋封装＋STEP
```

## 与 AI 编码代理一起使用

`akcli` 就是一个普通 CLI，只要它在 PATH 上，任何能运行 shell 命令的代理都能驱动它。命令以 `--json`
输出结构化 JSON（`read` 与各项检查带有 `schema_version`；`net` 为数组），op-list 携带
`protocol_version`，因此输出始终可被机器校验且幂等。管道（`akcli … | head`）下 shell 报告的是管道的
exit code 而非 akcli 的——若要据此判断请加 `set -o pipefail`。

- **Claude Code** — 安装随附的插件（见下方），即可获得 `/altium-kicad:circuit-review`、
  `circuit-pinmap`、`circuit-draw`、`circuit-diff` 命令与八个 skills：`circuit-design`（读取/分析/
  绘制基础）、`circuit-debug`（连接与工具排障）、`schematic-review`（按严重度分级的设计评审）、
  `schematic-authoring`（用 op-list 从零设计电路）、`altium-interop`（与 Altium Designer 互通）、
  `parts-sourcing`（JLC/LCSC 元件选型）、`jlcpcb-capabilities`（JLCPCB 制程能力参考）、
  `design-calc`（`akcli calc` 的 31 个附标准引用的工程计算器）。
- **Codex** — 安装随附的插件（见下方）：内含全部八个 skills 与 session hook；或把 skills 文件夹放进
  `.agents/skills/` 让其自动发现。见 [docs/codex-plugin.md](../docs/codex-plugin.md)。
- **OpenCode** — 会自动发现随附的 skills；把它们放进各自的 skills 目录，
  并让代理通过 shell 调用 `akcli`。命令与一键设置 prompt 见 [INSTALL.md](../INSTALL.md#use-with-ai-coding-agents)。

原生 MCP 服务器仍在[路线图](#路线图)中。

## 安装

尚未发布到 PyPI——请从源码安装。零运行时依赖，需要 **Python ≥ 3.11**（用于标准库 `tomllib`）：

```bash
# 从 clone 直接运行，无需安装
git clone https://github.com/tipoLi5890/altium-kicad-cli
./altium-kicad-cli/bin/akcli --help        # 包装器会自动选择 Python ≥ 3.11

# 或用 pipx 把 CLI 装到 PATH 上
pipx install git+https://github.com/tipoLi5890/altium-kicad-cli
akcli --version
```

Claude Code 插件（marketplace 名称为 `altium-kicad`）：

```text
/plugin marketplace add tipoLi5890/altium-kicad-cli
/plugin install altium-kicad@altium-kicad
```

Codex 插件（名称同为 `altium-kicad`）：

```bash
codex plugin marketplace add tipoLi5890/altium-kicad-cli   # 或在 clone 内用 `add ./`
codex plugin install altium-kicad@altium-kicad
```

完整细节、各代理配置与故障排查见 [INSTALL.md](../INSTALL.md)。

## 路线图

当前已提供：Altium `.SchDoc` / `.SchLib` 与 KiCad `.kicad_sch` 读取（与版本无关，KiCad **含层级
图纸**）、net 推断、ERC/power/BOM/diff/pinmap 检查、KiCad 写入/绘制（16 种 op,含 delete/move 与
多单元放置,输出经 KiCad 自身 ERC 验证）,以及 JLCPCB / LCSC 元件搜索。完整里程碑规划
（v0.2 → v1.0，各里程碑附验收条件）见 **[ROADMAP.md](../ROADMAP.md)**。重点待开发项目：

- Altium `.PcbDoc` **二进制**段（焊盘/走线/过孔/圆弧/填充/区域）——目前可读 ASCII 段。
- **离线 Altium 写入**与以 Altium 为权威的 ERC/网表（目前需运行中的即时驱动）。
- **分层 / 多图纸**的 KiCad *写入*（writer 仍为扁平结构；reader 已支持层级读取）。
- 面向 Windows + Altium 22+ 的 Altium **即时驱动**（DelphiScript 部分仍为待验证的 scaffold）。
- 原生 **MCP 服务器**。

---

## 致谢

`akcli jlc` 构建于以下开源项目之上（完整署名与许可证文本见
[ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) 与 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)）：

- **JLC2KiCadLib**，作者 **TousstNicolas**（MIT）——LCSC → KiCad 转换核心，以 vendored 方式内嵌（见 THIRD_PARTY_NOTICES）。
- **jlcsearch**（tscircuit，MIT）与 **jlcparts**（MIT）——元件搜索后端。
- **EasyEDA / LCSC / JLCPCB**——元件数据来源。

---

## 联系方式

如有疑问、缺陷或功能请求：请[开一个 GitHub issue](https://github.com/tipoLi5890/altium-kicad-cli/issues)。

---

## 许可证

MIT © 2026 Li, ching yu。见 [LICENSE](../LICENSE)；第三方署名见
[ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) 与 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)，
安全模型见 [SECURITY.md](../SECURITY.md)。
