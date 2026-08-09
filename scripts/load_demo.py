#!/usr/bin/env python3
"""一键载入演示数据到 backend/uploads（评委无 API Key 可浏览）。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SEED = os.path.join(ROOT, 'demo_seed')
UPLOADS = os.path.join(ROOT, 'backend', 'uploads')


def _write_json_atomic(path: str, payload: dict) -> None:
    """Replace one JSON file without leaving a partially written replay bundle."""

    fd, temporary = tempfile.mkstemp(
        prefix=f'.{os.path.basename(path)}.',
        suffix='.tmp',
        dir=os.path.dirname(path),
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _mark_json_replay(path: str) -> bool:
    """Mark a copied task/run descriptor as replay-only.

    ``execution_mode`` is written both on the descriptor and its TaskCard so
    old readers can recognise the bundle while new confirmation endpoints can
    reject attempts to execute it as a live AgentTeams task.
    """

    with open(path, 'r', encoding='utf-8') as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f'演示描述文件必须是 JSON object：{path}')
    payload['execution_mode'] = 'replay'
    task_card = payload.get('task_card')
    if isinstance(task_card, dict):
        task_card['execution_mode'] = 'replay'
    _write_json_atomic(path, payload)
    return isinstance(task_card, dict)


def _mark_database_replay(path: str) -> int:
    """Mark every TaskCard in a freshly copied seed database as replay-only."""

    connection = sqlite3.connect(path)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_run'"
        ).fetchone()
        if not table:
            return 0
        rows = connection.execute(
            'SELECT run_id, task_card_json FROM task_run'
        ).fetchall()
        updates = []
        for run_id, raw_task_card in rows:
            task_card = json.loads(raw_task_card)
            if not isinstance(task_card, dict):
                raise ValueError(f'task_run {run_id} 的 TaskCard 不是 JSON object')
            task_card['execution_mode'] = 'replay'
            updates.append((json.dumps(task_card, ensure_ascii=False), run_id))
        connection.executemany(
            'UPDATE task_run SET task_card_json = ? WHERE run_id = ?', updates
        )
        connection.commit()
        return len(updates)
    finally:
        connection.close()


def mark_loaded_demo_replay(uploads_dir: str, copied: list[str]) -> dict[str, int]:
    """Apply replay metadata only to top-level entries copied in this call."""

    stats = {'task_files': 0, 'run_files': 0, 'task_cards': 0, 'database_runs': 0}
    if 'tasks' in copied:
        tasks_dir = os.path.join(uploads_dir, 'tasks')
        for current, _, files in os.walk(tasks_dir):
            for name in files:
                if name not in {'task.json', 'run.json'}:
                    continue
                path = os.path.join(current, name)
                has_task_card = _mark_json_replay(path)
                stats['task_files' if name == 'task.json' else 'run_files'] += 1
                stats['task_cards'] += int(has_task_card)
    if 'chengzhu.db' in copied:
        stats['database_runs'] = _mark_database_replay(
            os.path.join(uploads_dir, 'chengzhu.db')
        )
    return stats


def load_demo(seed_dir: str = SEED, uploads_dir: str = UPLOADS, *, force: bool = False) -> list[str]:
    """Copy one demo seed into an uploads directory and return copied names.

    The parameterised form keeps the normal CLI behaviour while allowing the
    keyless package to be verified in a temporary directory without touching a
    developer's real uploads.
    """

    if not os.path.isdir(seed_dir):
        raise FileNotFoundError(f'demo_seed 不存在：{seed_dir}')
    os.makedirs(uploads_dir, exist_ok=True)
    copied: list[str] = []
    for name in os.listdir(seed_dir):
        src = os.path.join(seed_dir, name)
        dst = os.path.join(uploads_dir, name)
        if os.path.isdir(src):
            if os.path.exists(dst):
                if not force:
                    print(f'skip existing {name} (use --force)')
                    continue
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            print(f'copied dir {name}')
        else:
            if os.path.exists(dst) and not force:
                print(f'skip existing {name}')
                continue
            shutil.copy2(src, dst)
            print(f'copied file {name}')
        copied.append(name)
    replay_stats = mark_loaded_demo_replay(uploads_dir, copied)
    if copied:
        print(
            'marked replay metadata '
            f"(task={replay_stats['task_files']}, run={replay_stats['run_files']}, "
            f"db_runs={replay_stats['database_runs']})"
        )
    return copied


def main():
    parser = argparse.ArgumentParser(description='Load Chengzhu demo seed')
    parser.add_argument('--force', action='store_true', help='覆盖已有 uploads')
    parser.add_argument('--seed-dir', default=SEED, help=argparse.SUPPRESS)
    parser.add_argument('--uploads-dir', default=UPLOADS, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if not os.path.isdir(args.seed_dir):
        print(f'demo_seed 不存在：{args.seed_dir}，请先运行 scripts/build_demo_seed.py')
        sys.exit(1)
    load_demo(args.seed_dir, args.uploads_dir, force=args.force)
    print('Demo seed loaded. Start backend and open frontend.')


if __name__ == '__main__':
    main()
