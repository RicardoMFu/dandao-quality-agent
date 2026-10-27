from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from .schema import Judgment
from .rubric import public_rubric, PROMPT_VERSION
from .evidence import validate_judgment


class JudgeError(Exception):
    pass


def settings():
    return {"configured": bool(os.getenv("JUDGE_BASE_URL") and os.getenv("JUDGE_MODEL")), "model": os.getenv("JUDGE_MODEL", ""), "prompt_version": PROMPT_VERSION}


def judge(case, evidence):
    base, model = os.getenv("JUDGE_BASE_URL", "").rstrip("/"), os.getenv("JUDGE_MODEL", "")
    if not base or not model:
        raise JudgeError("尚未配置项目模型接口，请设置 JUDGE_BASE_URL 与 JUDGE_MODEL")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise JudgeError("模型接口必须使用HTTPS；本机测试可使用HTTP")
    headers = {"Content-Type": "application/json"}
    if os.getenv("JUDGE_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["JUDGE_API_KEY"]
    prompt = (Path(__file__).resolve().parents[1] / "prompts/judge.md").read_text()
    example_score = {"level": None, "reason": "缺少依据", "source_ids": [], "answer_quote": "", "source_quote": ""}
    example = {"scores": {d["id"]: example_score for d in public_rubric()}, "findings": [], "missing_points": [], "summary": "逐项核对结论"}
    payload = {"model": model, "messages": [
        {"role": "system", "content": prompt + "\n评分锚点：" + json.dumps(public_rubric(), ensure_ascii=False) + "\nJSON结构：" + json.dumps(example, ensure_ascii=False)},
        {"role": "user", "content": json.dumps({"case": case.model_dump(mode="json"), "evidence_check": evidence}, ensure_ascii=False)},
    ], "response_format": {"type": "json_object"}, "max_tokens": 5000}
    request = urllib.request.Request(base + "/chat/completions", data=json.dumps(payload).encode(), headers=headers, method="POST")
    start = time.monotonic()
    try:
        # 禁止重定向，防止接口重定向把Authorization转发到其他站点。
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None
        with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
            body = json.loads(response.read(512000))
        choice = body["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise JudgeError("模型输出未正常结束，未生成评分")
        result = Judgment.model_validate_json(choice["message"]["content"])
        validate_judgment(result, case, evidence)
    except JudgeError:
        raise
    except urllib.error.HTTPError as exc:
        raise JudgeError(f"模型接口返回HTTP {exc.code}，未生成评分") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise JudgeError("模型接口连接失败或超时，未生成评分") from None
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        # 不回传原始响应，避免日志/错误信息携带密钥或敏感问答。
        raise JudgeError("模型输出未通过九维字段或证据校验，未生成评分") from exc
    return result, {"model": model, "elapsed_ms": round((time.monotonic() - start) * 1000), "usage": body.get("usage", {}), "prompt_version": PROMPT_VERSION}
