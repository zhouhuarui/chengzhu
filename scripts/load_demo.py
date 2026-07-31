#!/usr/bin/env python3
"""一键载入演示数据到 backend/uploads（评委无 API Key 可浏览）。"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SEED = os.path.join(ROOT, 'demo_seed')
UPLOADS = os.path.join(ROOT, 'backend', 'uploads')


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
