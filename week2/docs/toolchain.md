# 工具链环境

## OpenModelica
- 安装: https://openmodelica.org/download/
- 命令行: `omc`
- Python 接口: `pip install OMPython`（可选，不装则 fallback subprocess）

## SysML v2 渲染（手动）
- Eclipse + OMG SysML-v2-Pilot-Implementation 插件
- 安装指南: https://github.com/Systems-Modeling/SysML-v2-Pilot-Implementation

## DeepSeek API
- 注册: https://platform.deepseek.com
- 环境变量:
  ```
  DEEPSEEK_API_KEY=sk-xxx
  DEEPSEEK_MODEL=deepseek-chat  # 可选
  ```

## Python
- Python 3.10+
- `pip install -r requirements.txt`
