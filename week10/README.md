# Week 10：V9 追溯、有限 GraphRAG 与变更依赖

V9 在 V8 认证建模流水线上增加投影诊断、需求与模型追溯、图检索问答，以及有限的参数派生依赖分析。代码复用同仓库的 `week9`，请保留完整仓库目录结构，在仓库根目录运行命令。

## 功能

- **投影诊断**：细化 AI 提取结果与本地契约不一致的原因，提供独立 provider 和 runner，沿用 V8 验证规则。
- **追溯查询**：查询需求、参数、SysML / Modelica 声明及检查之间的显式关系，支持影响查询和版本比较。
- **有限 GraphRAG**：支持声明、影响、检查反查和版本比较四类查询；可直接展示证据，也可调用模型组织回答。
- **派生依赖**：验证已记录的 RC 公式 `C = 1 / (2 * pi * R * fc)`，查询截止频率或电阻对电容及模型声明的影响。

统一 CLI 是查询入口，尚未接通“用户提出修改 → 人工确认 → 自动更新并重验”的交互流程。派生变更的真实重验使用本地专用场景脚本，当前源码发布不包含历史运行目录。人工确认阶段也尚未接回当前建模入口。

## 目录与发布范围

| 目录 | 内容 |
|---|---|
| `src/` | provider、runner、追溯、GraphRAG、证据展示及依赖查询 |
| `tests/` | 单元测试与依赖本地数据的集成测试 |
| `configs/` | 公开开发输入、问题集及实验协议配置 |
| `experiments/` | 投影诊断、受控调用和统计脚本 |
| `tools/` | 离线查询包构建工具 |

总结、规划、学习材料、授权记录、实验结果、归档包和本机认证记录仅保留本地，不随此次源码发布上传。

## 查看入口

开发环境使用 Python 3.13。以下帮助命令不调用模型：

```powershell
python -B -m week10.src.v9_cli --help
python -B -m week10.src.v9_cli trace --help
python -B -m week10.src.v9_cli evidence --help
python -B -m week10.src.v9_cli dependency --help
python -B -m week10.src.v9_cli ask --help
```

## 查询已有模型记录

需要先在本地提供符合 `v9-public-trace-manifest-1` 格式的 manifest 和对应 `RunResult` JSON。manifest 及记录必须位于 `week10/results/`，记录路径为项目相对路径，并绑定文件 SHA-256。加载规则见 [trace_cli.py](src/trace_cli.py)。仅有 SysML 或 Modelica 文件不能直接替代这些记录。

以下命令中的路径、运行 ID 和参数 ID 需替换为自己的有效数据：

```powershell
python -B -m week10.src.v9_cli trace --manifest week10/results/my-project/manifest.json requirements
python -B -m week10.src.v9_cli trace --manifest week10/results/my-project/manifest.json impact --run-id my_run --target-id parameter:input_voltage
python -B -m week10.src.v9_cli evidence --manifest week10/results/my-project/manifest.json --question "输入电压对应哪些模型声明？" --run-id my_run
python -B -m week10.src.v9_cli dependency --manifest week10/results/my-project/manifest.json impact --run-id my_run --target-id parameter:cutoff_frequency
python -B -m week10.src.v9_cli ask --manifest week10/results/my-project/manifest.json --question "输入电压对应哪些模型声明？" --run-id my_run
```

`evidence` 直接展示图中的事实，默认 Markdown，也支持 `--format json`。`ask` 默认仅离线检索，显式加 `--answer` 才调用 DeepSeek；在线参数见 `ask --help`，密钥通过环境变量提供，不写入代码。模型引用检查通过不代表回答内容完整或全部正确。

源码中的默认 manifest 指向本地历史演示目录，该目录未上传。克隆后执行实际查询时，需显式提供自己的 manifest。

## 本地生成与验证

生成入口复用 V8 的依赖、SysML 解析器、OpenModelica 和认证配置，具体环境要求见 [V8 README](../week9/README.md)。只有 Python 和源码不足以完成整个流水线。

环境准备完成后，可使用公开开发输入，输出目录应为新目录：

```powershell
python -B -m week10.src.runner --case rc_lowpass --out week10/results/my-local-rc/result.json
```

其他正例为 `rlc_lowpass` 和 `dual_room_thermal`。默认使用本地生成，不调用模型。投影诊断脚本保留冻结配置、环境和授权校验；公开协议文件不等于可复用的在线执行授权，历史验收和授权材料未上传。

## 离线测试与能力边界

以下测试使用内置数据和 mock，无需模型 API 或历史运行结果：

```powershell
python -B -m unittest week10.tests.test_projection_contract week10.tests.test_projection_diagnostics week10.tests.test_projection_contract_v2
```

部分其他测试依赖未上传的历史模型快照，因此源码克隆不等于全量验收环境复现。离线查询包构建工具也需要先准备有效的 manifest 和运行记录。

图查询只使用显式记录的关系；派生依赖目前仅覆盖上述 RC 公式。该版本不提供任意文本自动建图、通用因果推理或自动故障修复。图检索与关键词检索的开发对照不构成一般性的算法优势结论。
