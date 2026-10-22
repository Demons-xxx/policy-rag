# -*- coding: utf-8 -*-
"""PolicyRAG 问答评测脚本。

读取 tests/eval/questions.jsonl，逐题调用 POST /api/ask（NDJSON 流），
统计关键词命中、引用文档/条款命中、拒答是否正确、耗时，输出 JSON 与控制台表格。

常用命令（项目根目录）：
    .venv\\Scripts\\python.exe scripts\\evaluate.py --dry-run
    .venv\\Scripts\\python.exe scripts\\evaluate.py
    .venv\\Scripts\\python.exe scripts\\evaluate.py --base-url http://127.0.0.1:7880 --limit 5
    .venv\\Scripts\\python.exe scripts\\evaluate.py --id q26 --id q27 --verbose

说明：
- expected_keywords 中的每一项都要求命中；用竖线分隔可写多个同义说法，
  例如 "3个工作日|三天|3天"，命中任意一个即可。
- after_update 题的 forbidden_keywords 只作为“旧值提示”记录，不直接判失败：
  回答只要给出新版关键词并引用新版文档即算通过。
- 多轮题（depends_on）会自动把依赖题纳入本轮，并把依赖题的问题/回答作为
  history 一起提交；单跑 --id q15 时也会自动补跑 q14。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUESTIONS = ROOT / "tests" / "eval" / "questions.jsonl"
DEFAULT_BASE_URL = "http://127.0.0.1:7880"
ALLOWED_TYPES = {"direct", "fuzzy", "followup", "multi_turn", "cross_doc", "refuse", "after_update"}
REFUSAL_RE = re.compile(
    r"未在现行(?:有效)?(?:制度|规定|办法|文件)中找到依据|未找到(?:相关)?依据|没有(?:相关)?(?:依据|规定|制度)|"
    r"无法(?:回答|确定|从现有)|未收录|不在(?:现行)?(?:制度|资料|知识库)(?:范围|中)|"
    r"现行(?:有效)?(?:制度|规定)(?:中|里)未(?:作|有)|未涉及"
)
CITE_RE = re.compile(r"\[(?:证据)?(\d+)\]")


# --------------------------------------------------------------------------
# 读取与校验
# --------------------------------------------------------------------------
class ServiceError(RuntimeError):
    pass


def load_questions(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"题目文件不存在：{path}")
    items: list[dict] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{lineno} JSON 解析失败：{exc}")
        item["_line"] = lineno
        items.append(item)
    return items


def validate(items: list[dict]) -> list[str]:
    errors: list[str] = []
    ids = [str(q.get("id", "")) for q in items]
    dup = [k for k, v in Counter(ids).items() if v > 1]
    if dup:
        errors.append(f"题号重复：{', '.join(dup)}")
    pos = {qid: i for i, qid in enumerate(ids)}
    for q in items:
        qid = str(q.get("id", ""))
        where = f"{qid or '?'}（第{q.get('_line')}行）"
        qtype = q.get("type")
        if not qid:
            errors.append(f"{where} 缺少 id")
        if qtype not in ALLOWED_TYPES:
            errors.append(f"{where} type 非法：{qtype!r}，允许 {sorted(ALLOWED_TYPES)}")
        question = q.get("question")
        if not isinstance(question, str) or not question.strip():
            errors.append(f"{where} question 不能为空")
        elif len(question) > 500:
            errors.append(f"{where} question 超过 500 字")
        doc = q.get("expected_doc")
        if qtype == "refuse":
            if doc not in (None, "", []):
                errors.append(f"{where} 拒答题不应设置 expected_doc")
            if q.get("expected_keywords"):
                errors.append(f"{where} 拒答题不应设置 expected_keywords")
        else:
            if not doc:
                errors.append(f"{where} 非拒答题必须设置 expected_doc")
            if isinstance(doc, list):
                if not all(isinstance(v, str) and v.strip() for v in doc):
                    errors.append(f"{where} expected_doc 列表含空值")
            elif not isinstance(doc, str):
                errors.append(f"{where} expected_doc 必须是字符串或字符串列表")
        article = q.get("expected_article")
        if article is not None and not isinstance(article, str):
            errors.append(f"{where} expected_article 必须是字符串或 null")
        keywords = q.get("expected_keywords")
        if not isinstance(keywords, list) or not all(isinstance(v, str) and v.strip() for v in keywords):
            errors.append(f"{where} expected_keywords 必须是字符串列表")
        forbidden = q.get("forbidden_keywords")
        if forbidden is not None and (not isinstance(forbidden, list)
                                      or not all(isinstance(v, str) for v in forbidden)):
            errors.append(f"{where} forbidden_keywords 必须是字符串列表")
        dep = q.get("depends_on")
        if dep is not None:
            if dep not in pos:
                errors.append(f"{where} depends_on 指向不存在的题号：{dep}")
            elif pos[dep] >= pos[qid]:
                errors.append(f"{where} depends_on 必须指向更早的题号：{dep}")
            if qtype != "multi_turn":
                errors.append(f"{where} 只有 multi_turn 题可以设置 depends_on")
    return errors


def select_items(items: list[dict], ids: list[str] | None, limit: int | None) -> list[dict]:
    if not ids:
        chosen = list(items)
    else:
        wanted = set(ids)
        unknown = wanted - {q["id"] for q in items}
        if unknown:
            raise SystemExit(f"未知题号：{', '.join(sorted(unknown))}")
        by_id = {q["id"]: q for q in items}
        need: set[str] = set()

        def expand(qid: str) -> None:
            if qid in need:
                return
            need.add(qid)
            dep = by_id[qid].get("depends_on")
            if dep:
                expand(dep)

        for qid in ids:
            expand(qid)
        order = {q["id"]: i for i, q in enumerate(items)}
        chosen = [q for q in items if q["id"] in need]
        chosen.sort(key=lambda q: order[q["id"]])
    if limit:
        chosen = chosen[:limit]
    return chosen


# --------------------------------------------------------------------------
# 调用 /api/ask
# --------------------------------------------------------------------------
def build_history(q: dict, done: dict[str, dict]) -> list[dict]:
    dep = q.get("depends_on")
    if not dep:
        return []
    prev = done.get(dep)
    if not prev or not prev.get("answer"):
        raise ServiceError(f"依赖题 {dep} 没有可用回答，已跳过本题")
    return [
        {"role": "user", "content": prev["question"]},
        {"role": "assistant", "content": prev["answer"][:4000]},
    ]


def ask(base_url: str, question: str, history: list[dict], timeout: float,
        run_tag: str) -> dict:
    payload = json.dumps({
        "question": question,
        "history": history,
        "run_tag": run_tag,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/ask",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/x-ndjson"},
        method="POST",
    )
    started = time.perf_counter()
    answer_parts: list[str] = []
    sources: list[dict] = []
    events = 0
    done_seconds: float | None = None
    error: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events += 1
                stage = event.get("stage")
                if stage == "answer":
                    answer_parts.append(str(event.get("text") or ""))
                elif stage == "revision":
                    # 后置校验下发的修正稿：整体替换已流式累积的回答
                    answer_parts = [str(event.get("text") or "")]
                elif stage == "sources":
                    items = event.get("items")
                    if isinstance(items, list):
                        sources = items
                elif stage == "done":
                    value = event.get("seconds")
                    if isinstance(value, (int, float)):
                        done_seconds = float(value)
                elif stage == "error":
                    error = str(event.get("message") or "服务返回错误")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = body
        try:
            parsed = json.loads(body)
            detail = parsed.get("detail") or body
        except json.JSONDecodeError:
            pass
        raise ServiceError(f"HTTP {exc.code}：{detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise ServiceError(f"无法连接 {base_url}：{exc.reason}") from exc
    except TimeoutError as exc:
        raise ServiceError(f"请求超时（>{timeout:g}s）") from exc
    wall = time.perf_counter() - started
    answer = "".join(answer_parts).strip()
    if error and not answer:
        raise ServiceError(error)
    return {
        "answer": answer,
        "sources": sources,
        "events": events,
        "seconds": done_seconds if done_seconds is not None else round(wall, 3),
        "wall_seconds": round(wall, 3),
        "server_error": error,
    }


# --------------------------------------------------------------------------
# 判分
# --------------------------------------------------------------------------
def norm(text: str) -> str:
    text = (text or "").replace("\xa0", " ")
    return re.sub(r"\s+", "", text).lower()


def keyword_variants(keyword: str) -> list[str]:
    return [v for v in (part.strip() for part in keyword.split("|")) if v]


def keyword_hit(keyword: str, answer: str) -> bool:
    text = norm(answer)
    return any(norm(v) in text for v in keyword_variants(keyword))


def source_blob(sources: list[dict]) -> str:
    fields = ("title", "file", "doc_no", "version_label", "article", "chapter")
    return "\n".join(str(s.get(f) or "") for s in sources for f in fields)


def match_docs(expected: str | list[str], sources: list[dict]) -> tuple[bool, list[str]]:
    wanted = expected if isinstance(expected, list) else [expected]
    blob = norm(source_blob(sources))
    matched = [w for w in wanted if norm(w) in blob]
    return len(matched) == len(wanted), matched


def match_article(expected: str | None, sources: list[dict], answer: str) -> bool:
    if not expected:
        return True
    target = norm(expected)
    arts = norm(" ".join(str(s.get("article") or "") for s in sources))
    if target in arts:
        return True
    return target in norm(answer)


def score(q: dict, result: dict) -> dict:
    answer = result["answer"]
    sources = result["sources"]
    qtype = q["type"]
    keywords = q.get("expected_keywords") or []
    hits = [kw for kw in keywords if keyword_hit(kw, answer)]
    keyword_ok = len(hits) == len(keywords) and bool(keywords or qtype == "refuse")
    if qtype == "refuse":
        keyword_ok = True
    doc_ok, doc_matched = (True, [])
    if qtype != "refuse":
        doc_ok, doc_matched = match_docs(q["expected_doc"], sources)
    article_ok = match_article(q.get("expected_article"), sources, answer)
    refused = bool(REFUSAL_RE.search(answer))
    forbidden_hits = [kw for kw in (q.get("forbidden_keywords") or []) if keyword_hit(kw, answer)]
    cited = sorted({int(m) for m in CITE_RE.findall(answer)})
    source_ids = {int(s["id"]) for s in sources
                  if isinstance(s.get("id"), (int, str)) and str(s.get("id", "")).isdigit()}
    valid_citations = [n for n in cited if n in source_ids] if source_ids else [
        n for n in cited if 1 <= n <= len(sources)]
    invalid_citations = [n for n in cited if n not in valid_citations]
    evidence_ok = True if qtype == "refuse" else bool(doc_ok and article_ok)

    if qtype == "refuse":
        passed = refused and not cited
    else:
        passed = keyword_ok and doc_ok and article_ok and bool(answer)
    notes = []
    if qtype == "refuse" and cited:
        notes.append("拒答但给出了引用")
    if invalid_citations:
        notes.append("引用越界/悬空：" + "、".join(f"[证据{n}]" for n in invalid_citations))
    if forbidden_hits:
        notes.append("提及旧值/" + "、".join(forbidden_hits))
    if not answer:
        notes.append("空回答")
    return {
        "keyword_hits": hits,
        "keyword_total": len(keywords),
        "keyword_ok": keyword_ok,
        "doc_ok": doc_ok,
        "doc_matched": doc_matched,
        "article_ok": article_ok,
        "refused": refused,
        "refused_with_citations": bool(refused and cited),
        "forbidden_hits": forbidden_hits,
        "citations": cited,
        "valid_citations": valid_citations,
        "invalid_citations": invalid_citations,
        "citation_ok": not invalid_citations,
        "evidence_ok": evidence_ok,
        "passed": passed,
        "note": "；".join(notes),
    }


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, target: int) -> str:
    text = str(text)
    while width(text) > target:
        text = text[:-1]
    return text + " " * (target - width(text))


def print_table(results: list[dict]) -> None:
    header = (pad("ID", 5) + pad("TYPE", 13) + pad("RESULT", 8) + pad("KW", 8)
              + pad("DOC", 6) + pad("ART", 6) + pad("EVID", 6) + pad("REFUSE", 8)
              + pad("CITE", 9) + pad("SEC", 7) + "NOTE")
    print(header)
    print("-" * (width(header) + 12))
    for r in results:
        m = r["metrics"]
        kw = f"{len(m['keyword_hits'])}/{m['keyword_total']}" if m["keyword_total"] else "-"
        doc = "-" if r["type"] == "refuse" else ("ok" if m["doc_ok"] else "MISS")
        art = "-" if not r.get("expected_article") else ("ok" if m["article_ok"] else "MISS")
        ref = "ok" if m["refused"] else "-" if r["type"] != "refuse" else "MISS"
        evid = "-" if r["type"] == "refuse" else ("ok" if m["evidence_ok"] else "MISS")
        cite = (f"{len(m['valid_citations'])}/{len(m['citations'])}"
                if m["citations"] else "-")
        row = (pad(r["id"], 5) + pad(r["type"], 13)
               + pad("PASS" if m["passed"] else "FAIL", 8)
               + pad(kw, 8) + pad(doc, 6) + pad(art, 6) + pad(evid, 6) + pad(ref, 8)
               + pad(cite, 9) + pad(f"{r['seconds']:.1f}", 7)
               + (m["note"] or r.get("error") or ""))
        print(row)


def summarize(results: list[dict], elapsed: float) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r["metrics"]["passed"])
    normal = [r for r in results if r["type"] != "refuse"]
    refuse = [r for r in results if r["type"] == "refuse"]
    with_article = [r for r in normal if r.get("expected_article")]
    secs = sorted(r["seconds"] for r in results if r["seconds"] is not None)

    def rate(n: int, d: int) -> float:
        return round(n / d, 4) if d else 0.0

    by_type: dict[str, dict] = OrderedDict()
    for qtype in ("direct", "fuzzy", "multi_turn", "cross_doc", "refuse", "after_update", "followup"):
        rows = [r for r in results if r["type"] == qtype]
        if not rows:
            continue
        by_type[qtype] = {
            "total": len(rows),
            "passed": sum(1 for r in rows if r["metrics"]["passed"]),
            "pass_rate": rate(sum(1 for r in rows if r["metrics"]["passed"]), len(rows)),
        }
    return {
        "total": total,
        "passed": passed,
        "pass_rate": rate(passed, total),
        "keyword_rate": rate(sum(1 for r in normal if r["metrics"]["keyword_ok"]), len(normal)),
        "doc_cite_rate": rate(sum(1 for r in normal if r["metrics"]["doc_ok"]), len(normal)),
        "article_cite_rate": rate(sum(1 for r in with_article if r["metrics"]["article_ok"]),
                                  len(with_article)),
        "article_checked": len(with_article),
        "refusal_accuracy": rate(sum(1 for r in refuse if r["metrics"]["refused"]), len(refuse)),
        "refusal_checked": len(refuse),
        "evidence_recall_rate": rate(sum(1 for r in normal if r["metrics"]["evidence_ok"]), len(normal)),
        "evidence_checked": len(normal),
        "citations_total": sum(len(r["metrics"]["citations"]) for r in results),
        "citations_valid": sum(len(r["metrics"]["valid_citations"]) for r in results),
        "citation_accuracy": rate(sum(len(r["metrics"]["valid_citations"]) for r in results),
                                  sum(len(r["metrics"]["citations"]) for r in results)),
        "refusal_conflicts": sum(1 for r in results if r["metrics"]["refused_with_citations"]),
        "legacy_value_mentions": sum(1 for r in results if r["metrics"]["forbidden_hits"]),
        "errors": sum(1 for r in results if r.get("error")),
        "avg_seconds": round(statistics.mean(secs), 3) if secs else None,
        "p50_seconds": round(statistics.median(secs), 3) if secs else None,
        "p95_seconds": round(secs[min(len(secs) - 1, int(len(secs) * 0.95))], 3) if secs else None,
        "elapsed_seconds": round(elapsed, 2),
        "by_type": by_type,
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="PolicyRAG 问答评测（调用 /api/ask）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"服务地址，默认 {DEFAULT_BASE_URL}")
    parser.add_argument("--questions", default=str(DEFAULT_QUESTIONS), help="题目 JSONL 路径")
    parser.add_argument("--out", default=None, help="结果 JSON 路径；默认 tests/eval/results-<时间>.json")
    parser.add_argument("--id", action="append", default=None, help="只跑指定题号，可重复")
    parser.add_argument("--limit", type=int, default=None, help="最多跑前 N 题")
    parser.add_argument("--timeout", type=float, default=180.0, help="单题超时秒数，默认 180")
    parser.add_argument("--delay", type=float, default=0.3, help="题间等待秒数，默认 0.3")
    parser.add_argument("--run-tag", default="eval", help="请求里的 run_tag，默认 eval")
    parser.add_argument("--fail-under", type=float, default=0.0, help="通过率低于该值返回码 1")
    parser.add_argument("--dry-run", action="store_true", help="只校验题目，不请求服务")
    parser.add_argument("--verbose", action="store_true", help="失败题额外打印回答摘录")
    parser.add_argument("--no-save", action="store_true", help="不写结果 JSON")
    args = parser.parse_args(argv)

    questions_path = Path(args.questions).expanduser()
    items = load_questions(questions_path)
    errors = validate(items)
    if args.dry_run:
        counts = Counter(q.get("type", "?") for q in items)
        print(f"题目文件：{questions_path}")
        print(f"题量：{len(items)}   类型分布：" + "，".join(f"{k} {v}" for k, v in counts.items()))
        print(f"题目格式：{'通过' if not errors else '失败'}")
        for err in errors:
            print("  - " + err)
        if not errors:
            for q in items:
                dep = f"（依赖 {q['depends_on']}）" if q.get("depends_on") else ""
                print(f"  {q['id']} [{q['type']}] {q['question']}{dep}")
        return 1 if errors else 0
    if errors:
        print("题目格式有误，已停止：")
        for err in errors:
            print("  - " + err)
        return 1
    try:
        selected = select_items(items, args.id, args.limit)
    except SystemExit as exc:
        print(exc)
        return 1
    if not selected:
        print("没有可执行的题目")
        return 1

    base_url = args.base_url.strip().rstrip("/")
    if not re.match(r"^https?://", base_url):
        base_url = "http://" + base_url
    print(f"服务地址：{base_url}")
    print(f"题目文件：{questions_path}（共 {len(selected)} 题）")
    started_at = datetime.now().astimezone()
    started = time.perf_counter()
    done: dict[str, dict] = {}
    results: list[dict] = []

    for index, q in enumerate(selected, 1):
        print(f"[{index}/{len(selected)}] {q['id']} {q['question'][:36]}", flush=True)
        record: dict[str, Any] = {
            "id": q["id"],
            "type": q["type"],
            "question": q["question"],
            "expected_doc": q.get("expected_doc"),
            "expected_article": q.get("expected_article"),
            "expected_keywords": q.get("expected_keywords") or [],
            "depends_on": q.get("depends_on"),
            "answer": "",
            "sources": [],
            "seconds": None,
            "events": 0,
            "error": None,
        }
        try:
            history = build_history(q, done)
            response = ask(base_url, q["question"], history, args.timeout, args.run_tag)
            record.update({
                "answer": response["answer"],
                "sources": [
                    {k: s.get(k) for k in ("id", "doc_id", "title", "file", "doc_no",
                                           "version_label", "article", "chapter", "pages")}
                    for s in response["sources"]
                ],
                "seconds": response["seconds"],
                "events": response["events"],
            })
            if response.get("server_error"):
                record["error"] = response["server_error"]
        except ServiceError as exc:
            record["error"] = str(exc)
            if index == 1 and ("无法连接" in str(exc) or "Connection refused" in str(exc)):
                print("\n服务尚未启动：" + str(exc))
                print("请先启动服务（默认 http://127.0.0.1:7880），或使用 --base-url 指定地址。")
                return 2
        record["metrics"] = score(q, {"answer": record["answer"],
                                      "sources": record["sources"]})
        results.append(record)
        done[q["id"]] = record
        if args.verbose and not record["metrics"]["passed"]:
            snippet = record["answer"].replace("\n", " ")[:160]
            print(f"    ! {record['metrics']['note'] or record['error'] or '未通过'} | {snippet}")
        if args.delay and index < len(selected):
            time.sleep(args.delay)

    elapsed = time.perf_counter() - started
    summary = summarize(results, elapsed)
    print()
    print_table(results)
    print()
    article_text = f"{summary['article_cite_rate']:.1%}" if summary["article_checked"] else "—"
    refusal_text = f"{summary['refusal_accuracy']:.1%}" if summary["refusal_checked"] else "—"
    print(f"通过 {summary['passed']}/{summary['total']}（{summary['pass_rate']:.1%}）  "
          f"关键词 {summary['keyword_rate']:.1%}  文档引用 {summary['doc_cite_rate']:.1%}  "
          f"条款引用 {article_text}  证据召回 {summary['evidence_recall_rate']:.1%}  "
          f"引用准确 {summary['citation_accuracy']:.1%}（{summary['citations_valid']}/{summary['citations_total']}）  "
          f"拒答正确 {refusal_text}  拒答冲突 {summary['refusal_conflicts']}")
    print(f"耗时：平均 {summary['avg_seconds']}s  P50 {summary['p50_seconds']}s  "
          f"P95 {summary['p95_seconds']}s  总 {elapsed:.1f}s")
    for qtype, row in summary["by_type"].items():
        print(f"  {qtype:<12} {row['passed']}/{row['total']}  {row['pass_rate']:.1%}")

    payload = {
        "schema": 1,
        "base_url": base_url,
        "questions_file": str(questions_path),
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": summary,
        "results": results,
    }
    if not args.no_save:
        out = Path(args.out) if args.out else (
            ROOT / "tests" / "eval" / f"results-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"结果已写入：{out}")

    if summary["pass_rate"] < args.fail_under:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
