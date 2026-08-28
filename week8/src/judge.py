"""V7 LLM-as-Judge — 用独立模型（Kimi）给 V4/V6 的产出盲评打分。

设计要点（对齐 V7 修订版 + V6 prompt 规范）:
  - 裁判模型 = Kimi（与被评的 DeepSeek **不同**，避免"同模型审自己"的盲区）
  - 盲评: 裁判只看到"一份产出 + 一份需求 + 官方示例"，不知道这是 V4 还是 V6
  - temperature=0: 保证可复现（同一份产出打分一致）
  - 评分标准 4 项 × 25 分 = 100，每项有具体判断标准（不笼统）
  - 贴官方 SysML 示例当"标准答案"（复用 week7 的 retrieval.get_references）

  ⚠️ 独立实现: 裁判用 Kimi 直连（OpenAI 兼容接口），**不依赖 week7 的 llm_client**
     （week7 只有 deepseek/zhipu 两个 provider，且改 week7 会污染 A/B 对比）。
     这样 judge 与被评模型彻底解耦。

环境变量（.env）:
    KIMI_API_KEY   — Kimi API 密钥
    KIMI_API_BASE  — Kimi API 地址（默认 https://api.moonshot.cn/v1）
    KIMI_MODEL     — Kimi 模型名（默认 kimi-k3）

评分 4 项:
  1. SysML 语法规范性（对照官方示例）       25 分
  2. 参数与需求一致性                      25 分
  3. 拓扑结构正确性                        25 分
  4. 追溯链完整性                          25 分

用法:
    # 命令行（由 run_ab.py 调起）
    python judge.py --result <runner结果json> --out <评分json>
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # D:\mbse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _load_env():
    """加载 .env（Kimi key 在里面）。"""
    env_path = _PROJECT_ROOT / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
        except ImportError:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


# ============================================================================
# Kimi 直连（OpenAI 兼容），独立于 week7 的 llm_client
# ============================================================================

def _kimi_chat(messages: list[dict], *, max_tokens: int = 16384) -> str:
    """调用 Kimi chat/completions，返回纯文本（content 字段）。带简单重试。

    Kimi K3 硬约束（官方）:
      - temperature 固定 1.0，**不可传**（传了就 400）
      - 思考模型输出长，max_tokens 建议 >= 16000
      - reasoning_effort 支持 low/high/max，默认 max；judge 用 low 省 token 更快
      - 返回的最终答案在 message.content（reasoning_content 是思考过程，不读）
    """
    api_key = os.environ.get("KIMI_API_KEY", "")
    api_base = os.environ.get("KIMI_API_BASE", "https://api.moonshot.cn/v1").rstrip("/")
    model = os.environ.get("KIMI_MODEL", "kimi-k3")

    if not api_key:
        raise RuntimeError("缺少 KIMI_API_KEY，请在 .env 中配置")

    # 与 week7 llm_client 一致: base 已含 /v1 则不再重复
    chat_path = "/chat/completions" if api_base.endswith("/v1") else "/v1/chat/completions"
    url = f"{api_base}{chat_path}"

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "reasoning_effort": "low",  # judge 只需打分，不需要高强度推理
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last_err = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=180)
            if resp.ok:
                msg = resp.json()["choices"][0]["message"]
                content = msg.get("content", "") or ""
                # 容错: 若 content 为空但 reasoning_content 有内容（极端情况），
                # 也返回 reasoning 结尾作为兜底（judge 的 JSON 解析会多级容错）
                if not content.strip():
                    rc = msg.get("reasoning_content", "") or ""
                    if rc.strip():
                        print("  [kimi] 警告: content 为空，回退到 reasoning_content 结尾", file=sys.stderr)
                        return rc[-3000:]
                return content
            detail = resp.text[:300]
            status = resp.status_code
            # 429（限流/过载）用更长的退避，因为 Kimi RPM=3 需要等 ~20s 才能恢复
            if status == 429:
                print(f"  [kimi] API 429 (attempt {attempt}): 限流，等待 20s 后重试", file=sys.stderr)
                time.sleep(20)
                continue
            print(f"  [kimi] API {status} (attempt {attempt}): {detail}", file=sys.stderr)
            last_err = RuntimeError(f"Kimi API {status}: {detail}")
        except requests.RequestException as e:
            print(f"  [kimi] 网络错误 (attempt {attempt}): {e}", file=sys.stderr)
            last_err = e
        if attempt < 3:
            time.sleep(3 * (2 ** (attempt - 1)))

    raise RuntimeError(f"Kimi 调用失败: {last_err}")


def _get_references(component_type: str) -> str:
    """从 week7 的 retrieval 拿官方示例当评分标准（纯 Python 匹配，不触发 LLM 调用）。"""
    try:
        week7_dir = str(_PROJECT_ROOT / "week7")
        if week7_dir not in sys.path:
            sys.path.insert(0, week7_dir)
        from src.retrieval import get_references
        # stage="review" 是审查对照标准，code_fences=False 避免多代码块 quirk
        return get_references(component_type, stage="review", code_fences=False)
    except Exception as e:
        print(f"⚠️ 检索官方示例失败（评分标准降级）: {e}", file=sys.stderr)
        return ""


def _build_score_prompt(result: dict, references: str) -> str:
    """构造盲评 prompt。不告诉裁判这是 V4 还是 V6。"""
    component_type = result.get("component_type", "")
    parameters = result.get("parameters", {})
    sysml_code = result.get("sysml_code", "")
    modelica_code = result.get("modelica_code", "")
    summary_text = result.get("summary_text", "")

    req_block = f"组件类型: {component_type}\n参数: {json.dumps(parameters, ensure_ascii=False, indent=2)}"

    prompt = f"""## 角色
你是 MBSE 系统建模质量裁判。请对照官方 SysML v2 示例，给一份产出**盲评打分**。
你只看到一份匿名产出，不知道它来自哪个版本、哪个模型。按统一标准逐条打分。

## 需求（系统应该做什么）
{req_block}

## 待评产出 ①: SysML v2 代码
```sysml
{sysml_code[:6000]}
```

## 待评产出 ②: Modelica 代码
```modelica
{modelica_code[:4000]}
```

## 待评产出 ③: 总结报告（用于评追溯链）
{summary_text[:2000] if summary_text else '(无总结报告)'}

## 官方 SysML v2 参考示例（评分对照标准）
{references if references else '(官方示例不可用，凭 SysML v2 通用规范判断)'}

## 评分标准（4 项，每项 0-25 分，总分 0-100）
1. **SysML 语法规范性 (25分)**: package/part def/attribute/port/connect 写法是否符合官方示例；
   是否有非法 import（如 import ISQ::* / import SI::*）、错误属性类型（:> ISQ::xxx 而非 : Real）、
   花括号不匹配、缺 package 声明等硬伤。0 分=多处硬伤，25 分=完全符合官方写法。
2. **参数与需求一致性 (25分)**: SysML/Modelica 中的参数值是否与需求一致（如 R=1000Ω 是否写成 1000 而非其他值）；
   是否有需求要求"根据公式计算"的参数（如电容）算错。0 分=参数多处与需求不符，25 分=完全一致。
3. **拓扑结构正确性 (25分)**: 部件/连接关系是否构成需求描述的系统（如 RC 是否串成低通、双房间是否两间都连到室外）；
   是否有缺失连接或多余连接。0 分=拓扑根本错误，25 分=拓扑完全正确。
4. **追溯链完整性 (25分)**: 总结报告是否说明需求→SysML→Modelica→仿真结果的完整链条；
   是否给出参数表、仿真数值对比。0 分=无追溯，25 分=链条完整、数值可对上。

## 输出格式（严格 JSON，只输出 JSON，不要任何解释）
```json
{{
  "scores": {{
    "syntax": 0-25,
    "consistency": 0-25,
    "topology": 0-25,
    "traceability": 0-25
  }},
  "total": 0-100,
  "comment": "一句话总评"
}}
```

## 重要规则
1. 只按标准打分，不要因为产出看起来"复杂"或"简单"而给分
2. 不确定的问题往低里估，但不要恶意压分
3. total = 四项之和，不要另算
4. 只输出 JSON"""

    return prompt


def _parse_score(raw: str) -> dict:
    """解析裁判返回的 JSON，多级容错。失败则返回 None。"""
    text = raw.strip()
    if not text:
        return None

    # 策略1: ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # 策略2: 贪婪匹配 { ... }
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def judge_one(result: dict, references: str = "", retries: int = 3) -> dict:
    """对一份产出打分。返回 {scores, total, comment, provider, model}。"""
    model = os.environ.get("KIMI_MODEL", "kimi-k3")
    print(f"  裁判模型: kimi / {model}")

    if not references:
        references = _get_references(result.get("component_type", ""))

    prompt = _build_score_prompt(result, references)

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            raw = _kimi_chat([{"role": "user", "content": prompt}])
            parsed = _parse_score(raw)
            if parsed and "scores" in parsed:
                scores = parsed.get("scores", {})
                total = parsed.get("total", sum(scores.values()) if scores else 0)
                return {
                    "scores": {
                        "syntax": int(scores.get("syntax", 0)),
                        "consistency": int(scores.get("consistency", 0)),
                        "topology": int(scores.get("topology", 0)),
                        "traceability": int(scores.get("traceability", 0)),
                    },
                    "total": int(total),
                    "comment": parsed.get("comment", ""),
                    "provider": "kimi",
                    "model": model,
                }
            last_err = f"返回非预期 JSON: {raw[:200]}"
        except Exception as e:
            last_err = f"调用失败: {str(e)[:200]}"
        if attempt < retries:
            time.sleep(2)

    return {"scores": {}, "total": None, "comment": f"评分失败: {last_err}",
            "provider": "kimi", "model": model}


def main():
    parser = argparse.ArgumentParser(description="V7 LLM-as-Judge（Kimi 盲评）")
    parser.add_argument("--result", required=True, help="runner 结果 JSON 路径")
    parser.add_argument("--out", required=True, help="评分 JSON 输出路径")
    args = parser.parse_args()

    _load_env()

    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    references = _get_references(result.get("component_type", ""))

    score = judge_one(result, references=references)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8")

    total = score.get("total")
    print(f"[judge] {result.get('version')}:{result.get('case_id')} "
          f"总分={total} 裁判={score.get('provider')}/{score.get('model')}", flush=True)


if __name__ == "__main__":
    main()
