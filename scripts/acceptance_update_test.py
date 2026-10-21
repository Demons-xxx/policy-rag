"""验收脚本：新制度零准备问答 + 知识库更新即时生效。

流程：
1. 生成一份知识库里没有的制度 DOCX（v1：提前 1 个工作日预约）；
2. 通过 /api/ingest 上传 → 等任务完成 → /api/publish 发布；
3. 提问，期望回答出现「1 个工作日」（此前知识库没有这份文件，属于零准备）；
4. 生成同主题修订版（v2：提前 3 个工作日），上传并发布；
5. 同一问题再问一次，期望回答变成「3 个工作日」，且提示新版本生效。
整个过程不重启服务。

用法：
    .\\.venv\\Scripts\\python.exe scripts/acceptance_update_test.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
from docx import Document

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

QUESTION = '会议室需要提前几天预约？'
TITLE = 'XX大学会议室预约使用管理办法'


def build_docx(path: Path, *, version: str, days: str, effective: str, doc_no: str) -> None:
    document = Document()
    document.add_heading(f'{TITLE}（{version}）', level=1)
    document.add_paragraph(f'文号：{doc_no}    施行日期：{effective}')
    document.add_paragraph('第一章  总则')
    document.add_paragraph(f'第一条  为规范学校会议室使用管理，提高会议室使用效率，制定本办法。')
    document.add_paragraph('第二条  校长办公室为会议室归口管理部门，负责会议室统筹调配与监督检查。')
    document.add_paragraph('第二章  预约与使用')
    document.add_paragraph('第三条  校内单位和教职工通过线上预约系统提交会议室使用申请。')
    document.add_paragraph(f'第四条  会议室应当提前{days}通过线上系统预约，经归口部门确认后方可使用。')
    document.add_paragraph('第五条  使用单位应当爱护室内设备，使用结束后恢复桌椅并关闭电源。')
    document.add_paragraph('第六条  无故占用会议室或超时使用的，归口部门可以暂停其预约权限。')
    document.add_paragraph('第三章  附则')
    document.add_paragraph('第七条  本办法由校长办公室负责解释，自施行之日起执行。')
    document.save(path)


def wait_job(client: httpx.Client, base: str, job_id: str, timeout: float = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f'{base}/api/ingest/{job_id}', timeout=30).json()
        if str(job.get('status')) in ('done', 'failed'):
            return job
        time.sleep(2)
    raise TimeoutError(f'入库任务超时：{job_id}')


def upload(client: httpx.Client, base: str, path: Path, meta: dict) -> str:
    with path.open('rb') as handle:
        response = client.post(
            base + '/api/ingest',
            files={'files': (path.name, handle, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
            data={'metadata': json.dumps(meta, ensure_ascii=False)},
            timeout=120,
        )
    response.raise_for_status()
    return response.json()['id']


def publish(client: httpx.Client, base: str) -> dict:
    response = client.post(base + '/api/publish', json={}, timeout=900)
    response.raise_for_status()
    return response.json()


def ask(client: httpx.Client, base: str, question: str, timeout: float) -> dict:
    events: list[dict] = []
    with client.stream('POST', base + '/api/ask', json={'question': question, 'history': []},
                       timeout=timeout) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.strip():
                events.append(json.loads(line))
    answer = ''.join(e.get('text', '') for e in events if e.get('stage') == 'answer')
    sources = next((e.get('items') for e in events if e.get('stage') == 'sources'), [])
    notes = next((e.get('notes') for e in events if e.get('stage') == 'evidence'), [])
    error = next((e.get('message') for e in events if e.get('stage') == 'error'), None)
    return {'question': question, 'answer': answer, 'sources': sources, 'notes': notes, 'error': error}


def find_doc(client: httpx.Client, base: str, title: str) -> dict | None:
    documents = client.get(base + '/api/documents', timeout=30).json().get('documents', [])
    for doc in documents:
        if str(doc.get('title') or '').startswith(title):
            return doc
    return None


def reset_documents() -> list[str]:
    """删掉上一次验收留下的同主题文档，保证本次从干净状态开始。"""
    import shutil

    from app.store.catalog import Catalog

    catalog = Catalog(config.CATALOG_DB)
    removed: list[str] = []
    try:
        for doc in catalog.list_documents():
            title = str(doc.get('title') or '')
            if title.startswith(TITLE):
                shutil.rmtree(config.DOCS_DIR / doc['doc_id'], ignore_errors=True)
                catalog.delete_document(doc['doc_id'])
                removed.append(doc['doc_id'])
    finally:
        catalog.close()
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description='新制度零准备 + 更新即时生效验收')
    parser.add_argument('--base-url', default='http://127.0.0.1:7880')
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    config.ensure_dirs()
    workspace = config.DATA_DIR / 'tmp' / 'acceptance'
    workspace.mkdir(parents=True, exist_ok=True)

    report: dict = {'base_url': base, 'checks': [], 'started_at': time.strftime('%Y-%m-%d %H:%M:%S')}

    def record(name: str, ok: bool, detail: str = '') -> None:
        report['checks'].append({'name': name, 'ok': bool(ok), 'detail': str(detail)[:600]})
        print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else ' | ' + str(detail)[:200]))

    v1 = workspace / f'{TITLE}（2026年版）.docx'
    v2 = workspace / f'{TITLE}（2026年修订）.docx'
    build_docx(v1, version='2026年版', days='1个工作日', effective='2026-03-01', doc_no='校办〔2026〕3号')
    build_docx(v2, version='2026年修订', days='3个工作日', effective='2026-10-01', doc_no='校办〔2026〕21号')

    with httpx.Client() as client:
        removed = reset_documents()
        if removed:
            print('已清理上次验收文档：' + '、'.join(removed), flush=True)
        before = client.get(base + '/api/info', timeout=20).json()
        report['before'] = before

        # ---- v1 入库 ----
        meta1 = {'title': f'{TITLE}（2026年版）', 'doc_no': '校办〔2026〕3号', 'version_label': '2026年版',
                 'effective_from': '2026-03-01', 'effective_to': None,
                 'department': '校长办公室', 'status': 'active'}
        job1 = upload(client, base, v1, meta1)
        state1 = wait_job(client, base, job1)
        record('v1 上传任务完成', state1.get('status') == 'done', state1)
        doc1 = find_doc(client, base, TITLE)
        record('v1 已登记为文档', bool(doc1), doc1)
        release1 = publish(client, base)
        record('v1 发布成功', bool(release1.get('id')), release1.get('id'))
        report['release_v1'] = release1

        first = ask(client, base, QUESTION, args.timeout)
        record('v1 零准备问答有回答', bool(first['answer']) and not first['error'], first['error'] or '')
        record('v1 回答命中「1个工作日」', '1个工作日' in first['answer'].replace(' ', ''), first['answer'][:300])
        record('v1 引用指向新文档', any(TITLE in str(s.get('title') or '') for s in first['sources']),
               first['sources'][:2])
        report['ask_v1'] = first

        # ---- v2 修订版入库 ----
        meta2 = {'title': f'{TITLE}（2026年修订）', 'doc_no': '校办〔2026〕21号', 'version_label': '2026年修订版',
                 'effective_from': '2026-10-01', 'effective_to': None,
                 'department': '校长办公室', 'status': 'active',
                 'supersedes': (doc1 or {}).get('id')}
        job2 = upload(client, base, v2, meta2)
        state2 = wait_job(client, base, job2)
        record('v2 上传任务完成', state2.get('status') == 'done', state2)
        release2 = publish(client, base)
        record('v2 发布成功', bool(release2.get('id')), release2.get('id'))
        report['release_v2'] = release2

        after = client.get(base + '/api/info', timeout=20).json()
        report['after'] = after
        record('发布后服务未重启且版本已切换',
               after.get('release_id') != before.get('release_id'), f"{before.get('release_id')} -> {after.get('release_id')}")

        second = ask(client, base, QUESTION, args.timeout)
        record('更新后同一问题有回答', bool(second['answer']) and not second['error'], second['error'] or '')
        record('更新后回答切换为「3个工作日」', '3个工作日' in second['answer'].replace(' ', ''), second['answer'][:300])
        used_new = any('修订' in str(item.get('title') or '') or '修订' in str(item.get('file') or '')
                       for item in second['sources'])
        record('更新后引用新版文档', used_new, second['sources'][:2])
        report['ask_v2'] = second

    failed = [c for c in report['checks'] if not c['ok']]
    report['passed'] = len(report['checks']) - len(failed)
    report['total'] = len(report['checks'])
    report['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    out = workspace / ('acceptance-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"\n{report['passed']}/{report['total']} 通过，报告：{out}")
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
