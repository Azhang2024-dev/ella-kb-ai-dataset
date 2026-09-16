"""并发执行工具：让抽取 / 语义复核 / 生题这类"逐条独立"的阶段并行跑。

为什么需要它
------------
流水线里三个阶段天然是"每条互不依赖"的：

- extract：读各自的本地文件、各自解析，纯 CPU + 磁盘 IO；
- screen 的语义复核：每条一次独立模型调用；
- query 生成：每份文档一次独立模型调用。

串行跑时，绝大部分时间耗在等待（模型网络往返、大文件 IO）。实测 30 篇中文文档
生题阶段几乎全花在模型往返上，并发 4-8 倍提速，且不改变任何业务结论。

设计原则
--------
1. **worker 只做纯计算/网络，不碰 SQLite**。状态写入一律回到主线程按原顺序做，
   这样断点续跑、幂等 upsert、commit 节奏都与串行版完全一致，可复现性不受影响；
2. 顺序无关，但输出顺序必须与输入一致（用 futures 的 index 回填），
   否则报告里的行序会随线程调度漂移，破坏"同输入同输出"的验收；
3. 单条失败不会拖垮整批：异常按 item 捕获，由调用方决定落成 needs_review 还是 failed；
4. concurrency <= 1 时退化为原地顺序执行，不引入任何线程开销。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def map_ordered(worker: Callable[[int, T], R], items: Sequence[T], *,
                concurrency: int = 1, on_result: Callable[[int, R], None] | None = None,
                ) -> list[R]:
    """对 items 并发执行 worker(index, item)，返回与输入等长、顺序一致的 results。

    worker 里抛出的异常会被捕获并作为结果返回（调用方自行判断/记录），
    以保证一个坏条目不会让整批失败。

    on_result(index, result) 在**主线程**按输入顺序回调，适合做落库/写报告。
    """
    n = len(items)
    results: list[Any] = [None] * n
    if n == 0:
        return results  # type: ignore[return-value]

    workers = max(1, int(concurrency))
    if workers <= 1 or n == 1:
        for idx, item in enumerate(items):
            try:
                res: Any = worker(idx, item)
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
                res = exc
            results[idx] = res
            if on_result is not None:
                on_result(idx, res)
        return results  # type: ignore[return-value]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_index = {pool.submit(worker, idx, item): idx
                        for idx, item in enumerate(items)}
        for future in future_index:
            idx = future_index[future]
            try:
                results[idx] = future.result()
            except Exception as exc:  # noqa: BLE001
                results[idx] = exc
    if on_result is not None:
        for idx in range(n):
            on_result(idx, results[idx])
    return results  # type: ignore[return-value]


def is_exception(value: Any) -> bool:
    return isinstance(value, BaseException)
