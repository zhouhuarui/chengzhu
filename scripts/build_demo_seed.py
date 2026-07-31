#!/usr/bin/env python3
"""从当前 uploads 打包 demo_seed（含已完成任务报告）。"""

from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
UPLOADS = os.path.join(ROOT, 'backend', 'uploads')
SEED = os.path.join(ROOT, 'demo_seed')
PRIVATE_MARKERS = (
    'datayes', 'wmcloud.com', 'private_derived_only',
    'datayes_api', 'datayes_warehouse',
)


def detect_private_datayes_artifacts(root: str) -> list[str]:
    """返回可能含采购数据的文件；只报告路径，不输出文件内容。"""
    findings: list[str] = []
    if not os.path.isdir(root):
        return findings
    for current, _, files in os.walk(root):
        for name in files:
            path = os.path.join(current, name)
            rel = os.path.relpath(path, root)
            if name == 'chengzhu.db':
                try:
                    # 扫描整个 SQLite 文件，不只是 evidence_card：凭证、
                    # tool log 或已删除页面中的私有来源痕迹也不应进入 seed。
                    with open(path, 'rb') as database_file:
                        raw = database_file.read().lower()
                    markers = tuple(marker.encode('utf-8') for marker in PRIVATE_MARKERS)
                    if any(marker in raw for marker in markers):
                        findings.append(rel)
                except OSError:
                    pass
                continue
            if os.path.splitext(name)[1].lower() not in {'.json', '.jsonl', '.md', '.txt'}:
                continue
            try:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    text = f.read().lower()
            except OSError:
                continue
            if any(marker in text for marker in PRIVATE_MARKERS):
                findings.append(rel)
    return sorted(set(findings))


def main():
    if not os.path.isdir(UPLOADS):
        print('uploads 不存在')
        sys.exit(1)
    private_files = detect_private_datayes_artifacts(UPLOADS)
    if private_files:
        print('拒绝打包：uploads 含 Datayes 私有/授权数据痕迹。')
        print('请使用只含公开或合成数据的干净 uploads 生成 keyless demo_seed。')
        for path in private_files[:20]:
            print('private artifact:', path)
        sys.exit(2)
    if os.path.isdir(SEED):
        shutil.rmtree(SEED)
    os.makedirs(SEED)

    # 复制 db + 最多 5 个已有任务 + graphs + scenarios + briefs
    for name in ('chengzhu.db',):
        src = os.path.join(UPLOADS, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(SEED, name))
            print('db ok')

    for folder in ('tasks', 'graphs', 'scenarios', 'briefs'):
        src = os.path.join(UPLOADS, folder)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(SEED, folder)
        os.makedirs(dst, exist_ok=True)
        entries = sorted(os.listdir(src))[:8]
        for e in entries:
            s = os.path.join(src, e)
            d = os.path.join(dst, e)
            if os.path.isdir(s):
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
            print('packed', folder, e)

    print('demo_seed ready at', SEED)


if __name__ == '__main__':
    main()
