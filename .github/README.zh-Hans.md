<div align="center">

<img src="../docs/assets/hero.png" alt="akcli — 面向人与 AI 代理的 KiCad 原生设计 CLI" width="820">

<p><strong>AI 原生原理图设计，专为 KiCad 打造 — 零依赖（纯 Python 标准库）的命令行工具。</strong></p>

<p>
  <a href="https://github.com/tipoLi5890/akcli/actions/workflows/ci.yml"><img src="https://github.com/tipoLi5890/akcli/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/dependencies-0-brightgreen" alt="零运行时依赖">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT">
</p>

<p><a href="../README.md">English</a> · <a href="README.zh-Hant.md">繁體中文</a> · <strong>简体中文</strong> · <a href="README.ja.md">日本語</a></p>

</div>

---

**akcli** 是一套零依赖的 Python CLI，专注于**在 KiCad 上进行 AI 原生原理图设计**——一条可脚本化的
设计闭环，由你或**任何 AI 代理**端到端驱动（随附 Claude Code、Codex、OpenCode 的插件／skills，
但其实只要有 shell 就能用）。依据 JSON op-list 绘制 `.kicad_sch`、运行 ERC／设计评审／BOM／
原理图 ↔ PCB 检查、在 ngspice 上仿真、查找真实可订购的 JLCPCB/LCSC 元件。既有的 Altium
`.SchDoc` / `.SchLib` / `.PcbDoc` / `.PcbLib` 设计可只读导入——作为进入 KiCad 流程的入口，
所有后续发展都以 KiCad 为路线。

## 安装

零运行时依赖，需要 **Python ≥ 3.11**（用于标准库 `tomllib`）。发行名为 `akcli-kicad`，命令仍是 `akcli`：

```bash
# 从 clone 直接运行，无需安装
git clone https://github.com/tipoLi5890/akcli
./akcli/bin/akcli --help        # 包装器会自动选择 Python ≥ 3.11

# 或从 PyPI 装到 PATH 上
pipx install akcli-kicad        # 或：pip install akcli-kicad
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

## 快速上手

直接在 shell 里读取设计、检查、并绘制进去：

```bash
akcli read  board.kicad_sch --summary                            # 归一化 JSON，压缩在上下文预算内
akcli check board.kicad_sch                                      # ERC-lite + power + BOM + 连通性 + 文档完整性
akcli draw  board.kicad_sch --ops ops.json                       # dry-run：显示改动 + net diff
akcli draw  board.kicad_sch --ops ops.json --apply --strict-nets # 原子写入 + 校验 + 备份
akcli undo  board.kicad_sch                                      # 回退上一次写入
```

每次写入默认都是 dry run；`--apply` 会经过原子化的 快照 → 临时文件 → 校验 → 替换 流水线，并由纯
Python 的连通性闸门把关，`akcli undo` 则从轮替备份栈回退。离线状态下 Altium 文件一律只读。

## 功能一览

每个命令背后都是同一套归一化模型，因此每一项检查、比对与报告都作用于 KiCad `.kicad_sch`——
对导入的 Altium `.SchDoc` 也同样适用：

| 命令 | 功能 |
|---|---|
| `read` · `net` · `component` · `pins` | 把 KiCad 或 Altium 解析成同一套归一化 JSON 模型；查询 net（整条 net，或引脚级的 `net --at`／`--same`／`--why`）、元件与引脚坐标。 |
| `new` · `plan` · `draw` · `ops` · `arrange` | 依据带版本号的 **22 种 op + 10 种宏** JSON op-list 绘制 `.kicad_sch`（根文件，或既有层次结构中的子图页），由 net-diff 安全闸门与一键 `undo` 把关；`ops from-intent` 可把设计文档（声明的 nets + 元件）直接编译成 op-list。符号来源依次为 `--symbols`、配置文件路径，最后是项目与全局的 `sym-lib-table`。 |
| `check` · `verify` · `diff` · `pinmap` | ERC-lite + power + BOM + 文档完整性 + intent／contract 检查、原理图 ↔ PCB 等价、net 成员关系 diff、MCU 引脚 → net 表。 |
| `review` | 建议性（advisory）、分级置信度的设计评审，横跨六大检测族（signal／validation／pcb／emc／domain／gerber）；标记 DNP 的元件不满足任何规则。 |
| `sim` | 转成 SPICE deck，在 KiCad 自带 ngspice 上断言；角点扫描；用 datasheet 点拟合二极管模型。元件值先归一化，无法确定解读的一律拒绝而不猜测。 |
| `jlc` | 搜索 JLCPCB/LCSC（库存、价格、Basic／Extended），进程内把元件转成 KiCad 库（带电气类型的引脚、真实 `Value`），抓取规格书。 |
| `calc` | **60** 个附标准引用的离线计算器（E 系列、IPC-2221、阻抗、I²C 上拉、buck／boost……），每条都附引用。 |
| `library` · `fab` · `release` | 库工作区审计／修复、从引脚表生成元件符号（`new-symbol`——提供引脚表，akcli 计算坐标）并附独立 lint（`lint-symbols`）与把关写入的共点引脚检查、版本化 fab profile 检查，以及写出可追溯 manifest 的 release preflight。 |
| `gate` | 迭代级的四项检查组合，供每轮 GUI 编辑后重跑（完整 `check` 集、intent 断言、原理图 ↔ PCB `verify`、BOM lock 漂移——默认离线）；已配置的项目零参数即可执行，发布级闸门仍是 `release preflight`。 |
| `export` · `render` · `doc` · `view` | netlist 导出（Protel／KiCad／CSV——KiCad 格式带有重新导入所需的 UUID sheetpath 身份）、纯标准库 SVG 渲染、Markdown pinout book，以及 localhost 的 `/calc` + `/live` 仪表板。 |

两个代表性示例——一次设计评审与一次元件搜索：

```bash
akcli review analyze board.kicad_sch --profile deep --pcb board.kicad_pcb   # 建议性评审发现 + 证据
akcli jlc search "0.1uF 0402 X7R"                                           # JLCPCB/LCSC 元件库（需联网）
```

## 与 AI 代理一起使用

`akcli` 就是一个普通 CLI，只要它在 PATH 上，任何能运行 shell 命令的代理都能驱动它。命令以 `--json`
输出结构化 JSON（带版本戳——`schema_version`，或 `proposals_version`／`facts_version` 这类家族字段），op-list 携带 `protocol_version`，`akcli capabilities`
会用一份 JSON 文档自描述整个 CLI 能力面——而且每个错误码都携带机器可读的 `remediation` 修复提示。

- **Claude Code** — 安装随附插件，即可获得 8 个 `/akcli:circuit-*` 命令（review、pinmap、draw、
  diff、parts、sim、datasheet、release）与 12 个 skills，涵盖设计、评审、电路编写、Altium 互通、元件选型、计算器与 release
  把关。
- **Codex** — 安装随附插件，或把 skills 文件夹放进 `.agents/skills/` 让其自动发现。见
  [docs/codex-plugin.md](../docs/codex-plugin.md)。
- **OpenCode** — 会自动发现随附的 skills；命令见
  [INSTALL.md](../INSTALL.md#use-with-ai-coding-agents)。

## 为什么选 akcli

- **零运行时依赖。** 仅使用标准库（含 `tomllib`）——易于内嵌（vendor）、沙箱化或在 CI 中运行。
- **专为 KiCad 打造。** 核心是迭代式的 KiCad S-expression 解析器与字节级稳定的写入器；
  Altium 设计经纯标准库的 OLE2/CFBF 记录解码只读导入，直接接入同一条 KiCad 流程。
  无需任何编译扩展。
- **字节级完全一致的重复应用。** 确定性 UUIDv5 + 就地替换，让每次编辑都幂等——重跑同一份 op-list
  会产生完全相同的字节。
- **连通性是唯一的硬性写入闸门。** 每次 `plan`／`draw` 都打印写入前后的 net diff（拆分、合并、
  改名——按引脚成员关系匹配，绝不按名称匹配）；`--strict-nets` 会拒绝任何拆分或合并具名 net 的写入。
- **值得信赖的 net 推断。** 重建的 net 层可处理全局同名合并、连接点、T 型连接点和 No-ERC 标记——
  修复了经典的「同名 net 被拆成单引脚 net」缺陷。

## 可选的外部工具

「零依赖」指的是 **Python 包**依赖为零：`pip install` 不会拉进任何第三方包，核心流程
（read／plan／draw／check／diff／calc／render）仅靠标准库即可完整运行。少数功能可以借助
Python 之外的东西——一律在运行时探测，核心流程永远不需要它们：

| 功能 | 使用 | 缺少时 |
|---|---|---|
| 建议性 ERC 第二意见、`view live` 的 SVG | `kicad-cli`（本机可执行文件） | 直接跳过——不会导致失败，结果本来就只是建议性的。 |
| `sim` 执行（`--deck-only` 不需要） | libngspice（KiCad 自带） | `sim` 以 `NGSPICE_MISSING` 退出；其余功能不受影响。 |
| `jlc` 元件搜索／规格书抓取 | 网络 | **唯一**联网的命令家族；其余全部离线。 |

`akcli doctor` 会逐项探测上述三者，并打印各操作系统的修复建议。

## 文档

| 文档 | 内容 |
|---|---|
| [SPEC.md](../docs/SPEC.md) | 数据模型、配置表、JSON schema |
| [cli-reference.md](../docs/cli-reference.md) | 每个命令与参数 |
| [op-list-authoring.md](../docs/op-list-authoring.md) | op-list 编写权威指南（op、宏、group） |
| [design-integrity.md](../docs/design-integrity.md) | 契约、fab profile、release preflight |
| [review-rules.md](../docs/review-rules.md) | 设计评审规则目录 |
| [sim.md](../docs/sim.md) | 仿真参考 |
| [ROADMAP.md](../ROADMAP.md) | 通往 v1.0 的路线图与验收条件 |

## 路线图

**当前已提供（v0.18.0）：** KiCad 写入／绘制（22 种 op + 10 种宏，含层次 `add_sheet` 与写入既有
层次结构中的子图页、net-diff 安全护栏、`new`／多级 `undo`）、保持 net 不变的 `arrange --groups`
重新布局、建议性的 `akcli review` 评审引擎（六大检测族，现已纳入电源入口保护检测、datasheet facts
存储、`propose`／`tree`／`validate`）、ERC／power／BOM／diff／pinmap／intent／contract 检查（含
waiver 与 SARIF）、只读的文档完整性检查 `check --integrity`、原理图 ↔ PCB `verify`、项目 `library`
工作区、版本化 `fab` profile、`release preflight` 把关、KiCad 自带 ngspice 上的 `akcli sim`、
JLCPCB／LCSC 元件搜索与规格书抓取、60 个附标准引用的计算器、能画出 KiCad `lib_symbols` 真实符号图形
的纯标准库 SVG 渲染，以及版本容忍的 Altium／KiCad 读取器。标准符号 nickname 在原厂 KiCad 安装上即可
解析——那些编译内建的 `${KICAD*_SYMBOL_DIR}` 路径会从找到的 `kicad-cli` 推导，`akcli doctor` 会报告
这条解析路径在本机是否可用。

**前瞻（→ v1.0）：** 契约冻结审计（contract freeze audit）。首次发布到 PyPI（`pip install
akcli-kicad`）已于 0.15.0 发布。*按决策暂缓：* 为原理图 PR 把关的 GitHub Action、`view` 波形面板，以及原生 MCP
服务器（目前代理直接驱动 CLI）。完整计划与验收条件见 [ROADMAP.md](../ROADMAP.md)。

## 致谢

`akcli jlc` 构建于以下开源项目之上（完整署名与许可证文本见
[ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) 与 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)）：

- **JLC2KiCadLib**，作者 **TousstNicolas**（MIT）——LCSC → KiCad 转换核心，以 vendored 方式内嵌。
- **jlcsearch**（tscircuit，MIT）与 **jlcparts**（MIT）——元件搜索后端。
- **EasyEDA / LCSC / JLCPCB**——元件数据来源。

## 联系方式

如有疑问、缺陷或功能请求：请[开一个 GitHub issue](https://github.com/tipoLi5890/akcli/issues)。

## 许可证

MIT © 2026 Li, ching yu。见 [LICENSE](../LICENSE)；第三方署名见
[ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) 与 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)，
安全模型见 [SECURITY.md](../SECURITY.md)。
