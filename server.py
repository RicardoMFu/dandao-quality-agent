"""只监听本机的演示服务。正式多人部署须替换为带身份验证的服务。"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from pydantic import ValidationError
from agent.schema import Case, Review
from agent.engine import run_case, safe_case, redact
from agent.evidence import validate_judgment
from agent.rubric import calculate, public_rubric, RULE_VERSION
from agent.judge import settings, JudgeError
from agent.storage import Store
from agent.upstream import group_cases

ROOT = Path(__file__).resolve().parent


def csv_safe(value):
    text = str(value if value is not None else "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # 不将问题原文、错误响应或凭证写入访问日志。

        def valid_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def send(self, value, status=200, content_type="application/json; charset=utf-8"):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.valid_host():
                return self.send({"error": "非法Host"}, 403)
            path = urlparse(self.path).path
            if path == "/api/bootstrap":
                return self.send({"cases": json.loads((ROOT / "examples/cases.json").read_text()), "rubric": public_rubric(), "settings": settings(), "rule_version": RULE_VERSION, "history": store.export()})
            if path == "/api/export":
                return self.send(store.export())
            if path == "/api/export.csv":
                out = io.StringIO()
                writer = csv.writer(out)
                writer.writerow(["run_id", "case_id", "问题", "引擎", "机器等级", "机器总分", "人工复核次数", "最近人工等级", "最近人工理由"])
                data = store.export()
                for run in data["runs"]:
                    reviews = [r for r in data["reviews"] if r["run_id"] == run["run_id"]]
                    latest = reviews[-1] if reviews else None
                    writer.writerow([csv_safe(v) for v in [run["run_id"], run["case"]["id"], run["case"]["question"], run["mode"], run["decision"]["label"], run["decision"]["final_total"], len(reviews), latest["decision"]["label"] if latest else "", latest["reason"] if latest else ""]])
                return self.send(('\ufeff' + out.getvalue()).encode(), content_type="text/csv; charset=utf-8")
            static = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "application/javascript"), "/style.css": ("style.css", "text/css")}
            if path in static:
                name, mime = static[path]
                return self.send((ROOT / "web" / name).read_bytes(), content_type=mime + "; charset=utf-8")
            return self.send({"error": "页面不存在"}, 404)

        def do_POST(self):
            if not self.valid_host():
                return self.send({"error": "非法Host"}, 403)
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                return self.send({"error": "仅接受本机页面请求"}, 403)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send({"error": "只接受JSON"}, 415)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 2_000_000:
                    return self.send({"error": "请求大小超限或为空"}, 413)
                data = json.loads(self.rfile.read(size))
                if self.path == "/api/run":
                    mode = data.get("mode", "rules")
                    if mode not in {"rules", "llm"}:
                        raise ValueError("未知运行模式")
                    run = run_case(Case.model_validate(data["case"]), mode)
                    store.add_run(run)
                    return self.send(run)
                if self.path == "/api/review":
                    review = Review.model_validate(data)
                    run = store.get_run(review.run_id)
                    case = Case.model_validate(run["case"])
                    validate_judgment(review.judgment, case, run["evidence"])
                    if any(c["verdict"] == "contradicted" for c in run["evidence"]["claims"]) and (review.judgment.scores["accuracy"].level or 0) > 1:
                        raise ValueError("已标注事实与来源相反，请先修正回答再重新检查")
                    decision = calculate(review.judgment, review.confirmed_risks, run["evidence"]["holds"])
                    if decision["label"] in {"不通过", "待核实", "人工复核"} and review.resolution == "已解决":
                        raise ValueError("存在风险或未核实依据，不能标为已解决")
                    # 只允许保存经过脱敏的审阅文本。
                    if redact(review.model_dump_json()) != review.model_dump_json():
                        raise ValueError("复核备注含疑似号码或凭证，请先脱敏")
                    return self.send(store.add_review(review, decision))
                if self.path == "/api/import":
                    raw = data["cases"]
                    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
                        raise ValueError("每批支持1—100条JSON案例")
                    cases = [safe_case(Case.model_validate(c)) for c in raw]
                    if len({c.id for c in cases}) != len(cases):
                        raise ValueError("导入案例ID不能重复")
                    return self.send({"cases": [c.model_dump(mode="json") for c in cases], "groups": group_cases(cases)})
                return self.send({"error": "接口不存在"}, 404)
            except ValidationError as exc:
                errors = [".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in exc.errors()]
                return self.send({"error": "；".join(errors)}, 400)
            except JudgeError as exc:
                return self.send({"error": str(exc)}, 502)
            except (ValueError, KeyError, TypeError):
                return self.send({"error": "输入或证据校验失败，请检查字段、引文、标签与判定是否一致"}, 400)
            except Exception:
                return self.send({"error": "服务处理失败，未生成成功结果"}, 500)
    return Handler


def main():
    parser = argparse.ArgumentParser(description="旦DAO质检工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=str(ROOT / ".runtime/quality.sqlite"))
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(Store(args.db)))
    print(f"旦DAO质检工作台 http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
