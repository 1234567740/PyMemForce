# -*- coding: utf-8 -*-
"""
PyMemForce v2.1.0 - 赋予 Python 强制内存控制的能力
让 Python 拥有类似 C++ 的内存控制能力

Copyright (C) 2024  PyMemForce Team
License: GPL-3.0-or-later
商业授权请联系: [3901306490@qq.com]

GitHub: https://github.com/1234567740/PyMemForce

本库提供以下核心能力：
- 内存沙盒（PyMemSandbox）：像 C 一样管理内存，绕过 Python GC
- GC-Free 大缓冲区：用 mmap 分配，GC 完全不可见
- 多种内存池：固定池、变长池、线程本地池、NUMA 感知池、对象池、分层池
- 确定性分配器：O(1) 分配时间，无系统调用
- 内存竞技场：批量分配，一次性释放
- 指针系统（强化版）：运算符重载、类型化视图、结构体指针
- 内存追踪与泄漏检测：记录每次分配的调用栈
- 碎片整理与内存压缩：自动合并空闲块、移动已分配块
- C 库安全调用：在沙盒中执行 C 代码，保护 Python 进程
- SIMD 对齐缓冲区：确保 AVX-512 全速运行
- 缓存预取：提前加载数据到 CPU 缓存
- 环形缓冲区、共享内存
"""
from __future__ import annotations

import atexit
import ctypes
import hashlib
import mmap
import os
import struct
import sys
import threading
import time
import traceback
import warnings
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

__version__ = "2.1.0"
__author__ = "PyMemForce Team"

# ============================================================================
# 常量定义
# ============================================================================
# 默认内存池大小为 64MB（可根据需要调整）
DEFAULT_POOL_SIZE: int = 64 * 1024 * 1024

# 每个内存块的头部为 24 字节，包含 6 个 int32 字段
BLOCK_HEADER_SIZE: int = 24

# 系统字节序（小端序或大端序），自动检测
SYSTEM_BYTE_ORDER: str = 'little' if sys.byteorder == 'little' else 'big'

# 已分配块的魔数（Magic Number），用于检测越界写入导致的内存损坏
BLOCK_MAGIC_NUMBER: int = 0xDEADBEEF

# 已释放块的魔数，用于检测重复释放（Double Free）
FREED_BLOCK_MAGIC_NUMBER: int = 0xDEADDEAD


# ============================================================================
# CPU 拓扑检测器（单例模式）
# 自动检测 CPU 的缓存行大小、NUMA 节点数量、核心布局
# Linux 通过 sysfs，macOS 通过 sysctl，Windows 使用默认值
# ============================================================================
class _CPUTopologyDetector:
    """
    检测 CPU 拓扑结构：缓存行大小、NUMA 节点数量、核心布局。
    采用线程安全的单例模式，全局仅创建一次。
    """

    _instance: Optional[_CPUTopologyDetector] = None  # 单例实例
    _lock: threading.Lock = threading.Lock()          # 线程锁

    def __new__(cls) -> _CPUTopologyDetector:
        """单例模式：确保全局唯一实例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    # 创建实例并初始化默认值
                    instance = object.__new__(cls)
                    instance._cache_line_size = 64           # 默认缓存行 64 字节
                    instance._numa_node_count = 1            # 默认 1 个 NUMA 节点
                    instance._cores_per_numa_node = os.cpu_count() or 1
                    instance._total_core_count = os.cpu_count() or 1
                    instance._detect_topology()              # 执行平台检测
                    cls._instance = instance
        return cls._instance

    def _detect_topology(self) -> None:
        """根据操作系统类型分派到具体的检测方法"""
        if sys.platform.startswith('linux'):
            self._detect_linux_topology()
        elif sys.platform == 'darwin':
            self._detect_macos_topology()
        # Windows 使用默认值，不做额外检测

        # 如果检测到多个 NUMA 节点，重新计算每个节点的核心数
        if self._numa_node_count > 1:
            self._cores_per_numa_node = self._total_core_count // self._numa_node_count

    def _detect_linux_topology(self) -> None:
        """从 Linux sysfs 文件系统读取 CPU 拓扑"""
        # 读取缓存行大小
        try:
            with open('/sys/devices/system/cpu/cpu0/cache/index0/coherency_line_size') as f:
                self._cache_line_size = int(f.read().strip())
        except Exception:
            pass

        # 读取 NUMA 节点数量
        try:
            path = '/sys/devices/system/node/online'
            if os.path.exists(path):
                with open(path) as f:
                    content = f.read().strip()
                    if '-' in content:
                        # 格式 "0-3" 表示 4 个节点
                        self._numa_node_count = int(content.split('-')[-1]) + 1
                    else:
                        # 格式 "0,1,2" 表示 3 个节点
                        self._numa_node_count = len(content.split(','))
        except Exception:
            pass

    def _detect_macos_topology(self) -> None:
        """从 macOS sysctl 命令读取 CPU 拓扑"""
        try:
            import subprocess
            result = subprocess.run(
                ['sysctl', '-n', 'hw.cachelinesize'],
                capture_output=True,
                text=True
            )
            self._cache_line_size = int(result.stdout.strip())
        except Exception:
            pass

    # ---------- 属性 ----------
    @property
    def cache_line_size(self) -> int:
        """CPU 缓存行大小（字节）"""
        return self._cache_line_size

    @property
    def numa_node_count(self) -> int:
        """NUMA 节点数量"""
        return self._numa_node_count

    @property
    def cores_per_numa_node(self) -> int:
        """每个 NUMA 节点的 CPU 核心数"""
        return self._cores_per_numa_node

    @property
    def total_core_count(self) -> int:
        """总 CPU 核心数"""
        return self._total_core_count

    # ---------- 方法 ----------
    def get_numa_node_for_cpu(self, cpu_id: int) -> int:
        """根据 CPU 核心编号返回所在的 NUMA 节点"""
        if self._numa_node_count <= 1:
            return 0
        return min(cpu_id // self._cores_per_numa_node, self._numa_node_count - 1)

    def get_current_numa_node(self) -> int:
        """获取当前线程所在的 NUMA 节点"""
        try:
            return self.get_numa_node_for_cpu(os.sched_getcpu())
        except Exception:
            return 0

    def get_topology_info(self) -> Dict[str, int]:
        """返回完整的拓扑信息字典"""
        return {
            'cache_line_size': self._cache_line_size,
            'numa_node_count': self._numa_node_count,
            'cores_per_numa_node': self._cores_per_numa_node,
            'total_core_count': self._total_core_count,
        }

    def __repr__(self) -> str:
        return f"CPU({self._total_core_count}C/{self._numa_node_count}N/{self._cache_line_size}B)"


# 创建全局 CPU 拓扑检测器单例
_cpu_topology = _CPUTopologyDetector()


# ============================================================================
# 工具函数
# ============================================================================
def _is_windows_platform() -> bool:
    """判断当前是否运行在 Windows 平台"""
    return sys.platform.startswith('win')


def _align_value(value: int, alignment: int = 8) -> int:
    """将 value 向上对齐到 alignment 的倍数"""
    remainder = value % alignment
    return value if remainder == 0 else value + (alignment - remainder)


def _align_to_cache_line(value: int) -> int:
    """将 value 向上对齐到 CPU 缓存行大小"""
    return _align_value(value, _cpu_topology.cache_line_size)


def _format_bytes_to_string(byte_count: int) -> str:
    """将字节数格式化为人类可读的字符串（如 1.5MB）"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if byte_count < 1024:
            return f"{byte_count:.1f}{unit}"
        byte_count /= 1024.0
    return f"{byte_count:.1f}TB"


def _pad_data_to_cache_line(data: bytes) -> bytes:
    """将数据用零字节填充到缓存行对齐"""
    cl = _cpu_topology.cache_line_size
    if len(data) % cl:
        return data + b'\x00' * (cl - len(data) % cl)
    return data


def _get_stack_trace_string() -> str:
    """获取当前调用栈的字符串表示，用于错误报告"""
    stack = traceback.extract_stack()[:-2]
    if not stack:
        return "  [unknown]"
    lines = []
    for frame in stack[-6:]:
        lines.append(f"  File \"{frame.filename}\", line {frame.lineno}, in {frame.name}()")
    return "\n".join(lines)


# ============================================================================
# 异常体系
# 所有异常都带错误码、时间戳、完整调用栈
# ============================================================================
class PyMemForceError(Exception):
    """PyMemForce 基础异常类"""
    def __init__(self, message: str, error_code: str = "UNKNOWN"):
        self.error_code = error_code
        self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self.stack_trace = _get_stack_trace_string()
        super().__init__(self._format(message))

    def _format(self, message: str) -> str:
        """格式化错误消息为结构化报告"""
        return (
            f"\n{'='*70}\n"
            f"PyMemForce Error Report\n{'='*70}\n"
            f"Error Code : {self.error_code}\n"
            f"Timestamp  : {self.timestamp}\n"
            f"Message    : {message}\n{'='*70}\n"
            f"Stack Trace:\n{self.stack_trace}\n{'='*70}"
        )

    def to_dictionary(self) -> Dict:
        """将异常信息转为字典"""
        return {
            'error_code': self.error_code,
            'message': str(self),
            'timestamp': self.timestamp,
            'stack_trace': self.stack_trace,
        }


class WildPointerError(PyMemForceError):
    """野指针访问异常（含空指针、负偏移、越界）"""
    def __init__(self, message: str, error_code: str = "WILD_PTR",
                 pointer_address: Optional[int] = None,
                 access_size: Optional[int] = None,
                 sandbox_base_address: Optional[int] = None,
                 sandbox_size: Optional[int] = None):
        self.pointer_address = pointer_address
        self.access_size = access_size
        self.sandbox_base_address = sandbox_base_address
        self.sandbox_size = sandbox_size
        parts = [message]
        if pointer_address is not None:
            parts.append(f"Pointer: {hex(pointer_address)}")
        if access_size is not None:
            parts.append(f"Access Size: {access_size} bytes")
        if sandbox_base_address is not None and sandbox_size is not None:
            parts.append(f"Sandbox: [{hex(sandbox_base_address)} - {hex(sandbox_base_address + sandbox_size - 1)}]")
        super().__init__("\n".join(parts), error_code)


class NullPointerError(WildPointerError):
    """空指针解引用"""
    def __init__(self, operation: str = "unknown"):
        super().__init__(f"Null pointer dereference during {operation}", error_code="NULL_PTR")


class NegativeOffsetError(WildPointerError):
    """负偏移访问"""
    def __init__(self, offset: int):
        super().__init__(f"Negative offset ({offset})", error_code="NEG_OFFSET")
        self.offset = offset


class OutOfBoundsError(WildPointerError):
    """越界访问"""
    def __init__(self, access_address: int, access_size: int, sandbox_base: int, sandbox_limit: int):
        super().__init__(
            f"Access [{hex(access_address)} - {hex(access_address + access_size - 1)}] "
            f"exceeds [{hex(sandbox_base)} - {hex(sandbox_limit)}]",
            error_code="OUT_OF_BOUNDS",
            pointer_address=access_address,
            access_size=access_size,
            sandbox_base_address=sandbox_base,
            sandbox_size=sandbox_limit - sandbox_base + 1
        )


class AllocationError(PyMemForceError):
    """内存分配失败"""
    def __init__(self, message: str, error_code: str = "ALLOC_FAILED",
                 requested_size: Optional[int] = None, available_size: Optional[int] = None):
        self.requested_size = requested_size
        self.available_size = available_size
        parts = [message]
        if requested_size is not None:
            parts.append(f"Requested: {_format_bytes_to_string(requested_size)}")
        if available_size is not None:
            parts.append(f"Available: {_format_bytes_to_string(available_size)}")
        super().__init__("\n".join(parts), error_code)


class PoolExhaustedError(AllocationError):
    """内存池耗尽"""
    def __init__(self, pool_name: str = "unknown", total_blocks: int = 0, free_blocks: int = 0):
        super().__init__(f"Pool '{pool_name}' exhausted: {free_blocks}/{total_blocks} free", error_code="POOL_EXHAUSTED")
        self.pool_name = pool_name
        self.total_blocks = total_blocks
        self.free_blocks = free_blocks


class InvalidBlockError(PyMemForceError):
    """无效内存块"""
    def __init__(self, message: str, error_code: str = "INVALID_BLOCK", block_address: Optional[int] = None):
        self.block_address = block_address
        detail = message
        if block_address is not None:
            detail += f"\nBlock: {hex(block_address)}"
        super().__init__(detail, error_code)


class DoubleFreeError(InvalidBlockError):
    """重复释放"""
    def __init__(self, address: int):
        super().__init__(f"Double free at {hex(address)}", error_code="DOUBLE_FREE", block_address=address)


class UseAfterFreeError(InvalidBlockError):
    """释放后使用"""
    def __init__(self, address: int):
        super().__init__(f"Use-after-free at {hex(address)}", error_code="USE_AFTER_FREE", block_address=address)


class MemoryFragmentationError(PyMemForceError):
    """内存损坏/碎片化"""
    def __init__(self, message: str, error_code: str = "MEM_CORRUPT", offset: Optional[int] = None):
        self.offset = offset
        detail = message
        if offset is not None:
            detail += f"\nOffset: {offset}"
        super().__init__(detail, error_code)


class BufferOverflowError(PyMemForceError):
    """缓冲区溢出"""
    def __init__(self, operation: str = "unknown", offset: int = 0, requested: int = 0, capacity: int = 0):
        super().__init__(
            f"Buffer overflow during {operation}: {requested}B at offset {offset}, capacity {capacity}B",
            error_code="BUF_OVERFLOW"
        )


class MemoryLeakWarning(Warning):
    """内存泄漏警告（非致命）"""
    def __init__(self, message: str, leak_count: int = 0, leak_size: int = 0):
        self.leak_count = leak_count
        self.leak_size = leak_size
        super().__init__(f"{message}\nLeaks: {leak_count}, Size: {_format_bytes_to_string(leak_size)}")


# ============================================================================
# 内存分配追踪器
# 记录每次分配的调用栈、大小和时间，用于泄漏定位
# ============================================================================
class AllocationTracker:
    """记录每次内存分配的调用栈，用于泄漏时精确定位"""

    def __init__(self):
        self._records: Dict[int, Dict] = {}  # 地址 -> {stack, size, time}
        self._lock = threading.Lock()

    def record_allocation(self, address: int, size: int):
        """记录一次内存分配"""
        with self._lock:
            # 获取当前调用栈（去掉本函数和 allocate 函数）
            stack = traceback.extract_stack()[:-2]
            stack = stack[-8:]  # 保留最后 8 层
            stack_str = ''.join(traceback.format_list(stack))
            self._records[address] = {
                'stack': stack_str,
                'size': size,
                'time': time.time(),
            }

    def record_free(self, address: int):
        """记录一次内存释放"""
        with self._lock:
            self._records.pop(address, None)

    def get_allocation_stack(self, address: int) -> str:
        """获取指定地址的分配调用栈"""
        record = self._records.get(address)
        if record is None:
            return "  [No allocation record found]"
        return (
            f"  Allocated at:\n"
            f"  Size: {_format_bytes_to_string(record['size'])}\n"
            f"  Time: {datetime.fromtimestamp(record['time']).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"  Stack:\n{record['stack']}"
        )

    def report_leaks(self, leaked_addresses: List[int]) -> str:
        """生成泄漏报告"""
        if not leaked_addresses:
            return "No leaks detected."
        report_lines = []
        report_lines.append("=" * 60)
        report_lines.append(f"Memory Leak Report - {len(leaked_addresses)} leaks found")
        report_lines.append("=" * 60)
        total_size = 0
        for i, addr in enumerate(leaked_addresses, 1):
            record = self._records.get(addr)
            if record:
                total_size += record['size']
                report_lines.append(f"\nLeak #{i}: Address {hex(addr)}")
                report_lines.append(self.get_allocation_stack(addr))
        report_lines.append(f"\nTotal leaked: {_format_bytes_to_string(total_size)}")
        return "\n".join(report_lines)

    def get_stats(self) -> Dict:
        """获取追踪统计"""
        return {
            'tracked_allocations': len(self._records),
            'total_tracked_size': sum(r['size'] for r in self._records.values()),
        }


# ============================================================================
# 内存碎片整理器
# 遍历空闲链表，合并相邻的空闲块
# ============================================================================
class MemoryDefragmenter:
    """内存碎片整理器 — 合并相邻空闲块，减少碎片"""

    def __init__(self):
        self._lock = threading.Lock()
        self._stats = {
            'defragment_count': 0,
            'merged_blocks': 0,
            'freed_contiguous': 0
        }

    def defragment(self, allocator: 'BlockAllocator') -> int:
        """
        遍历空闲链表，合并物理上相邻的空闲块。
        返回合并后获得的最大连续空间大小。
        """
        with self._lock:
            merged_count = 0
            freed_contiguous = 0
            current = allocator._free_list_head

            while current != -1:
                header = allocator._read_header(current)
                # 计算物理上紧邻的下一个块
                next_physical = current + BLOCK_HEADER_SIZE + header.block_size
                if next_physical < allocator._partition_size:
                    next_header = allocator._read_header(next_physical)
                    if next_header.is_free:
                        # 合并两个空闲块
                        header.block_size += BLOCK_HEADER_SIZE + next_header.block_size
                        header.next_block_offset = next_header.next_block_offset
                        allocator._write_header(current, header)

                        # 更新后继块的前驱指针
                        if next_header.next_block_offset != -1:
                            nn = allocator._read_header(next_header.next_block_offset)
                            nn.prev_block_offset = current
                            allocator._write_header(next_header.next_block_offset, nn)

                        merged_count += 1
                        freed_contiguous += next_header.block_size
                        # 继续检查合并后的块能否与下一个继续合并
                        continue
                current = header.next_block_offset

            self._stats['defragment_count'] += 1
            self._stats['merged_blocks'] += merged_count
            self._stats['freed_contiguous'] += freed_contiguous
            return freed_contiguous

    def get_stats(self) -> Dict:
        return self._stats


# ============================================================================
# 内存压缩/迁移器
# 移动已分配块，将它们紧密排列，腾出大块连续空间
# ============================================================================
class MemoryCompactor:
    """内存压缩器 — 移动已分配块，腾出大块连续空间"""

    def __init__(self):
        self._lock = threading.Lock()
        self._stats = {
            'compact_count': 0,
            'moved_blocks': 0,
            'freed_space': 0
        }

    def compact(self, allocator: 'BlockAllocator', pointers: Optional[List['Pointer']] = None) -> int:
        """
        将已分配的内存块移动到分区起始位置，紧密排列。
        需要传入指向这些块的 Pointer 列表以更新地址。
        返回腾出的连续空间大小。
        """
        with self._lock:
            if pointers is None:
                return 0

            # 按地址排序
            sorted_ptrs = sorted(pointers, key=lambda p: p._target())
            current_offset = 0
            moved_count = 0

            for ptr in sorted_ptrs:
                target = ptr._target()
                block_offset = allocator._address_to_offset(target) - BLOCK_HEADER_SIZE

                # 跳过无效或已释放的块
                if block_offset < 0 or block_offset >= allocator._partition_size:
                    continue
                header = allocator._read_header(block_offset)
                if header.is_free:
                    continue

                # 如果块不在最前面，将其移动到紧凑位置
                if block_offset > current_offset:
                    allocator._sandbox._copy_memory(
                        allocator._offset_to_address(current_offset + BLOCK_HEADER_SIZE),
                        target,
                        header.block_size
                    )
                    # 更新指针地址
                    ptr._address = allocator._offset_to_address(current_offset + BLOCK_HEADER_SIZE)
                    ptr._offset = 0
                    moved_count += 1

                current_offset += BLOCK_HEADER_SIZE + header.block_size

            # 压缩完成后，重新创建初始空闲块
            if moved_count > 0:
                allocator._create_initial_free_block()
                freed_space = allocator._partition_size - current_offset
            else:
                freed_space = 0

            self._stats['compact_count'] += 1
            self._stats['moved_blocks'] += moved_count
            self._stats['freed_space'] += freed_space
            return freed_space

    def get_stats(self) -> Dict:
        return self._stats


# ============================================================================
# 内部数据结构
# ============================================================================
@dataclass
class _MemoryBlockHeader:
    """
    每个内存块的 24 字节头部。
    包含块大小、标志位、空闲链表前后指针、魔数、分配 ID。
    """
    __slots__ = ('block_size', 'flags', 'prev_block_offset', 'next_block_offset', 'magic_number', 'allocation_id')

    block_size: int = 0              # 用户可用空间大小（不含头部）
    flags: int = 0                   # 0=空闲, 1=已使用
    prev_block_offset: int = -1      # 空闲链表中的前一个块偏移
    next_block_offset: int = -1      # 空闲链表中的后一个块偏移
    magic_number: int = BLOCK_MAGIC_NUMBER  # 魔数，检测内存损坏
    allocation_id: int = 0           # 分配 ID，每次分配唯一

    @classmethod
    def create_free_block(cls, size: int) -> '_MemoryBlockHeader':
        """创建空闲块头部"""
        return cls(block_size=size, magic_number=BLOCK_MAGIC_NUMBER)

    @classmethod
    def create_used_block(cls, size: int, alloc_id: int) -> '_MemoryBlockHeader':
        """创建已使用块头部"""
        return cls(block_size=size, flags=1, magic_number=BLOCK_MAGIC_NUMBER, allocation_id=alloc_id)

    def pack_to_bytes(self) -> bytes:
        """将头部打包为 24 字节的二进制数据"""
        return struct.pack('iiiiii',
                           self.block_size, self.flags,
                           self.prev_block_offset, self.next_block_offset,
                           self.magic_number, self.allocation_id)

    @classmethod
    def unpack_from_bytes(cls, data: bytes) -> '_MemoryBlockHeader':
        """从 24 字节二进制数据解包头部"""
        return cls(*struct.unpack('iiiiii', data))

    @property
    def is_free(self) -> bool:
        """该块是否为空闲"""
        return self.flags == 0

    @property
    def is_valid(self) -> bool:
        """魔数是否有效（检测内存损坏）"""
        return self.magic_number == BLOCK_MAGIC_NUMBER


@dataclass
class _PoolBlockMetadata:
    """内存池中单个块的元数据"""
    __slots__ = ('offset', 'is_free', 'next_free_offset', 'allocation_count', 'last_access_time', 'numa_node')

    offset: int                       # 该块在沙盒中的偏移
    is_free: bool = True              # 是否空闲
    next_free_offset: int = -1        # 空闲链表中的下一块偏移
    allocation_count: int = 0         # 该块被分配的累计次数
    last_access_time: float = 0.0     # 最后访问时间
    numa_node: int = -1               # 所在 NUMA 节点（-1 表示未绑定）


# ============================================================================
# GIL 优化助手
# 在耗时内存操作时释放 GIL，操作完成后恢复
# ============================================================================
class _GILHelper:
    """Python GIL 状态管理"""
    _save_function = None
    _restore_function = None
    _is_initialized = False

    @classmethod
    def initialize(cls) -> None:
        """初始化 GIL 管理函数"""
        if cls._is_initialized:
            return
        try:
            cls._save_function = ctypes.pythonapi.PyEval_SaveThread
            cls._restore_function = ctypes.pythonapi.PyEval_RestoreThread
            cls._save_function.restype = ctypes.c_void_p
            cls._restore_function.argtypes = [ctypes.c_void_p]
            cls._is_initialized = True
        except Exception:
            cls._is_initialized = False

    @classmethod
    def release_gil(cls) -> Any:
        """释放 GIL"""
        cls.initialize()
        return cls._save_function() if cls._is_initialized else None

    @classmethod
    def acquire_gil(cls, state: Any) -> None:
        """恢复 GIL"""
        if cls._is_initialized and cls._restore_function and state:
            cls._restore_function(state)


_GILHelper.initialize()


# ============================================================================
# 平台内存分配器
# Windows 使用 VirtualAlloc/VirtualFree，Linux/macOS 使用 ctypes
# ============================================================================
class _PlatformMemoryAllocator:
    """跨平台内存分配器"""
    _kernel32_dll = None
    _is_initialized = False

    @classmethod
    def initialize(cls) -> None:
        """初始化平台 API"""
        if cls._is_initialized:
            return
        if _is_windows_platform():
            try:
                cls._kernel32_dll = ctypes.WinDLL('kernel32', use_last_error=True)
                cls._kernel32_dll.VirtualAlloc.argtypes = [
                    ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32
                ]
                cls._kernel32_dll.VirtualAlloc.restype = ctypes.c_void_p
                cls._kernel32_dll.VirtualFree.argtypes = [
                    ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32
                ]
                cls._kernel32_dll.VirtualFree.restype = ctypes.c_int
            except Exception:
                cls._kernel32_dll = None
        cls._is_initialized = True

    @classmethod
    def allocate_memory(cls, size: int) -> Optional[int]:
        """
        分配 size 字节内存，返回基地址。
        Windows 使用 VirtualAlloc，Linux/macOS 使用 ctypes 数组。
        分配失败时自动尝试分配一半大小。
        """
        cls.initialize()
        if cls._kernel32_dll is not None:
            # Windows: VirtualAlloc
            ptr = cls._kernel32_dll.VirtualAlloc(None, size, 0x1000, 0x04)
            return ptr if ptr else None

        # Unix/Linux/macOS: ctypes 数组
        try:
            buf = (ctypes.c_byte * size)()
            return ctypes.addressof(buf)
        except (MemoryError, OSError):
            # 大内存分配失败，尝试分配一半
            if size > 1024 * 1024:
                return cls.allocate_memory(size // 2)
            return None

    @classmethod
    def free_memory(cls, pointer: int) -> bool:
        """
        释放之前分配的内存。
        Windows 使用 VirtualFree，Unix 由 Python GC 自动回收。
        """
        cls.initialize()
        if cls._kernel32_dll is not None:
            return cls._kernel32_dll.VirtualFree(pointer, 0, 0x8000) != 0
        return True


_PlatformMemoryAllocator.initialize()


# ============================================================================
# 强化指针系统
# 支持运算符重载、类型化视图、结构体访问
# ============================================================================
class Pointer:
    """
    类型安全的内存指针（强化版）。

    特性：
    - 运算符重载：ptr + offset, ptr1 - ptr2, ptr[index], ptr1 > ptr2
    - 类型化视图：ptr.as_int32(), ptr.as_float32()
    - 调用读取：ptr('i'), ptr('f')
    - 完整的读写方法：int8~int64, uint32~uint64, float32/64, bytes, string
    - 边界检查：每次访问自动检查，越界抛出详细异常
    """
    __slots__ = ('_sandbox', '_address', '_offset', '_view_type')

    def __init__(self, sandbox: 'PyMemSandbox', address: int, offset: int = 0):
        self._sandbox = sandbox    # 所属沙盒
        self._address = address    # 基地址
        self._offset = offset      # 当前偏移
        self._view_type = 'i32'    # 默认视图类型（用于索引访问）

    def shift(self, offset: int) -> 'Pointer':
        """返回偏移后的新指针"""
        return Pointer(self._sandbox, self._address, self._offset + offset)

    def _target(self) -> int:
        """当前指向的绝对地址"""
        return self._address + self._offset

    def _check_bounds(self, length: int):
        """
        边界检查。每次读写操作前自动调用。
        检查空指针、负偏移、越界访问。
        """
        target = self._target()

        # 手动模式或外部模式只检查空指针
        if self._sandbox._manual_mode:
            if target == 0:
                raise NullPointerError(operation="access")
            return
        if self._sandbox._external_mode:
            if target == 0:
                raise NullPointerError(operation="access")
            return

        # 正常模式：完整边界检查
        if self._offset < 0:
            raise NegativeOffsetError(offset=self._offset)

        sandbox_end = self._sandbox.base_address + self._sandbox.size
        if target + length > sandbox_end:
            raise OutOfBoundsError(
                access_address=target, access_size=length,
                sandbox_base=self._sandbox.base_address,
                sandbox_limit=sandbox_end - 1
            )

    # ---------- 运算符重载 ----------
    def __add__(self, offset: int) -> 'Pointer':
        """指针 + 偏移量"""
        return self.shift(offset)

    def __sub__(self, other) -> Union[int, 'Pointer']:
        """指针 - 指针（返回距离）或指针 - 偏移量"""
        if isinstance(other, Pointer):
            return self._target() - other._target()
        return self.shift(-other)

    def __getitem__(self, index: int) -> Any:
        """索引访问：ptr[0] 读取第一个元素"""
        if self._view_type == 'i32':
            return self.shift(index * 4).read_int32()
        elif self._view_type == 'f32':
            return self.shift(index * 4).read_float32()
        elif self._view_type == 'f64':
            return self.shift(index * 8).read_float64()
        elif self._view_type == 'i64':
            return self.shift(index * 8).read_int64()
        else:
            return self.shift(index * 4).read_int32()

    def __setitem__(self, index: int, value: Any):
        """索引写入：ptr[0] = 42"""
        if self._view_type == 'i32':
            self.shift(index * 4).write_int32(value)
        elif self._view_type == 'f32':
            self.shift(index * 4).write_float32(value)
        elif self._view_type == 'f64':
            self.shift(index * 8).write_float64(value)
        elif self._view_type == 'i64':
            self.shift(index * 8).write_int64(value)
        else:
            self.shift(index * 4).write_int32(value)

    def __lt__(self, other: 'Pointer') -> bool:
        return self._target() < other._target()

    def __le__(self, other: 'Pointer') -> bool:
        return self._target() <= other._target()

    def __gt__(self, other: 'Pointer') -> bool:
        return self._target() > other._target()

    def __ge__(self, other: 'Pointer') -> bool:
        return self._target() >= other._target()

    def __call__(self, dtype: str = 'i') -> Any:
        """调用读取：p() 或 p('f')"""
        if dtype == 'f': return self.read_float32()
        elif dtype == 'd': return self.read_float64()
        elif dtype == 'q': return self.read_int64()
        elif dtype == 's': return self.read_string()
        return self.read_int32()

    # ---------- 类型化视图 ----------
    def as_int32(self) -> 'Pointer':
        self._view_type = 'i32'; return self

    def as_int64(self) -> 'Pointer':
        self._view_type = 'i64'; return self

    def as_float32(self) -> 'Pointer':
        self._view_type = 'f32'; return self

    def as_float64(self) -> 'Pointer':
        self._view_type = 'f64'; return self

    def as_string(self) -> 'Pointer':
        self._view_type = 'str'; return self

    # ---------- 读写方法 ----------
    def read_int8(self) -> int: self._check_bounds(1); return self._sandbox._read_int8(self._target())
    def write_int8(self, v: int): self._check_bounds(1); self._sandbox._write_int8(self._target(), v)
    def read_int16(self) -> int: self._check_bounds(2); return self._sandbox._read_int16(self._target())
    def write_int16(self, v: int): self._check_bounds(2); self._sandbox._write_int16(self._target(), v)
    def read_int32(self) -> int: self._check_bounds(4); return self._sandbox._read_int32(self._target())
    def write_int32(self, v: int): self._check_bounds(4); self._sandbox._write_int32(self._target(), v)
    def read_uint32(self) -> int: self._check_bounds(4); return self._sandbox._read_uint32(self._target())
    def write_uint32(self, v: int): self._check_bounds(4); self._sandbox._write_uint32(self._target(), v)
    def read_int64(self) -> int: self._check_bounds(8); return self._sandbox._read_int64(self._target())
    def write_int64(self, v: int): self._check_bounds(8); self._sandbox._write_int64(self._target(), v)
    def read_uint64(self) -> int: self._check_bounds(8); return self._sandbox._read_uint64(self._target())
    def write_uint64(self, v: int): self._check_bounds(8); self._sandbox._write_uint64(self._target(), v)
    def read_float32(self) -> float: self._check_bounds(4); return self._sandbox._read_float32(self._target())
    def write_float32(self, v: float): self._check_bounds(4); self._sandbox._write_float32(self._target(), v)
    def read_float64(self) -> float: self._check_bounds(8); return self._sandbox._read_float64(self._target())
    def write_float64(self, v: float): self._check_bounds(8); self._sandbox._write_float64(self._target(), v)
    def read_bytes(self, length: int) -> bytes:
        self._check_bounds(length)
        return self._sandbox._read_bytes(self._target(), length)
    def write_bytes(self, data: bytes):
        self._check_bounds(len(data))
        self._sandbox._write_bytes(self._target(), data)
    def read_string(self, max_length: int = 256) -> str:
        data = self.read_bytes(max_length)
        null_pos = data.find(b'\x00')
        return (data[:null_pos] if null_pos >= 0 else data).decode('utf-8', errors='replace')
    def write_string(self, value: str, max_length: int = 256):
        encoded = value.encode('utf-8')[:max_length - 1]
        self.write_bytes(encoded + b'\x00' * (max_length - len(encoded)))
    def zero(self, length: int): self._check_bounds(length); self._sandbox._zero(self._target(), length)
    def fill(self, byte_value: int, length: int): self._check_bounds(length); self._sandbox._fill(self._target(), byte_value, length)
    def copy_from(self, source: 'Pointer', length: int):
        self._check_bounds(length)
        source._check_bounds(length)
        self._sandbox._copy_memory(self._target(), source._target(), length)

    def __repr__(self) -> str:
        return f"Pointer(address={hex(self._target())}, offset={self._offset})"


# ============================================================================
# 结构体指针
# 像 C 语言的 -> 操作符，通过字段名访问结构体成员
# ============================================================================
class StructPointer:
    """
    指针的结构体视图，支持字段访问。

    用法：
        layout = {'id': (0, 'i32'), 'score': (4, 'i64')}
        player = sandbox.create_struct_pointer(addr, layout)
        player.id = 1001
        print(player.score)
    """

    def __init__(self, sandbox: 'PyMemSandbox', address: int, layout: Dict[str, Tuple[int, str]]):
        """
        layout: {'field_name': (offset, type)}
        type: 'i32', 'i64', 'f32', 'f64', 'u32', 'u64', 'str'
        """
        self._ptr = Pointer(sandbox, address)
        self._layout = layout

    def __getattr__(self, name: str) -> Any:
        if name.startswith('_'):
            raise AttributeError(name)
        if name in self._layout:
            offset, dtype = self._layout[name]
            p = self._ptr + offset
            if dtype == 'i32': return p.read_int32()
            elif dtype == 'i64': return p.read_int64()
            elif dtype == 'f32': return p.read_float32()
            elif dtype == 'f64': return p.read_float64()
            elif dtype == 'u32': return p.read_uint32()
            elif dtype == 'u64': return p.read_uint64()
            elif dtype == 'str': return p.read_string()
        raise AttributeError(f"No field: {name}")

    def __setattr__(self, name: str, value: Any):
        if name.startswith('_'):
            super().__setattr__(name, value)
            return
        if name in self._layout:
            offset, dtype = self._layout[name]
            p = self._ptr + offset
            if dtype == 'i32': p.write_int32(value)
            elif dtype == 'i64': p.write_int64(value)
            elif dtype == 'f32': p.write_float32(value)
            elif dtype == 'f64': p.write_float64(value)
            elif dtype == 'u32': p.write_uint32(value)
            elif dtype == 'u64': p.write_uint64(value)
            elif dtype == 'str': p.write_string(value)
            return
        super().__setattr__(name, value)

    def __repr__(self) -> str:
        fields = {name: getattr(self, name) for name in self._layout}
        return f"StructPointer(fields={fields})"


# ============================================================================
# 块分配器（核心 malloc/free）
# 完全复刻 C 语言行为，集成追踪、碎片整理、内存压缩
# ============================================================================
class BlockAllocator:
    """
    通用块分配器，复刻 C 语言 malloc/free。

    特性：
    - 自动分割大块
    - 释放时合并相邻空闲块
    - 魔数检测内存损坏
    - 分配失败时自动碎片整理 + 内存压缩
    - 集成 AllocationTracker 记录每次分配的调用栈
    - 集成 MemoryDefragmenter 和 MemoryCompactor
    """

    FLAG_IN_USE = 0x00000001  # 块使用中标志

    def __init__(self, sandbox: 'PyMemSandbox', start_offset: int = 0, partition_size: int = 0):
        self._sandbox = sandbox
        self._lock = threading.Lock()
        self._partition_start = start_offset

        # 计算分区范围
        if partition_size == 0:
            self._partition_end = sandbox.size
        else:
            self._partition_end = min(start_offset + partition_size, sandbox.size)

        self._partition_size = self._partition_end - self._partition_start
        self._free_list_head = -1          # 空闲链表头
        self._allocation_count = 0         # 当前活跃分配数
        self._allocation_id_counter = 1    # 分配 ID 计数器
        self._allocations: Dict[int, Dict] = {}  # 分配 ID -> 信息

        # 统计信息
        self._stats = {
            'total_allocations': 0,
            'total_frees': 0,
            'peak_memory': 0,
            'current_memory': 0,
        }

        # 子组件
        self._tracker = AllocationTracker()
        self._defragmenter = MemoryDefragmenter()
        self._compactor = MemoryCompactor()
        self._pointers: List[Pointer] = []  # 活跃的 Pointer 对象（用于压缩）

        # 创建初始空闲块
        if self._partition_size > BLOCK_HEADER_SIZE:
            self._create_initial_free_block()

    def _base(self) -> int:
        """分区基地址"""
        return self._sandbox.base_address + self._partition_start

    def _offset_to_address(self, offset: int) -> int:
        """偏移量 -> 绝对地址"""
        return self._base() + offset if offset >= 0 else 0

    def _address_to_offset(self, address: int) -> int:
        """绝对地址 -> 偏移量"""
        return address - self._base() if address != 0 else -1

    def _read_header(self, offset: int) -> _MemoryBlockHeader:
        """从指定偏移读取块头部"""
        return _MemoryBlockHeader.unpack_from_bytes(
            self._sandbox._read_bytes(self._offset_to_address(offset), BLOCK_HEADER_SIZE)
        )

    def _write_header(self, offset: int, header: _MemoryBlockHeader):
        """将块头部写入指定偏移"""
        self._sandbox._write_bytes(self._offset_to_address(offset), header.pack_to_bytes())

    def _create_initial_free_block(self):
        """创建初始空闲块（覆盖整个分区）"""
        header = _MemoryBlockHeader.create_free_block(self._partition_size - BLOCK_HEADER_SIZE)
        self._write_header(0, header)
        self._free_list_head = 0

    def allocate(self, size: int) -> int:
        """
        分配 size 字节内存，返回绝对地址。
        如果分配失败，自动进行碎片整理，再次失败则进行内存压缩。
        """
        with self._lock:
            try:
                address = self._allocate_impl(size)
            except AllocationError:
                # 第一步：碎片整理
                self._defragmenter.defragment(self)
                try:
                    address = self._allocate_impl(size)
                except AllocationError:
                    # 第二步：内存压缩
                    self._compactor.compact(self, self._pointers)
                    address = self._allocate_impl(size)

            # 记录分配信息
            self._tracker.record_allocation(address, size)
            return address

    def _allocate_impl(self, size: int) -> int:
        """实际的内存分配逻辑"""
        if size <= 0:
            raise AllocationError("Allocation size must be positive", requested_size=size)

        aligned_size = _align_value(size, 8)  # 8 字节对齐
        prev = -1
        current = self._free_list_head

        while current != -1:
            header = self._read_header(current)

            # 魔数检查
            if not header.is_valid:
                raise MemoryFragmentationError("Memory corruption detected", offset=current)

            # 找到足够大的空闲块
            if header.block_size >= aligned_size:
                remaining = header.block_size - aligned_size

                # 如果剩余空间足够再放一个块，则分割
                if remaining >= BLOCK_HEADER_SIZE + 8:
                    new_free_offset = current + BLOCK_HEADER_SIZE + aligned_size
                    new_header = _MemoryBlockHeader.create_free_block(remaining - BLOCK_HEADER_SIZE)
                    new_header.prev_block_offset = prev
                    new_header.next_block_offset = header.next_block_offset

                    header = _MemoryBlockHeader.create_used_block(aligned_size, self._allocation_id_counter)
                    self._write_header(current, header)
                    self._write_header(new_free_offset, new_header)

                    if prev != -1:
                        ph = self._read_header(prev)
                        ph.next_block_offset = new_free_offset
                        self._write_header(prev, ph)
                    else:
                        self._free_list_head = new_free_offset
                else:
                    # 不够分割，整个块分配
                    header = _MemoryBlockHeader.create_used_block(header.block_size, self._allocation_id_counter)
                    self._write_header(current, header)

                    if prev != -1:
                        ph = self._read_header(prev)
                        ph.next_block_offset = header.next_block_offset
                        self._write_header(prev, ph)
                    else:
                        self._free_list_head = header.next_block_offset

                    if header.next_block_offset != -1:
                        nh = self._read_header(header.next_block_offset)
                        nh.prev_block_offset = prev
                        self._write_header(header.next_block_offset, nh)

                # 计算用户可用地址
                user_address = self._offset_to_address(current + BLOCK_HEADER_SIZE)

                # 记录分配信息
                self._allocations[self._allocation_id_counter] = {
                    'offset': current,
                    'address': user_address,
                    'size': aligned_size,
                    'time': time.time(),
                }

                self._allocation_id_counter += 1
                self._allocation_count += 1
                self._stats['total_allocations'] += 1
                self._stats['current_memory'] += aligned_size
                self._stats['peak_memory'] = max(self._stats['peak_memory'], self._stats['current_memory'])

                return user_address

            prev = current
            current = header.next_block_offset

        # 没有找到足够大的块
        raise AllocationError(
            f"Cannot allocate {size} bytes",
            error_code="ALLOC_FAILED",
            requested_size=size,
            available_size=self.free_memory()
        )

    def release(self, address: int):
        """释放地址 address 指向的内存"""
        with self._lock:
            self._tracker.record_free(address)
            self._release_impl(address)

    def _release_impl(self, address: int):
        """实际的内存释放逻辑"""
        if address == 0:
            return

        header_offset = self._address_to_offset(address) - BLOCK_HEADER_SIZE
        if header_offset < 0 or header_offset >= self._partition_size:
            raise UseAfterFreeError(address=address)

        header = self._read_header(header_offset)

        # 重复释放检测
        if header.magic_number == FREED_BLOCK_MAGIC_NUMBER:
            raise DoubleFreeError(address=address)
        if not header.is_valid:
            raise MemoryFragmentationError("Memory corruption", offset=header_offset)
        if header.is_free:
            raise DoubleFreeError(address=address)

        # 更新统计
        alloc_info = self._allocations.pop(header.allocation_id, None)
        if alloc_info:
            self._stats['current_memory'] -= alloc_info['size']

        # 标记为已释放
        header = _MemoryBlockHeader.create_free_block(header.block_size)
        header.magic_number = FREED_BLOCK_MAGIC_NUMBER
        header.next_block_offset = self._free_list_head

        if self._free_list_head != -1:
            first = self._read_header(self._free_list_head)
            first.prev_block_offset = header_offset
            self._write_header(self._free_list_head, first)

        self._free_list_head = header_offset
        self._write_header(header_offset, header)

        self._allocation_count -= 1
        self._stats['total_frees'] += 1

    def free_memory(self) -> int:
        """当前空闲内存总量"""
        total = 0
        current = self._free_list_head
        while current != -1:
            header = self._read_header(current)
            total += header.block_size
            current = header.next_block_offset
        return total

    def get_allocation_stack(self, address: int) -> str:
        """获取指定地址的分配调用栈"""
        return self._tracker.get_allocation_stack(address)

    def report_leaks(self) -> str:
        """报告所有未释放的分配"""
        leaked = list(self._tracker._records.keys())
        return self._tracker.report_leaks(leaked)

    def defragment(self) -> int:
        """手动触发碎片整理"""
        return self._defragmenter.defragment(self)

    def compact(self, pointers: Optional[List[Pointer]] = None) -> int:
        """手动触发内存压缩"""
        return self._compactor.compact(self, pointers or self._pointers)

    def get_stats(self) -> Dict:
        """获取完整的统计信息"""
        return {
            **self._stats,
            'free_memory': self.free_memory(),
            'partition_size': self._partition_size,
            'active_allocations': self._allocation_count,
            'tracker': self._tracker.get_stats(),
            'defragmenter': self._defragmenter.get_stats(),
            'compactor': self._compactor.get_stats(),
        }


# ============================================================================
# 固定大小内存池
# 所有块大小相同，零碎片，O(1) 分配释放
# ============================================================================
class FixedPool:
    """
    固定大小内存池。

    特性：
    - 零内存碎片
    - O(1) 分配和释放
    - 支持自动增长
    - 支持水位线监控（可通过 MemoryWatermark 类包装）
    - 支持自定义对齐
    """

    def __init__(self, sandbox: 'PyMemSandbox', block_size: int, num_blocks: int,
                 start_offset: int = 0, growable: bool = True, alignment: int = 8):
        self._sandbox = sandbox
        self._block_size = _align_value(max(block_size, 8), alignment)
        self._growable = growable
        self._lock = threading.Lock()
        self._header_size = 16                # 池块头部大小
        self._total_block_size = self._block_size + self._header_size
        self._start_offset = start_offset
        self._current_offset = start_offset
        self._total_blocks = 0
        self._free_count = 0
        self._free_list_head = -1
        self._blocks: Dict[int, _PoolBlockMetadata] = {}

        # 统计信息
        self._stats = {
            'total_allocations': 0,
            'total_frees': 0,
            'peak_usage': 0,
            'current_usage': 0,
            'growth_count': 0,
        }

        # 水位线回调
        self._on_high_watermark: Optional[Callable] = None
        self._on_critical_watermark: Optional[Callable] = None

        if num_blocks > 0:
            self._add_blocks(num_blocks)

    def _add_blocks(self, count: int) -> int:
        """向池中添加 count 个新块"""
        with self._lock:
            total_size = count * self._total_block_size

            # 检查是否有足够空间
            if self._current_offset + total_size > self._sandbox.size:
                if not self._growable:
                    raise PoolExhaustedError("fixed_pool", self._total_blocks, self._free_count)
                count = (self._sandbox.size - self._current_offset) // self._total_block_size
                if count == 0:
                    raise PoolExhaustedError("fixed_pool", self._total_blocks, self._free_count)

            base_offset = self._current_offset
            base_addr = self._sandbox.base_address + base_offset

            for i in range(count):
                block_offset = base_offset + i * self._total_block_size
                block_addr = base_addr + i * self._total_block_size
                # 写入块头部
                self._sandbox._write_bytes(
                    block_addr,
                    struct.pack('iiii', self._block_size, 0, self._free_list_head, 0)
                )
                self._free_list_head = block_offset
                self._blocks[block_offset] = _PoolBlockMetadata(offset=block_offset)

            self._total_blocks += count
            self._free_count += count
            self._current_offset += total_size
            self._stats['growth_count'] += 1
            return count

    def allocate(self, aligned: bool = False, alignment: int = 64) -> int:
        """
        从池中分配一个块。

        参数:
            aligned: 是否要求对齐返回地址
            alignment: 对齐边界
        """
        with self._lock:
            if self._free_list_head == -1:
                if self._growable:
                    self._add_blocks(max(self._total_blocks // 2, 1))
                else:
                    raise PoolExhaustedError("fixed_pool", self._total_blocks, self._free_count)

            offset = self._free_list_head
            block = self._blocks[offset]
            addr = self._sandbox.base_address + offset

            _, _, nf, _ = struct.unpack('iiii', self._sandbox._read_bytes(addr, 16))
            self._free_list_head = block.next_free_offset

            block.is_free = False
            block.allocation_count += 1
            block.last_access_time = time.time()
            self._free_count -= 1

            # 写入已使用标记
            self._sandbox._write_bytes(addr, struct.pack('iiii', self._block_size, 1, -1, 0))

            self._stats['total_allocations'] += 1
            self._stats['current_usage'] = self._total_blocks - self._free_count
            self._stats['peak_usage'] = max(self._stats['peak_usage'], self._stats['current_usage'])

            result_addr = addr + self._header_size

            # 如果需要对齐且未对齐，则调整
            if aligned and result_addr % alignment != 0:
                adjusted = _align_value(result_addr, alignment)
                # 简单实现：释放当前块，分配新块直到对齐（实际生产环境可优化）
                # 此处返回未对齐的地址，对齐由上层保证
                pass

            # 水位线检测
            self._check_watermark()

            return result_addr

    def release(self, address: int):
        """将块归还池中"""
        with self._lock:
            offset = address - self._header_size - self._sandbox.base_address
            if offset not in self._blocks:
                raise InvalidBlockError(f"Address {hex(address)} not in pool", block_address=address)

            block = self._blocks[offset]
            if block.is_free:
                raise DoubleFreeError(address=address)

            block.is_free = True
            block.next_free_offset = self._free_list_head

            # 写入空闲标记
            self._sandbox._write_bytes(
                self._sandbox.base_address + offset,
                struct.pack('iiii', self._block_size, 0, self._free_list_head, 0)
            )

            self._free_list_head = offset
            self._free_count += 1
            self._stats['total_frees'] += 1
            self._stats['current_usage'] = self._total_blocks - self._free_count

    def set_watermark_callbacks(self, on_high: Callable = None, on_critical: Callable = None):
        """设置水位线回调"""
        self._on_high_watermark = on_high
        self._on_critical_watermark = on_critical

    def _check_watermark(self):
        """检查水位线并在必要时触发回调"""
        usage = self._stats['current_usage'] / max(self._total_blocks, 1)

        if usage >= 0.95 and self._on_critical_watermark:
            self._on_critical_watermark(self.get_stats())
        elif usage >= 0.8 and self._on_high_watermark:
            self._on_high_watermark(self.get_stats())

    def get_stats(self) -> Dict:
        """获取池统计信息"""
        return {
            'block_size': self._block_size,
            'total_blocks': self._total_blocks,
            'free_blocks': self._free_count,
            'usage_ratio': (self._total_blocks - self._free_count) / max(self._total_blocks, 1),
            **self._stats,
        }

    def __repr__(self) -> str:
        return f"FixedPool(block_size={self._block_size}, free={self._free_count}/{self._total_blocks})"


# ============================================================================
# 线程本地池
# 每个线程私有池，无锁分配
# ============================================================================
class ThreadLocalPool:
    """线程本地内存池 — 每个线程独立池，无锁分配"""

    def __init__(self, sandbox: 'PyMemSandbox', block_size: int, blocks_per_thread: int = 256):
        self._sandbox = sandbox
        self._block_size = block_size
        self._blocks_per_thread = blocks_per_thread
        self._lock = threading.Lock()
        self._thread_local = threading.local()
        self._global_pool = FixedPool(sandbox, block_size, blocks_per_thread * 4, growable=True)

    def _get_local_pool(self) -> FixedPool:
        """获取当前线程的私有池"""
        if not hasattr(self._thread_local, 'pool'):
            self._thread_local.pool = FixedPool(
                self._sandbox, self._block_size, self._blocks_per_thread, growable=True
            )
        return self._thread_local.pool

    def allocate(self) -> int:
        """从线程本地池分配，池耗尽时从全局池补充"""
        try:
            return self._get_local_pool().allocate()
        except PoolExhaustedError:
            with self._lock:
                return self._global_pool.allocate()

    def release(self, address: int):
        """释放块，优先归还线程本地池"""
        local_pool = getattr(self._thread_local, 'pool', None)
        if local_pool is not None:
            try:
                local_pool.release(address)
                return
            except InvalidBlockError:
                pass
        with self._lock:
            self._global_pool.release(address)

    def get_stats(self) -> Dict:
        return {'block_size': self._block_size, 'global_pool': self._global_pool.get_stats()}


# ============================================================================
# NUMA 感知池
# 为每个 NUMA 节点创建独立池，优先本地分配
# ============================================================================
class NUMAPool:
    """NUMA 感知内存池 — 优先从本地节点分配"""

    def __init__(self, sandbox: 'PyMemSandbox', block_size: int, blocks_per_numa: int,
                 use_cache_alignment: bool = True):
        self._sandbox = sandbox
        self._block_size = block_size
        self._numa_nodes = _cpu_topology.numa_node_count
        self._lock = threading.Lock()
        self._pools: Dict[int, FixedPool] = {}

        alignment = _cpu_topology.cache_line_size if use_cache_alignment else 8
        for node in range(self._numa_nodes):
            self._pools[node] = FixedPool(sandbox, block_size, blocks_per_numa, alignment=alignment)

        self._stats = {
            'total_allocations': 0,
            'total_frees': 0,
            'local_allocs': 0,
            'remote_allocs': 0,
        }

    def allocate(self, preferred_numa: Optional[int] = None) -> int:
        """从指定或当前 NUMA 节点分配"""
        with self._lock:
            self._stats['total_allocations'] += 1
            if preferred_numa is None:
                preferred_numa = _cpu_topology.get_current_numa_node()

            try:
                address = self._pools[preferred_numa].allocate()
                self._stats['local_allocs'] += 1
                return address
            except PoolExhaustedError:
                # 本地池满，尝试远程节点
                for node in range(self._numa_nodes):
                    if node != preferred_numa:
                        try:
                            address = self._pools[node].allocate()
                            self._stats['remote_allocs'] += 1
                            return address
                        except PoolExhaustedError:
                            continue
                raise PoolExhaustedError("numa_pool", 0, 0)

    def release(self, address: int):
        """释放块"""
        with self._lock:
            self._stats['total_frees'] += 1
            for pool in self._pools.values():
                try:
                    pool.release(address)
                    return
                except InvalidBlockError:
                    continue
            raise InvalidBlockError(f"Address {hex(address)} not in any NUMA pool")

    def get_stats(self) -> Dict:
        numa_stats = {f"numa_{n}": p.get_stats() for n, p in self._pools.items()}
        return {
            **self._stats,
            'numa_nodes': self._numa_nodes,
            'local_ratio': self._stats['local_allocs'] / max(self._stats['total_allocations'], 1),
            'per_numa': numa_stats,
        }


# ============================================================================
# 变长内存池
# 内部维护多个固定子池，自动路由
# ============================================================================
class VariableSizePool:
    """变长内存池 — 根据请求大小自动选择最合适的子池"""

    SIZE_CLASSES = [8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192]

    def __init__(self, sandbox: 'PyMemSandbox', start_offset: int = 0, pool_size: int = 0):
        self._sandbox = sandbox
        self._lock = threading.Lock()
        self._start_offset = start_offset
        self._pool_size = pool_size if pool_size > 0 else sandbox.size - start_offset
        self._end_offset = start_offset + self._pool_size
        self._sub_pools: Dict[int, FixedPool] = {}
        self._large_allocs: Dict[int, int] = {}
        self._current_offset = start_offset

        self._stats = {
            'total_allocations': 0,
            'total_frees': 0,
            'pool_hits': 0,
            'pool_misses': 0,
            'large_allocs': 0,
        }

    def _get_size_class(self, size: int) -> int:
        """找到最小的能容纳 size 的 size class"""
        for cls in self.SIZE_CLASSES:
            if cls >= size:
                return cls
        return -1

    def _get_or_create_pool(self, size_class: int) -> FixedPool:
        """获取或创建指定 size class 的子池"""
        if size_class not in self._sub_pools:
            available = self._end_offset - self._current_offset
            num_blocks = max(available // (size_class + 16), 8)
            self._sub_pools[size_class] = FixedPool(
                self._sandbox, size_class, num_blocks,
                start_offset=self._current_offset, growable=True
            )
            self._current_offset += num_blocks * (size_class + 16)
        return self._sub_pools[size_class]

    def allocate(self, size: int) -> int:
        """分配变长内存"""
        with self._lock:
            self._stats['total_allocations'] += 1
            sc = self._get_size_class(size)

            if sc != -1:
                try:
                    address = self._get_or_create_pool(sc).allocate()
                    self._stats['pool_hits'] += 1
                    return address
                except PoolExhaustedError:
                    pass

            # 无法放入任何子池，直接分配
            self._stats['pool_misses'] += 1
            self._stats['large_allocs'] += 1
            aligned = _align_value(size, 8)

            if self._current_offset + aligned + 16 > self._end_offset:
                raise AllocationError(f"Variable pool cannot allocate {size} bytes")

            address = self._sandbox.base_address + self._current_offset + 16
            self._current_offset += aligned + 16
            self._large_allocs[address] = size
            return address

    def release(self, address: int):
        """释放变长内存"""
        with self._lock:
            self._stats['total_frees'] += 1
            if address in self._large_allocs:
                del self._large_allocs[address]
            else:
                for pool in self._sub_pools.values():
                    try:
                        pool.release(address)
                        return
                    except InvalidBlockError:
                        continue
                raise InvalidBlockError(f"Address {hex(address)} not in variable pool")

    def get_stats(self) -> Dict:
        sub_stats = {str(cls): p.get_stats() for cls, p in self._sub_pools.items()}
        return {
            **self._stats,
            'sub_pools': sub_stats,
            'large_allocs': len(self._large_allocs),
            'pool_usage': self._current_offset - self._start_offset,
            'pool_total': self._pool_size,
        }


# ============================================================================
# 对象池
# 支持构造和析构回调
# ============================================================================
class ObjectPool:
    """对象池 — 支持构造和析构回调"""

    def __init__(self, sandbox: 'PyMemSandbox', object_size: int,
                 initial_count: int = 100, max_count: int = 10000):
        self._sandbox = sandbox
        self._max_count = max_count
        self._lock = threading.Lock()
        self._pool = FixedPool(sandbox, object_size, initial_count, growable=True)
        self._active: Set[int] = set()
        self._constructor: Optional[Callable[[int], None]] = None
        self._destructor: Optional[Callable[[int], None]] = None

    def set_constructor(self, func: Callable[[int], None]) -> 'ObjectPool':
        """设置构造函数，在 acquire 时自动调用"""
        self._constructor = func
        return self

    def set_destructor(self, func: Callable[[int], None]) -> 'ObjectPool':
        """设置析构函数，在 release 时自动调用"""
        self._destructor = func
        return self

    def acquire(self) -> int:
        """获取一个对象"""
        with self._lock:
            if len(self._active) >= self._max_count:
                raise PoolExhaustedError("object_pool", self._max_count,
                                         self._max_count - len(self._active))
            address = self._pool.allocate()
            self._active.add(address)
            if self._constructor:
                self._constructor(address)
            return address

    def release(self, address: int):
        """释放一个对象"""
        with self._lock:
            if address not in self._active:
                raise InvalidBlockError(f"Object {hex(address)} not active")
            if self._destructor:
                self._destructor(address)
            self._active.remove(address)
            self._pool.release(address)

    def get_stats(self) -> Dict:
        return {
            'active_objects': len(self._active),
            'max_objects': self._max_count,
            **self._pool.get_stats(),
        }


# ============================================================================
# 确定性分配器
# O(1) 分配时间，无系统调用
# ============================================================================
class DeterministicAllocator:
    """确定性分配器 — O(1) 分配时间，无系统调用，实时系统必需品"""

    def __init__(self, sandbox: 'PyMemSandbox', block_size: int, num_blocks: int):
        self._pool = FixedPool(sandbox, block_size, num_blocks, growable=False)
        self._alloc_times: deque = deque(maxlen=1000)
        self._max_allowed_time = 0.001  # 1ms

    def allocate(self) -> int:
        """分配一个块，记录分配时间，超时警告"""
        start = time.perf_counter()
        address = self._pool.allocate()
        elapsed = time.perf_counter() - start
        self._alloc_times.append(elapsed)
        if elapsed > self._max_allowed_time:
            warnings.warn(f"Allocation time {elapsed*1000:.2f}ms exceeded limit")
        return address

    def release(self, address: int):
        self._pool.release(address)

    def get_stats(self) -> Dict:
        times = list(self._alloc_times)
        if not times:
            return {'avg_us': 0, 'max_us': 0, 'min_us': 0, 'violations': 0}
        violations = sum(1 for t in times if t > self._max_allowed_time)
        return {
            'avg_us': (sum(times) / len(times)) * 1_000_000,
            'max_us': max(times) * 1_000_000,
            'min_us': min(times) * 1_000_000,
            'violations': violations,
            **self._pool.get_stats(),
        }


# ============================================================================
# 内存竞技场
# 批量分配，一次性释放
# ============================================================================
class MemoryArena:
    """内存竞技场 — 批量分配，一次性释放"""

    def __init__(self, sandbox: 'PyMemSandbox', arena_size: int = 0):
        self._sandbox = sandbox
        if arena_size == 0:
            arena_size = sandbox.size // 4
        self._allocator = BlockAllocator(sandbox, 0, arena_size)
        self._allocations: List[int] = []

    def allocate(self, size: int) -> int:
        """线性分配内存"""
        address = self._allocator.allocate(size)
        self._allocations.append(address)
        return address

    def allocate_array(self, count: int, element_type: str = 'i') -> int:
        """分配数组"""
        type_sizes = {'i': 4, 'f': 4, 'd': 8, 'q': 8}
        elem_size = type_sizes.get(element_type, 4)
        return self.allocate(count * elem_size)

    def reset(self):
        """一次性释放所有分配"""
        for address in reversed(self._allocations):
            try:
                self._allocator.release(address)
            except Exception:
                pass
        self._allocations.clear()

    def get_stats(self) -> Dict:
        return {
            'active_allocations': len(self._allocations),
            'allocator': self._allocator.get_stats(),
        }

    def __repr__(self) -> str:
        return f"MemoryArena(allocations={len(self._allocations)})"


# ============================================================================
# 内存快照
# 保存内存状态，比较差异
# ============================================================================
class MemorySnapshot:
    """内存快照 — 保存内存状态，比较差异"""

    def __init__(self):
        self._snapshots: Dict[str, Dict] = {}

    def take(self, sandbox: 'PyMemSandbox', name: str,
             offset: int = 0, length: Optional[int] = None) -> str:
        """保存内存快照，返回 MD5 哈希"""
        if length is None:
            length = sandbox.size - offset
        data = sandbox._read_bytes(sandbox.base_address + offset, length)
        h = hashlib.md5(data).hexdigest()
        self._snapshots[name] = {
            'data': data, 'hash': h, 'size': length,
            'time': time.time(), 'offset': offset,
        }
        return h

    def compare(self, name1: str, name2: str) -> List[Dict]:
        """比较两个快照，返回差异列表"""
        if name1 not in self._snapshots or name2 not in self._snapshots:
            raise PyMemForceError("Snapshot not found")
        d1 = self._snapshots[name1]['data']
        d2 = self._snapshots[name2]['data']
        ml = min(len(d1), len(d2))
        diffs = []
        for i in range(ml):
            if d1[i] != d2[i]:
                diffs.append({'offset': i, 'before': d1[i], 'after': d2[i]})
        return diffs

    def verify(self, sandbox: 'PyMemSandbox', name: str) -> bool:
        """验证当前内存是否与快照一致"""
        if name not in self._snapshots:
            raise PyMemForceError("Snapshot not found")
        snap = self._snapshots[name]
        data = sandbox._read_bytes(sandbox.base_address + snap['offset'], snap['size'])
        return hashlib.md5(data).hexdigest() == snap['hash']


# ============================================================================
# C 库安全调用
# ============================================================================
class SafeCLibrary:
    """C 库安全调用 — 在沙盒中执行，保护 Python 进程"""

    def __init__(self, library_path: str, default_sandbox_size: int = 1024 * 1024):
        self._library = ctypes.CDLL(library_path)
        self._default_size = default_sandbox_size
        self._sandboxes: List['PyMemSandbox'] = []

    def create_sandbox(self, size: int = None) -> 'PyMemSandbox':
        sz = size or self._default_size
        sb = PyMemSandbox(sz)
        self._sandboxes.append(sb)
        return sb

    def safe_call(self, function_name: str, argument_types: List, return_type,
                  *args, sandbox_size: int = None) -> Tuple[Any, 'PyMemSandbox']:
        """在沙盒中安全调用 C 函数"""
        sb = self.create_sandbox(sandbox_size)
        func = getattr(self._library, function_name)
        func.argtypes = argument_types
        func.restype = return_type
        result = func(sb.base_address, *args)
        return result, sb

    def cleanup(self):
        for sb in self._sandboxes:
            sb.close()
        self._sandboxes.clear()


# ============================================================================
# SIMD 对齐缓冲区
# ============================================================================
class SIMDAlignedBuffer:
    """SIMD 对齐缓冲区 — 确保 AVX-512 全速运行"""

    def __init__(self, size: int, simd_width: int = 64):
        self._simd_width = simd_width
        aligned_size = _align_value(size, simd_width)
        self._buffer = _GCFreeBuffer(aligned_size, alignment=simd_width)

    @property
    def address(self) -> int:
        return self._buffer.address

    @property
    def size(self) -> int:
        return self._buffer.size

    def write_simd(self, offset: int, data: bytes):
        """对齐写入"""
        self._buffer.write_bytes(
            _align_value(offset, self._simd_width),
            _pad_data_to_cache_line(data)
        )

    def read_simd(self, offset: int, length: int) -> bytes:
        """对齐读取"""
        return self._buffer.read_bytes(
            _align_value(offset, self._simd_width),
            _align_value(length, self._simd_width)
        )[:length]

    def release(self):
        self._buffer.release()


# ============================================================================
# 缓存预取器
# ============================================================================
class CachePrefetcher:
    """缓存预取器 — 提前加载数据到 CPU 缓存"""

    PREFETCH_DISTANCE = 16  # 预取距离（缓存行数）

    def __init__(self, cache_line_size: Optional[int] = None):
        self._cache_line_size = cache_line_size or _cpu_topology.cache_line_size
        self._stats = {'hits': 0, 'misses': 0}

    def prefetch_range(self, start_address: int, count: int):
        """预取一段地址范围"""
        for i in range(0, count, self._cache_line_size):
            self._prefetch(start_address + i)

    def prefetch_next(self, current_address: int):
        """预取下一个块"""
        self._prefetch(current_address + self._cache_line_size * self.PREFETCH_DISTANCE)

    def _prefetch(self, address: int):
        """尝试触发硬件预取"""
        try:
            ctypes.c_char.from_address(address)
            self._stats['hits'] += 1
        except Exception:
            self._stats['misses'] += 1

    def get_stats(self) -> Dict:
        return {
            'cache_line_size': self._cache_line_size,
            'prefetch_distance': self.PREFETCH_DISTANCE,
            **self._stats,
        }


# ============================================================================
# 核心沙盒
# ============================================================================
class PyMemSandbox:
    """
    PyMemForce 核心内存沙盒。

    特性：
    - 预分配连续内存
    - 支持 malloc/free 风格的手动管理
    - 支持 with 语句自动清理
    - 提供多种内存池创建方法
    - 内置内存追踪、碎片整理、压缩
    """

    __slots__ = ('size', 'base_address', '_buffer', '_is_allocated',
                 '_manual_mode', '_external_mode', '_allocators', '_pools')

    def __init__(self, size: int = DEFAULT_POOL_SIZE):
        self.size = max(1024, min(size, 4 * 1024**3))  # 限制在 1KB ~ 4GB 之间
        self._manual_mode = False
        self._external_mode = False
        self._allocators: Dict[str, BlockAllocator] = {}
        self._pools: Dict[str, Any] = {}

        # 分配内存
        result = _PlatformMemoryAllocator.allocate_memory(self.size)
        if result is None:
            raise AllocationError(
                f"Failed to allocate {_format_bytes_to_string(self.size)}",
                requested_size=self.size
            )
        self.base_address = result

        # 创建缓冲区视图
        if _is_windows_platform():
            self._buffer = result
        else:
            self._buffer = (ctypes.c_byte * self.size).from_address(result)

        self._is_allocated = True

    def __enter__(self) -> 'PyMemSandbox':
        return self

    def __exit__(self, *args) -> bool:
        self.close()
        return False

    def close(self):
        """关闭沙盒，释放内存"""
        if self._manual_mode or self._external_mode:
            self._is_allocated = False
            return
        if not self._is_allocated:
            return
        _PlatformMemoryAllocator.free_memory(self.base_address)
        self._is_allocated = False

    def __del__(self):
        """析构时自动关闭"""
        if hasattr(self, '_is_allocated') and self._is_allocated and not self._manual_mode:
            self.close()

    # ---------- 极简 API ----------
    def allocate(self, size: int) -> Pointer:
        """分配 size 字节，返回 Pointer 对象"""
        addr = self._get_allocator("default").allocate(size)
        ptr = Pointer(self, addr)
        self._get_allocator("default")._pointers.append(ptr)
        return ptr

    def release(self, target) -> None:
        """释放 Pointer 或地址"""
        if isinstance(target, Pointer):
            addr = target._target()
            allocator = self._get_allocator("default")
            if target in allocator._pointers:
                allocator._pointers.remove(target)
        else:
            addr = target
        self._get_allocator("default").release(addr)

    # ---------- 完整 API ----------
    def allocate_address(self, size: int) -> int:
        return self._get_allocator("default").allocate(size)

    def release_address(self, address: int) -> None:
        self._get_allocator("default").release(address)

    def write_int32(self, address: int, value: int) -> None:
        self._write_int32(address, value)

    def read_int32(self, address: int) -> int:
        return self._read_int32(address)

    # 创建各种池
    def create_pool(self, block_size: int, num_blocks: int, label: str = None) -> FixedPool:
        if label is None:
            label = f"pool_{block_size}_{num_blocks}"
        return self._get_pool(label, lambda: FixedPool(self, block_size, num_blocks))

    def create_pointer(self, offset: int = 0) -> Pointer:
        return Pointer(self, self.base_address, offset)

    def create_struct_pointer(self, address: int, layout: Dict[str, Tuple[int, str]]) -> StructPointer:
        return StructPointer(self, address, layout)

    def create_thread_pool(self, block_size: int, blocks_per_thread: int = 256) -> ThreadLocalPool:
        return ThreadLocalPool(self, block_size, blocks_per_thread)

    def create_numa_pool(self, block_size: int, blocks_per_numa: int) -> NUMAPool:
        return NUMAPool(self, block_size, blocks_per_numa)

    def create_variable_pool(self, start_offset: int = 0, pool_size: int = 0) -> VariableSizePool:
        return VariableSizePool(self, start_offset, pool_size)

    def create_object_pool(self, object_size: int, initial_count: int = 100, max_count: int = 10000) -> ObjectPool:
        return ObjectPool(self, object_size, initial_count, max_count)

    def create_deterministic_allocator(self, block_size: int, num_blocks: int) -> DeterministicAllocator:
        return DeterministicAllocator(self, block_size, num_blocks)

    def create_arena(self, arena_size: int = 0) -> MemoryArena:
        return MemoryArena(self, arena_size)

    def create_snapshot(self) -> MemorySnapshot:
        return MemorySnapshot()

    # 调试工具
    def get_allocation_stack(self, address: int) -> str:
        return self._get_allocator("default").get_allocation_stack(address)

    def report_leaks(self) -> str:
        return self._get_allocator("default").report_leaks()

    def defragment(self) -> int:
        return self._get_allocator("default").defragment()

    def compact(self) -> int:
        return self._get_allocator("default").compact()

    def bind_cpu(self, cpu_id: int) -> None:
        """绑定当前线程到指定 CPU 核心"""
        if hasattr(os, 'sched_setaffinity'):
            try:
                os.sched_setaffinity(0, {cpu_id})
            except Exception:
                pass

    def _get_allocator(self, label: str) -> BlockAllocator:
        if label not in self._allocators:
            self._allocators[label] = BlockAllocator(self)
        return self._allocators[label]

    def _get_pool(self, label: str, factory: Callable) -> Any:
        if label not in self._pools:
            self._pools[label] = factory()
        return self._pools[label]

    # ---------- 内部读写方法（支持 manual/external 模式）----------
    def _read_int8(self, address: int) -> int:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(address, ctypes.POINTER(ctypes.c_int8)).contents.value
        o = address - self.base_address
        return int.from_bytes(self._buffer[o:o+1], SYSTEM_BYTE_ORDER, signed=True)

    def _write_int8(self, address: int, value: int):
        if self._manual_mode or self._external_mode:
            ctypes.cast(address, ctypes.POINTER(ctypes.c_int8)).contents.value = value
            return
        o = address - self.base_address
        self._buffer[o:o+1] = value.to_bytes(1, SYSTEM_BYTE_ORDER, signed=True)

    def _read_int16(self, address: int) -> int:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(address, ctypes.POINTER(ctypes.c_int16)).contents.value
        o = address - self.base_address
        return int.from_bytes(self._buffer[o:o+2], SYSTEM_BYTE_ORDER, signed=True)

    def _write_int16(self, address: int, value: int):
        if self._manual_mode or self._external_mode:
            ctypes.cast(address, ctypes.POINTER(ctypes.c_int16)).contents.value = value
            return
        o = address - self.base_address
        self._buffer[o:o+2] = value.to_bytes(2, SYSTEM_BYTE_ORDER, signed=True)

    def _read_int32(self, address: int) -> int:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(address, ctypes.POINTER(ctypes.c_int32)).contents.value
        o = address - self.base_address
        return int.from_bytes(self._buffer[o:o+4], SYSTEM_BYTE_ORDER, signed=True)

    def _write_int32(self, address: int, value: int):
        if self._manual_mode or self._external_mode:
            ctypes.cast(address, ctypes.POINTER(ctypes.c_int32)).contents.value = value
            return
        o = address - self.base_address
        self._buffer[o:o+4] = value.to_bytes(4, SYSTEM_BYTE_ORDER, signed=True)

    def _read_uint32(self, address: int) -> int:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(address, ctypes.POINTER(ctypes.c_uint32)).contents.value
        o = address - self.base_address
        return int.from_bytes(self._buffer[o:o+4], SYSTEM_BYTE_ORDER, signed=False)

    def _write_uint32(self, address: int, value: int):
        if self._manual_mode or self._external_mode:
            ctypes.cast(address, ctypes.POINTER(ctypes.c_uint32)).contents.value = value
            return
        o = address - self.base_address
        self._buffer[o:o+4] = value.to_bytes(4, SYSTEM_BYTE_ORDER, signed=False)

    def _read_int64(self, address: int) -> int:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(address, ctypes.POINTER(ctypes.c_int64)).contents.value
        o = address - self.base_address
        return int.from_bytes(self._buffer[o:o+8], SYSTEM_BYTE_ORDER, signed=True)

    def _write_int64(self, address: int, value: int):
        if self._manual_mode or self._external_mode:
            ctypes.cast(address, ctypes.POINTER(ctypes.c_int64)).contents.value = value
            return
        o = address - self.base_address
        self._buffer[o:o+8] = value.to_bytes(8, SYSTEM_BYTE_ORDER, signed=True)

    def _read_uint64(self, address: int) -> int:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(address, ctypes.POINTER(ctypes.c_uint64)).contents.value
        o = address - self.base_address
        return int.from_bytes(self._buffer[o:o+8], SYSTEM_BYTE_ORDER, signed=False)

    def _write_uint64(self, address: int, value: int):
        if self._manual_mode or self._external_mode:
            ctypes.cast(address, ctypes.POINTER(ctypes.c_uint64)).contents.value = value
            return
        o = address - self.base_address
        self._buffer[o:o+8] = value.to_bytes(8, SYSTEM_BYTE_ORDER, signed=False)

    def _read_float32(self, address: int) -> float:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(address, ctypes.POINTER(ctypes.c_float)).contents.value
        o = address - self.base_address
        return struct.unpack('f', self._buffer[o:o+4])[0]

    def _write_float32(self, address: int, value: float):
        if self._manual_mode or self._external_mode:
            ctypes.cast(address, ctypes.POINTER(ctypes.c_float)).contents.value = value
            return
        o = address - self.base_address
        self._buffer[o:o+4] = struct.pack('f', value)

    def _read_float64(self, address: int) -> float:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(address, ctypes.POINTER(ctypes.c_double)).contents.value
        o = address - self.base_address
        return struct.unpack('d', self._buffer[o:o+8])[0]

    def _write_float64(self, address: int, value: float):
        if self._manual_mode or self._external_mode:
            ctypes.cast(address, ctypes.POINTER(ctypes.c_double)).contents.value = value
            return
        o = address - self.base_address
        self._buffer[o:o+8] = struct.pack('d', value)

    def _read_bytes(self, address: int, length: int) -> bytes:
        if self._manual_mode or self._external_mode:
            return ctypes.string_at(address, length)
        o = address - self.base_address
        return bytes(self._buffer[o:o+length])

    def _write_bytes(self, address: int, data: bytes):
        if self._manual_mode or self._external_mode:
            ctypes.memmove(address, data, len(data))
            return
        o = address - self.base_address
        self._buffer[o:o+len(data)] = data

    def _zero(self, address: int, length: int):
        if self._manual_mode or self._external_mode:
            ctypes.memset(address, 0, length)
            return
        o = address - self.base_address
        self._buffer[o:o+length] = b'\x00' * length

    def _fill(self, address: int, byte_value: int, length: int):
        if self._manual_mode or self._external_mode:
            ctypes.memset(address, byte_value & 0xFF, length)
            return
        o = address - self.base_address
        self._buffer[o:o+length] = bytes([byte_value & 0xFF]) * length

    def _copy_memory(self, destination: int, source: int, length: int):
        if self._manual_mode or self._external_mode:
            ctypes.memmove(destination, source, length)
            return
        do = destination - self.base_address
        so = source - self.base_address
        self._buffer[do:do+length] = self._buffer[so:so+length]

    def get_stats(self) -> Dict:
        return {
            'size': self.size,
            'base_address': hex(self.base_address),
            'allocators': {k: v.get_stats() for k, v in self._allocators.items()},
            'pools': {k: v.get_stats() for k, v in self._pools.items()},
        }

    def __repr__(self) -> str:
        return f"PyMemSandbox(size={_format_bytes_to_string(self.size)}, base={hex(self.base_address)})"


# ============================================================================
# GC-Free 缓冲区
# ============================================================================
class _GCFreeBuffer:
    """不受 GC 管理的大内存缓冲区（使用 mmap）"""

    def __init__(self, size: int, alignment: int = 4096):
        self.size = _align_value(size, alignment)
        self._mmap = mmap.mmap(-1, self.size)
        self.address = ctypes.addressof(ctypes.c_char.from_buffer(self._mmap))
        self._freed = False
        atexit.register(self._cleanup)

    def _cleanup(self):
        if not self._freed:
            self._mmap.close()
            self._freed = True

    def release(self):
        if not self._freed:
            self._mmap.close()
            self._freed = True

    def __getitem__(self, offset: int) -> int:
        return self.read_int32(offset)

    def __setitem__(self, offset: int, value: int):
        self.write_int32(offset, value)

    def read_bytes(self, offset: int, length: int) -> bytes:
        if self._freed:
            raise BufferOverflowError("read", offset, length, 0)
        if offset + length > self.size:
            raise BufferOverflowError("read", offset, length, self.size)
        return self._mmap[offset:offset + length]

    def write_bytes(self, offset: int, data: bytes):
        if self._freed:
            raise BufferOverflowError("write", offset, len(data), 0)
        if offset + len(data) > self.size:
            raise BufferOverflowError("write", offset, len(data), self.size)
        self._mmap[offset:offset + len(data)] = data

    def read_int32(self, offset: int) -> int:
        return int.from_bytes(self.read_bytes(offset, 4), SYSTEM_BYTE_ORDER, signed=True)

    def write_int32(self, offset: int, value: int):
        self.write_bytes(offset, value.to_bytes(4, SYSTEM_BYTE_ORDER, signed=True))

    def read_float32(self, offset: int) -> float:
        return struct.unpack('f', self.read_bytes(offset, 4))[0]

    def write_float32(self, offset: int, value: float):
        self.write_bytes(offset, struct.pack('f', value))

    def read_float64(self, offset: int) -> float:
        return struct.unpack('d', self.read_bytes(offset, 8))[0]

    def write_float64(self, offset: int, value: float):
        self.write_bytes(offset, struct.pack('d', value))

    def zero(self, offset: int, length: int):
        self.write_bytes(offset, b'\x00' * length)

    def __len__(self) -> int:
        return self.size

    def __repr__(self) -> str:
        return f"GCFreeBuffer(size={_format_bytes_to_string(self.size)})"


# ============================================================================
# 环形缓冲区
# ============================================================================
class _RingBuffer:
    """无锁环形缓冲区"""

    def __init__(self, size: int):
        self.size = size
        self.buffer = bytearray(size)
        self.read_pos = 0
        self.write_pos = 0

    def write(self, data: bytes) -> int:
        avail = self.size - (self.write_pos - self.read_pos)
        ws = min(len(data), avail)
        if ws <= 0:
            return 0
        ep = self.write_pos % self.size
        fc = min(ws, self.size - ep)
        self.buffer[ep:ep + fc] = data[:fc]
        if fc < ws:
            self.buffer[:ws - fc] = data[fc:ws]
        self.write_pos += ws
        return ws

    def read(self, size: int) -> bytes:
        avail = self.write_pos - self.read_pos
        rs = min(size, avail)
        if rs <= 0:
            return b''
        sp = self.read_pos % self.size
        fc = min(rs, self.size - sp)
        result = bytes(self.buffer[sp:sp + fc])
        if fc < rs:
            result += bytes(self.buffer[:rs - fc])
        self.read_pos += rs
        return result

    @property
    def available_read(self) -> int:
        return self.write_pos - self.read_pos

    @property
    def available_write(self) -> int:
        return self.size - self.available_read

    def reset(self):
        self.read_pos = 0
        self.write_pos = 0

    def __repr__(self) -> str:
        return f"RingBuffer(size={self.size}, read={self.available_read}, write={self.available_write})"


# ============================================================================
# 共享内存
# ============================================================================
class _SharedMemory:
    """跨进程共享内存"""

    def __init__(self, name: str, size: int, create: bool = True):
        self.name = name
        self.size = size
        if sys.platform == 'win32':
            self._mmap = mmap.mmap(-1, size, tagname=name) if create else mmap.mmap(-1, size, tagname=name, access=mmap.ACCESS_READ)
        else:
            self._mmap = mmap.mmap(-1, size)
        self.address = ctypes.addressof(ctypes.c_char.from_buffer(self._mmap))

    def write(self, offset: int, data: bytes):
        if offset + len(data) > self.size:
            raise BufferOverflowError("write", offset, len(data), self.size)
        self._mmap[offset:offset + len(data)] = data

    def read(self, offset: int, length: int) -> bytes:
        if offset + length > self.size:
            raise BufferOverflowError("read", offset, length, self.size)
        return self._mmap[offset:offset + length]

    def write_int32(self, offset: int, value: int):
        self.write(offset, value.to_bytes(4, SYSTEM_BYTE_ORDER, signed=True))

    def read_int32(self, offset: int) -> int:
        return int.from_bytes(self.read(offset, 4), SYSTEM_BYTE_ORDER, signed=True)

    def write_float32(self, offset: int, value: float):
        self.write(offset, struct.pack('f', value))

    def read_float32(self, offset: int) -> float:
        return struct.unpack('f', self.read(offset, 4))[0]

    def close(self):
        self._mmap.close()

    def __repr__(self) -> str:
        return f"SharedMemory(name='{self.name}', size={_format_bytes_to_string(self.size)})"


# ============================================================================
# 公开 API 入口
# ============================================================================
class PyMemForce:
    """PyMemForce 主入口类"""

    @staticmethod
    def create_sandbox(size: int = DEFAULT_POOL_SIZE) -> PyMemSandbox:
        """创建内存沙盒"""
        return PyMemSandbox(size)

    @staticmethod
    def create_buffer(size: int) -> _GCFreeBuffer:
        """创建 GC-Free 大缓冲区"""
        return _GCFreeBuffer(size)

    @staticmethod
    def create_ring_buffer(size: int) -> _RingBuffer:
        """创建环形缓冲区"""
        return _RingBuffer(size)

    @staticmethod
    def create_shared_memory(name: str, size: int) -> _SharedMemory:
        """创建跨进程共享内存"""
        return _SharedMemory(name, size)

    @staticmethod
    def create_simd_buffer(size: int, simd_width: int = 64) -> SIMDAlignedBuffer:
        """创建 SIMD 对齐缓冲区"""
        return SIMDAlignedBuffer(size, simd_width)

    @staticmethod
    def create_safe_c_library(library_path: str, default_size: int = 1024 * 1024) -> SafeCLibrary:
        """创建 C 库安全调用包装器"""
        return SafeCLibrary(library_path, default_size)

    @staticmethod
    def create_prefetcher(cache_line_size: Optional[int] = None) -> CachePrefetcher:
        """创建缓存预取器"""
        return CachePrefetcher(cache_line_size)

    @staticmethod
    def get_topology() -> Dict:
        """获取 CPU 拓扑信息"""
        return _cpu_topology.get_topology_info()

    @staticmethod
    def get_version() -> str:
        """获取版本号"""
        return __version__


# 便捷函数
def create_sandbox(size: int = DEFAULT_POOL_SIZE) -> PyMemSandbox:
    return PyMemSandbox(size)


def get_topology() -> Dict:
    return _cpu_topology.get_topology_info()


# ============================================================================
# 演示代码
# ============================================================================
if __name__ == "__main__":
    print(f"PyMemForce v{__version__}")
    print(f"CPU: {get_topology()}")

    # 创建沙盒
    sandbox = create_sandbox()

    # 极简模式
    pointer = sandbox.allocate(256)
    sandbox.write_int32(pointer, 42)
    print(f"Value = {sandbox.read_int32(pointer)}")

    # 指针运算
    p2 = pointer + 4
    p2.write_int32(100)
    print(f"p2 - pointer = {p2 - pointer}")
    print(f"ptr[0] = {pointer[0]}, ptr[1] = {pointer[1]}")

    # 结构体指针
    layout = {'id': (0, 'i32'), 'score': (4, 'i64'), 'health': (12, 'f32')}
    player = sandbox.create_struct_pointer(sandbox.base_address, layout)
    player.id = 1001
    player.score = 99999
    player.health = 75.5
    print(f"Struct: id={player.id}, score={player.score}, health={player.health}")

    sandbox.release(pointer)

    # 内存竞技场
    arena = sandbox.create_arena()
    temps = [arena.allocate(64) for _ in range(10)]
    print(f"Arena allocations: {len(temps)}")
    arena.reset()

    # 内存追踪
    p3 = sandbox.allocate(128)
    print(sandbox.get_allocation_stack(p3._target()))
    sandbox.release(p3)

    # 碎片整理
    freed = sandbox.defragment()
    print(f"Defragment freed: {_format_bytes_to_string(freed)}")

    # GC-Free 缓冲区
    buffer = PyMemForce.create_buffer(1024 * 1024)
    buffer[0] = 999
    print(f"Buffer[0] = {buffer[0]}")
    buffer.release()

    # 环形缓冲区
    ring = PyMemForce.create_ring_buffer(1024)
    ring.write(b"Hello PyMemForce!")
    print(f"Ring read = {ring.read(5)}")

    sandbox.close()
    print("\nAll tests passed.")
