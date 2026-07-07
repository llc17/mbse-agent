# V4 H2: LLM 生成 SysML vs 官方 Training 示例 — 对比报告

> 日期: 2026-07-07
> 官方库: `D:\sysml-v2-official\sysml\src\training\`
> 基线: V4 prompt (node2_sysml.txt)，对齐 OMG SysML v2 官方写法
> 方法: 10 项 checklist 逐项对比，分硬指标（语法正确性）和软指标（风格规范性）

---

## 对比数据来源

| 数据 | 说明 |
|------|------|
| V3 prompt | `week4/prompts/node2_sysml.txt`（旧：import ISQ::* / attribute :> ISQ::xxx） |
| V4 prompt | `week5/prompts/node2_sysml.txt`（新：private import ScalarValues::* / attribute : Real） |
| 官方示例 | 02 Part Definition, 05 Redefinition, 07 Parts, 09 Connections, 10 Ports |

---

## 10 项 Checklist 逐项对比

### 1. import 语句

| | 写法 | 官方一致? | sysmlpy 通过? |
|------|------|:--:|:--:|
| 官方 | `private import ScalarValues::*;` | ✅ | ✅ |
| V3 | `import ISQ::*;` + `import SI::*;` | ❌ | ❌ |
| V4 | `private import ScalarValues::*;` | ✅ | ✅ |

**结论**: V4 与官方完全一致。V3 的 `import ISQ::*` 是 sysmlpy 拒绝的第一原因。

---

### 2. part def 结构

| | 写法 |
|------|------|
| 官方 | `part def Vehicle { attribute mass : Real; part eng : Engine; }` |
| V3 | `part def Resistor {{ attribute resistance :> ISQ::resistance; ... }}` |
| V4 | `part def Resistor { attribute resistance : Real; port p : ElectricalPort; ... }` |

**差异**:
- V3 属性类型用 `:> ISQ::resistance`，官方用 `: Real`（简单属性）
- V3 用双花括号 `{{`（模板遗留），V4 已修正
- 结构组织一致：属性在前，嵌套 part/port 在后

**结论**: V4 与官方一致（硬指标 ✅）

---

### 3. attribute 声明

| | 语法 | 语义 |
|------|------|------|
| 官方 | `attribute mass : Real;` | 声明属性，类型为 Real |
| V3 | `attribute resistance :> ISQ::resistance;` | 声明属性并指定其为 ISQ::resistance 的子类型 |
| V4 | `attribute resistance : Real;` | 声明属性，类型为 Real |

**评估**: 官方示例中简单物理量（质量、温度等）直接用 `Real`，不使用 `:> ISQ::xxx` 语义类型。`ISQ::*` 语义类型在更高级的 SysML 建模中有其价值（可做量纲分析），但基础建模中用 `Real` 即可。V4 选择与官方 training 示例对齐。

**结论**: V4 与官方 training 风格一致（硬指标 ✅）

---

### 4. port 定义

| | 写法 |
|------|------|
| 官方 | `port def FuelOutPort { attribute temperature : Temp; out item fuelSupply : Fuel; }` |
| 官方简化 | `port def ElectricalPort;`（stub 声明，无内部结构） |
| V3/V4 | `port def ElectricalPort;` |

**评估**: 我们的电气/热域端口是简单的能量端口，没有定向 item flow。使用 stub 声明 `port def ElectricalPort;` 与官方简化写法一致。如果后续需要定向流（如液压/流体），可扩展为完整的 port def 含 `in/out item`。

**结论**: 当前领域适用（硬指标 ✅）

---

### 5. connect 语法

| | 写法 |
|------|------|
| 官方 | `connect [0..1] lugBoltJoints to [1] wheel.w.mountingHoles;` |
| V3/V4 | `connect src.p to r.p;` |

**评估**: 官方用了数组索引 `[0..1]` 和嵌套路径 `wheel.w.mountingHoles`，我们用了简单端口路径 `src.p`。语法核心 `connect X to Y` 一致。复杂索引和嵌套路径在当前用例中不需要。

**结论**: 语法一致（硬指标 ✅）

---

### 6. package 包裹

| | 写法 |
|------|------|
| 官方 | `package 'Part Definition Example' { ... }` （单引号包名） |
| V3 | `package RCLowPassFilter {{ ... }}` （双花括号 + 无引号） |
| V4 | `package RCLowPassFilter { ... }` （无引号 PascalCase） |

**评估**: 官方用单引号包裹含空格的名字。不带空格的名字可以不用引号。V4 用 PascalCase（无空格），与官方无引号变体一致。双花括号问题已修正。

**结论**: 语法一致（硬指标 ✅）

---

### 7. doc 注释

| | 写法 |
|------|------|
| 官方 | `doc /* 实际质量应小于要求质量 */`（中文示例用中文注释） |
| V3/V4 | `doc /* Ideal linear electrical resistor */` |

**评估**: `doc /* ... */` 语法一致。官方示例中 doc 出现在 part def 内部的 attribute 之前或之后。V3/V4 同样放在合适位置。

**结论**: 一致（硬指标 ✅）

---

### 8. 命名规范

| | 规范 |
|------|------|
| 官方 | PascalCase: `Vehicle`, `WheelHubAssembly`, `FuelTankAssembly` |
| V3 | PascalCase: `Resistor`, `Capacitor`, `VoltageSource` |
| V4 | 同上 |

**评估**: V3/V4 的命名与官方一致——使用 PascalCase，无下划线分隔。

**结论**: 一致（软指标 ✅）

---

### 9. requirement def 写法

| | 写法 |
|------|------|
| 官方 | `requirement def MassRequirement { doc /* ... */ attribute massRequired :> ISQ::mass; require constraint { massActual <= massRequired } }` |
| V3 | `requirement def LowPassFilterRequirement { doc /* ... */ attribute cutoffFrequency :> ISQ::frequency; require constraint { cutoffFrequency == 1.0 / (2.0 * pi * resistance * capacitance) } }` |
| V4 | `requirement def LowPassFilterRequirement { doc /* ... */ attribute cutoffFrequency : Real; require constraint { cutoffFrequency == 1.0 / (2.0 * pi * resistance * capacitance) } }` |

**评估**: 
- V3 使用 `:> ISQ::frequency`，官方用 `:> ISQ::mass`
- 注：官方 requirement def 示例中确实使用了 `:> ISQ::mass` 语义类型。这是 ISQ 类型的合理用法。但对于 V4 的 prompt，统一用 `Real` 更安全——sysmlpy 对 ISQ 命名空间的导入有严格要求
- V4 使用 `: Real`，虽失去语义类型信息，但提高了 sysmlpy 通过率

**结论**: V4 在实用性和合规性间做了权衡（软指标 ⚠️）

---

### 10. 整文件结构

| | 层级 |
|------|------|
| 官方 | `package { (import) → part def* → part* → connect* }` |
| V3/V4 | `package { (import) → port def* → part def* → part* → connect* }` |

**评估**: V3/V4 多了 `port def` 和 `requirement def`，因为我们的模型需要端口和需求定义。官方部分示例不包含端口和需求（它们在专门的示例中）。整体结构——定义在前、用法在后、连接在最后——与官方一致。

**结论**: 结构组织一致（软指标 ✅）

---

## 汇总

| # | 维度 | V3 | V4 | 类型 |
|---|------|:--:|:--:|------|
| 1 | import 语句 | ❌ | ✅ | 硬 |
| 2 | part def 结构 | ⚠️ | ✅ | 硬 |
| 3 | attribute 声明 | ❌ | ✅ | 硬 |
| 4 | port 定义 | ✅ | ✅ | 硬 |
| 5 | connect 语法 | ✅ | ✅ | 硬 |
| 6 | package 包裹 | ⚠️ | ✅ | 硬 |
| 7 | doc 注释 | ✅ | ✅ | 硬 |
| 8 | 命名规范 | ✅ | ✅ | 软 |
| 9 | requirement def | ⚠️ | ⚠️ | 软 |
| 10 | 文件结构 | ✅ | ✅ | 软 |

**硬指标通过率**: V3 = 3/8 (37.5%), V4 = 8/8 (100%)  
**软指标通过率**: V3 = 2/3 (67%), V4 = 2.5/3 (83%)

---

## 关键发现

1. **V3 最大的问题不是结构而是 import 和 attribute 语法**——这两个修复后，其余语法大部分已经合规
2. **官方示例不教 ISQ 语义类型**——training 示例中的物理量直接用 `Real`，`ISQ::xxx` 仅出现在 requirement def 的约束里
3. **sysmlpy 对 import 的解析是严格的**——必须先 `private import` 才能使用命名空间内类型
4. **双花括号 `{{}}` 是 Python 模板引擎的遗留痕迹**——在 `.replace()` 调用模式下不需要，LLM 甚至会原样输出
5. **V4 prompt 的通过率依赖于 LLM 是否遵循 prompt 示例**——探针实验将给出最终答案

---

## V5 建议

- 如果可以稳定使用 ISQ 语义类型（如 sysmlpy 后续版本支持），可考虑恢复 `:> ISQ::resistance` 写法以获得量纲检查能力
- requirement def 的约束表达式可以更复杂（官方支持丰富的数学表达式）
- 可考虑加入 `satisfy` 关系连接 requirement 和 part
