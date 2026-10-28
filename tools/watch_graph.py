"""盯着一次图谱构建任务，直到结束。

用法：python tools/watch_graph.py <novel_id> [--interval 120]
（不依赖 backend 的 venv，只用标准库 + urllib）

存在的理由：图谱重建是 1-2 小时的后台活，需要一条能逐行看进度的命令，
而不是每隔几分钟手点一次 /graph/status。
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime

API = "http://127.0.0.1:8000/api/v1/graph/status?novel_id="


def fetch(novel_id: str) -> dict:
    with urllib.request.urlopen(API + novel_id, timeout=15) as resp:
        return json.load(resp)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("novel_id")
    ap.add_argument("--interval", type=int, default=120, help="轮询间隔（秒）")
    args = ap.parse_args()

    while True:
        try:
            data = fetch(args.novel_id)
        except Exception as exc:  # noqa: BLE001
            print(f"{datetime.now():%H:%M:%S}  查询失败：{exc}", flush=True)
            time.sleep(args.interval)
            continue

        job = data.get("job") or {}
        status = job.get("status") or "NONE"
        stamp = datetime.now().strftime("%H:%M:%S")
        if status in ("PENDING", "RUNNING"):
            print(
                f"{stamp}  {status:8} {job.get('progress') or 0:5.1f}%  "
                f"章 {job.get('chapters_done')}/{job.get('chapters_total') or '?'}  "
                f"{job.get('stage')}",
                flush=True,
            )
            time.sleep(args.interval)
            continue

        print(f"{stamp}  {status}  {json.dumps(job.get('stats'), ensure_ascii=False)}", flush=True)
        if job.get("error"):
            print(f"  error: {job['error']}", flush=True)
        break


if __name__ == "__main__":
    main()
