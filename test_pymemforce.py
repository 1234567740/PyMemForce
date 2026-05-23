"""
PyMemForce v2.0.0 完整功能测试脚本
运行: python test_pymemforce.py
测试覆盖: GC停顿、对象池、多线程、确定性、环形缓冲、野指针检测、内存碎片
"""

import pymemforce as pmf
import time
import gc
import threading
import statistics
import random


def test_all():
    print("=" * 60)
    print("PyMemForce v2.0.0 完整功能测试")
    print("=" * 60)

    # ==========================================
    # 测试1：GC 停顿对比
    # ==========================================
    print("\n[1] GC 停顿测试 (100MB)")
    size = 100 * 1024 * 1024

    data = bytearray(size)
    gc.collect()
    start = time.perf_counter()
    gc.collect()
    without = (time.perf_counter() - start) * 1000
    del data
    gc.collect()

    buf = pmf.buffer(size)
    gc.collect()
    start = time.perf_counter()
    gc.collect()
    with_pmf = (time.perf_counter() - start) * 1000
    buf.release()

    print(f"  Python 默认:  {without:.1f}ms GC 停顿")
    print(f"  PyMemForce:   {with_pmf:.1f}ms GC 停顿")
    if with_pmf > 0:
        print(f"  提升:          {without/with_pmf:.0f}x")

    # ==========================================
    # 测试2：对象池 vs Python 对象创建
    # ==========================================
    print("\n[2] 对象池测试 (50000 次创建)")

    class Obj:
        __slots__ = ['x', 'y', 'z']
        def __init__(self):
            self.x = self.y = self.z = 0

    COUNT = 50000

    gc.disable()
    start = time.perf_counter()
    objs = [Obj() for _ in range(COUNT)]
    for o in objs:
        _ = o.x + o.y + o.z
    py_time = (time.perf_counter() - start) * 1000
    gc.enable()
    gc.collect()

    sb = pmf.sandbox(10 * 1024 * 1024)
    pool = sb.pool(24, COUNT)

    gc.disable()
    start = time.perf_counter()
    ptrs = [pool.alloc() for _ in range(COUNT)]
    for p in ptrs:
        _ = sb.read(p)
    pmf_time = (time.perf_counter() - start) * 1000
    gc.enable()

    for p in ptrs:
        pool.free(p)

    print(f"  Python 对象:  {py_time:.1f}ms")
    print(f"  对象池:       {pmf_time:.1f}ms")
    print(f"  提升:         {py_time/pmf_time:.1f}x")

    # ==========================================
    # 测试3：多线程缓存行对齐（伪共享测试）
    # ==========================================
    print("\n[3] 多线程伪共享测试 (4线程 x 250000次)")

    ITER = 1000000
    THREADS = 4

    data = bytearray(128)
    def worker_py(idx):
        off = idx * 32
        for _ in range(ITER // THREADS):
            v = int.from_bytes(data[off:off+4], 'little') + 1
            data[off:off+4] = v.to_bytes(4, 'little')

    start = time.perf_counter()
    threads = [threading.Thread(target=worker_py, args=(i,)) for i in range(THREADS)]
    for t in threads: t.start()
    for t in threads: t.join()
    py_time = (time.perf_counter() - start) * 1000

    pool2 = sb.pool(64, 10)
    ptrs2 = [pool2.alloc() for _ in range(THREADS)]

    def worker_pmf(idx):
        ptr = ptrs2[idx]
        for _ in range(ITER // THREADS):
            sb.write(ptr, sb.read(ptr) + 1)

    start = time.perf_counter()
    threads = [threading.Thread(target=worker_pmf, args=(i,)) for i in range(THREADS)]
    for t in threads: t.start()
    for t in threads: t.join()
    pmf_time = (time.perf_counter() - start) * 1000

    for p in ptrs2:
        pool2.free(p)

    print(f"  Python 默认:  {py_time:.1f}ms")
    print(f"  缓存行对齐:   {pmf_time:.1f}ms")
    print(f"  提升:         {py_time/pmf_time:.1f}x")

    # ==========================================
    # 测试4：确定性分配延迟
    # ==========================================
    print("\n[4] 确定性分配延迟测试 (1000 次)")

    pool3 = sb.pool(64, 5000)
    times = []
    for _ in range(1000):
        start = time.perf_counter()
        p = pool3.alloc()
        elapsed = (time.perf_counter() - start) * 1_000_000
        times.append(elapsed)
        pool3.free(p)

    print(f"  平均:  {statistics.mean(times):.1f} 微秒")
    print(f"  中位数: {statistics.median(times):.1f} 微秒")
    print(f"  P99:   {sorted(times)[990]:.1f} 微秒")
    print(f"  最大:   {max(times):.1f} 微秒")

    # ==========================================
    # 测试5：环形缓冲区吞吐量
    # ==========================================
    print("\n[5] 环形缓冲区测试 (50MB)")

    ring = pmf.ring(10 * 1024 * 1024)
    chunk = b'X' * 4096
    total_target = 50 * 1024 * 1024

    start = time.perf_counter()
    written = 0
    while written < total_target:
        written += ring.write(chunk)
        if ring.avail_read >= 4096:
            ring.read(4096)
    ring_time = (time.perf_counter() - start) * 1000

    print(f"  处理 50MB:  {ring_time:.1f}ms")
    print(f"  吞吐量:     {total_target/ring_time*1000/1024/1024:.1f} MB/s")

    # ==========================================
    # 测试6：内存碎片对比
    # ==========================================
    print("\n[6] 内存碎片测试 (10000 次随机分配)")

    pool4 = sb.pool(256, 5000)
    start = time.perf_counter()
    ptrs_fixed = []
    failures_fixed = 0
    for i in range(10000):
        try:
            ptrs_fixed.append(pool4.alloc())
        except:
            failures_fixed += 1
        if len(ptrs_fixed) > 100:
            pool4.free(ptrs_fixed.pop(random.randint(0, 99)))
    fixed_time = (time.perf_counter() - start) * 1000

    start = time.perf_counter()
    objs_py = []
    failures_py = 0
    for i in range(10000):
        try:
            objs_py.append(bytearray(256))
        except MemoryError:
            failures_py += 1
        if len(objs_py) > 100:
            objs_py.pop(random.randint(0, 99))
    py_time2 = (time.perf_counter() - start) * 1000

    print(f"  固定池:      {fixed_time:.1f}ms, 失败: {failures_fixed}")
    print(f"  Python 对象: {py_time2:.1f}ms, 失败: {failures_py}")

    # ==========================================
    # 测试7：野指针检测
    # ==========================================
    print("\n[7] 野指针检测测试")

    p = sb.alloc(256)

    sb.delete(p)
    try:
        sb.write(p, 100)
        print("  重复释放: 未检测到（异常）")
    except Exception as e:
        print(f"  重复释放: 成功捕获 ({e.__class__.__name__})")

    ptr = sb.ptr()
    try:
        ptr.shift(999999).read_i32()
        print("  越界访问: 未检测到（异常）")
    except Exception as e:
        print(f"  越界访问: 成功捕获 ({e.__class__.__name__})")

    # ==========================================
    # 测试8：大内存分配速度
    # ==========================================
    print("\n[8] 大内存分配速度测试")

    for size_mb in [10, 50, 100]:
        size_bytes = size_mb * 1024 * 1024

        start = time.perf_counter()
        data = bytearray(size_bytes)
        py_alloc = (time.perf_counter() - start) * 1000
        del data

        start = time.perf_counter()
        buf2 = pmf.buffer(size_bytes)
        pmf_alloc = (time.perf_counter() - start) * 1000
        buf2.release()

        print(f"  {size_mb}MB: Python {py_alloc:.1f}ms | PyMemForce {pmf_alloc:.1f}ms")

    # ==========================================
    # 测试9：沙盒统计
    # ==========================================
    print("\n[9] 沙盒统计")
    stats = sb.stats()
    print(f"  沙盒大小: {stats['size'] / 1024 / 1024:.0f}MB")
    print(f"  基地址:   {stats['base_address']}")

    # ==========================================
    # 测试10：CPU拓扑
    # ==========================================
    print("\n[10] CPU 拓扑信息")
    topo = pmf.topology()
    print(f"  缓存行:   {topo['cache_line']} 字节")
    print(f"  NUMA节点: {topo['numa_nodes']}")
    print(f"  总核心:   {topo['total_cores']}")

    # ==========================================
    # 清理
    # ==========================================
    sb.close()

    print("\n" + "=" * 60)
    print("所有测试完成！")
    print("=" * 60)


if __name__ == "__main__":
    test_all()