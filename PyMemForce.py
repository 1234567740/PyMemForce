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

__version__ = "2.0.0"
__author__ = "PyMemForce Team"
__license__ = "Apache-2.0"

# ================= 常量 =================
DEFAULT_POOL_SIZE = 64 * 1024 * 1024
HEADER_SIZE = 24
BYTE_ORDER = 'little' if sys.byteorder == 'little' else 'big'
MAGIC = 0xDEADBEEF
FREED_MAGIC = 0xDEADDEAD

# ================= CPU拓扑检测 =================
class _CPUTopology:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._cache_line = 64
                    obj._numa_nodes = 1
                    obj._cores_per_numa = os.cpu_count() or 1
                    obj._total_cores = os.cpu_count() or 1
                    obj._detect()
                    cls._instance = obj
        return cls._instance

    def _detect(self):
        if sys.platform.startswith('linux'):
            try:
                with open('/sys/devices/system/cpu/cpu0/cache/index0/coherency_line_size') as f:
                    self._cache_line = int(f.read().strip())
            except Exception:
                pass
            try:
                path = '/sys/devices/system/node/online'
                if os.path.exists(path):
                    with open(path) as f:
                        content = f.read().strip()
                        self._numa_nodes = int(content.split('-')[-1]) + 1 if '-' in content else len(content.split(','))
            except Exception:
                pass
        elif sys.platform == 'darwin':
            try:
                import subprocess
                r = subprocess.run(['sysctl', '-n', 'hw.cachelinesize'], capture_output=True, text=True)
                self._cache_line = int(r.stdout.strip())
            except Exception:
                pass
        if self._numa_nodes > 1:
            self._cores_per_numa = self._total_cores // self._numa_nodes

    @property
    def cache_line(self) -> int:
        return self._cache_line

    @property
    def numa_count(self) -> int:
        return self._numa_nodes

    @property
    def cores_per_numa(self) -> int:
        return self._cores_per_numa

    @property
    def total_cores(self) -> int:
        return self._total_cores

    def numa_for_cpu(self, cpu: int) -> int:
        if self._numa_nodes <= 1:
            return 0
        return min(cpu // self._cores_per_numa, self._numa_nodes - 1)

    def current_numa(self) -> int:
        try:
            return self.numa_for_cpu(os.sched_getcpu())
        except Exception:
            return 0

    def info(self) -> Dict:
        return {
            'cache_line': self._cache_line,
            'numa_nodes': self._numa_nodes,
            'cores_per_numa': self._cores_per_numa,
            'total_cores': self._total_cores,
        }

_cpu_topo = _CPUTopology()

# ================= 工具函数 =================
def _is_windows() -> bool:
    return sys.platform.startswith('win')

def _align(value: int, alignment: int = 8) -> int:
    remainder = value % alignment
    return value + (alignment - remainder) if remainder else value

def _align_cache_line(value: int) -> int:
    return _align(value, _cpu_topo.cache_line)

def _format_bytes(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"

def _pad_cache_line(data: bytes) -> bytes:
    cl = _cpu_topo.cache_line
    if len(data) % cl:
        return data + b'\x00' * (cl - len(data) % cl)
    return data

def _get_stack_trace() -> str:
    stack = traceback.extract_stack()[:-2]
    if not stack:
        return "  [unknown]"
    lines = []
    for frame in stack[-6:]:
        lines.append(f"  File \"{frame.filename}\", line {frame.lineno}, in {frame.name}()")
    return "\n".join(lines)

# ================= 异常体系 =================
class PyMemForceError(Exception):
    def __init__(self, message: str, error_code: str = "UNKNOWN"):
        self.error_code = error_code
        self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self.stack_trace = _get_stack_trace()
        super().__init__(self._format(message))

    def _format(self, message: str) -> str:
        return (
            f"\n{'='*70}\n"
            f"PyMemForce Error Report\n"
            f"{'='*70}\n"
            f"Error Code : {self.error_code}\n"
            f"Timestamp  : {self.timestamp}\n"
            f"Message    : {message}\n"
            f"{'='*70}\n"
            f"Stack Trace:\n{self.stack_trace}\n"
            f"{'='*70}"
        )

class WildPointerError(PyMemForceError):
    def __init__(self, message: str, error_code: str = "WILD_PTR",
                 ptr_address: Optional[int] = None,
                 access_size: Optional[int] = None,
                 sandbox_base: Optional[int] = None,
                 sandbox_size: Optional[int] = None):
        self.ptr_address = ptr_address
        self.access_size = access_size
        self.sandbox_base = sandbox_base
        self.sandbox_size = sandbox_size
        parts = [message]
        if ptr_address is not None:
            parts.append(f"Pointer: {hex(ptr_address)}")
        if access_size is not None:
            parts.append(f"Access Size: {access_size} bytes")
        if sandbox_base is not None and sandbox_size is not None:
            parts.append(f"Sandbox: [{hex(sandbox_base)} - {hex(sandbox_base + sandbox_size - 1)}]")
        super().__init__("\n".join(parts), error_code)

class NullPointerError(WildPointerError):
    def __init__(self, operation: str = "unknown"):
        super().__init__(f"Null pointer dereference during {operation}", error_code="NULL_PTR")

class NegativeOffsetError(WildPointerError):
    def __init__(self, offset: int):
        super().__init__(f"Negative offset ({offset}) results in invalid address", error_code="NEG_OFFSET")
        self.offset = offset

class OutOfBoundsError(WildPointerError):
    def __init__(self, addr: int, size: int, base: int, limit: int):
        super().__init__(
            f"Access [{hex(addr)} - {hex(addr + size - 1)}] exceeds [{hex(base)} - {hex(limit)}]",
            error_code="OUT_OF_BOUNDS",
            ptr_address=addr, access_size=size,
            sandbox_base=base, sandbox_size=limit - base + 1
        )

class AllocationError(PyMemForceError):
    def __init__(self, message: str, error_code: str = "ALLOC_FAILED",
                 requested_size: Optional[int] = None,
                 available_size: Optional[int] = None):
        self.requested_size = requested_size
        self.available_size = available_size
        parts = [message]
        if requested_size is not None:
            parts.append(f"Requested: {_format_bytes(requested_size)}")
        if available_size is not None:
            parts.append(f"Available: {_format_bytes(available_size)}")
        super().__init__("\n".join(parts), error_code)

class PoolExhaustedError(AllocationError):
    def __init__(self, pool_name: str = "unknown", total_blocks: int = 0, free_blocks: int = 0):
        super().__init__(f"Pool '{pool_name}' exhausted: {free_blocks}/{total_blocks} free", error_code="POOL_EXHAUSTED")
        self.pool_name = pool_name
        self.total_blocks = total_blocks
        self.free_blocks = free_blocks

class InvalidBlockError(PyMemForceError):
    def __init__(self, message: str, error_code: str = "INVALID_BLOCK", block_address: Optional[int] = None):
        self.block_address = block_address
        detail = message
        if block_address is not None:
            detail += f"\nBlock: {hex(block_address)}"
        super().__init__(detail, error_code)

class DoubleFreeError(InvalidBlockError):
    def __init__(self, address: int):
        super().__init__(f"Double free at {hex(address)}", error_code="DOUBLE_FREE", block_address=address)

class UseAfterFreeError(InvalidBlockError):
    def __init__(self, address: int):
        super().__init__(f"Use-after-free at {hex(address)}", error_code="USE_AFTER_FREE", block_address=address)

class MemoryFragmentationError(PyMemForceError):
    def __init__(self, message: str, error_code: str = "MEM_CORRUPT", offset: Optional[int] = None):
        self.offset = offset
        detail = message
        if offset is not None:
            detail += f"\nOffset: {offset}"
        super().__init__(detail, error_code)

class BufferOverflowError(PyMemForceError):
    def __init__(self, operation: str = "unknown", offset: int = 0, requested: int = 0, capacity: int = 0):
        super().__init__(
            f"Buffer overflow: {operation} {requested}B at offset {offset}, capacity {capacity}B",
            error_code="BUF_OVERFLOW"
        )

class MemoryLeakWarning(Warning):
    def __init__(self, message: str, leak_count: int = 0, leak_size: int = 0):
        self.leak_count = leak_count
        self.leak_size = leak_size
        super().__init__(f"{message}\nLeaks: {leak_count}, Size: {_format_bytes(leak_size)}")

# ================= 内部数据结构 =================
@dataclass(slots=True)
class _BlockHeader:
    size: int = 0
    flags: int = 0
    prev: int = -1
    next: int = -1
    magic: int = MAGIC
    alloc_id: int = 0

    @classmethod
    def create_free(cls, size: int) -> '_BlockHeader':
        return cls(size=size, magic=MAGIC)

    @classmethod
    def create_used(cls, size: int, alloc_id: int) -> '_BlockHeader':
        return cls(size=size, flags=1, magic=MAGIC, alloc_id=alloc_id)

    def pack(self) -> bytes:
        return struct.pack('iiiiii', self.size, self.flags, self.prev, self.next, self.magic, self.alloc_id)

    @classmethod
    def unpack(cls, data: bytes) -> '_BlockHeader':
        return cls(*struct.unpack('iiiiii', data))

    @property
    def is_free(self) -> bool:
        return self.flags == 0

    @property
    def is_valid(self) -> bool:
        return self.magic == MAGIC

@dataclass(slots=True)
class _PoolBlock:
    offset: int
    is_free: bool = True
    next_free: int = -1
    alloc_count: int = 0
    last_access: float = 0.0
    numa_node: int = -1

# ================= GIL优化 =================
class _GILHelper:
    _save = None
    _restore = None
    _ready = False

    @classmethod
    def init(cls):
        if cls._ready:
            return
        try:
            cls._save = ctypes.pythonapi.PyEval_SaveThread
            cls._restore = ctypes.pythonapi.PyEval_RestoreThread
            cls._save.restype = ctypes.c_void_p
            cls._restore.argtypes = [ctypes.c_void_p]
            cls._ready = True
        except Exception:
            cls._ready = False

    @classmethod
    def release(cls):
        cls.init()
        return cls._save() if cls._ready else None

    @classmethod
    def acquire(cls, state):
        if cls._ready and cls._restore and state:
            cls._restore(state)

_GILHelper.init()

# ================= 平台内存分配 =================
class _PlatformAllocator:
    _kernel32 = None
    _ready = False

    @classmethod
    def init(cls):
        if cls._ready:
            return
        if _is_windows():
            try:
                cls._kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
                cls._kernel32.VirtualAlloc.argtypes = [
                    ctypes.c_void_p, ctypes.c_size_t,
                    ctypes.c_uint32, ctypes.c_uint32
                ]
                cls._kernel32.VirtualAlloc.restype = ctypes.c_void_p
                cls._kernel32.VirtualFree.argtypes = [
                    ctypes.c_void_p, ctypes.c_size_t,
                    ctypes.c_uint32
                ]
                cls._kernel32.VirtualFree.restype = ctypes.c_int
            except Exception:
                cls._kernel32 = None
        cls._ready = True

    @classmethod
    def alloc(cls, size: int) -> Optional[int]:
        cls.init()
        if cls._kernel32:
            ptr = cls._kernel32.VirtualAlloc(None, size, 0x1000, 0x04)
            return ptr if ptr else None
        try:
            buf = (ctypes.c_byte * size)()
            return ctypes.addressof(buf)
        except (MemoryError, OSError):
            if size > 1024 * 1024:
                return cls.alloc(size // 2)
            return None

    @classmethod
    def free(cls, ptr: int) -> bool:
        cls.init()
        if cls._kernel32:
            return cls._kernel32.VirtualFree(ptr, 0, 0x8000) != 0
        return True

_PlatformAllocator.init()

# ================= 指针系统 =================
class Pointer:
    __slots__ = ('_sandbox', '_address', '_offset')

    def __init__(self, sandbox: 'PyMemSandbox', address: int, offset: int = 0):
        self._sandbox = sandbox
        self._address = address
        self._offset = offset

    def shift(self, offset: int) -> 'Pointer':
        return Pointer(self._sandbox, self._address, self._offset + offset)

    def _target(self) -> int:
        return self._address + self._offset

    def _check_bounds(self, length: int):
        target = self._target()
        if self._sandbox._manual_mode:
            if target == 0:
                raise NullPointerError(operation="access")
            return
        if self._sandbox._external_mode:
            if target == 0:
                raise NullPointerError(operation="access")
            return
        if self._offset < 0:
            raise NegativeOffsetError(offset=self._offset)
        sandbox_end = self._sandbox.base_address + self._sandbox.size
        if target + length > sandbox_end:
            raise OutOfBoundsError(
                addr=target, size=length,
                base=self._sandbox.base_address,
                limit=sandbox_end - 1
            )

    def read_i8(self) -> int: self._check_bounds(1); return self._sandbox._read_i8(self._target())
    def write_i8(self, v: int): self._check_bounds(1); self._sandbox._write_i8(self._target(), v)
    def read_i16(self) -> int: self._check_bounds(2); return self._sandbox._read_i16(self._target())
    def write_i16(self, v: int): self._check_bounds(2); self._sandbox._write_i16(self._target(), v)
    def read_i32(self) -> int: self._check_bounds(4); return self._sandbox._read_i32(self._target())
    def write_i32(self, v: int): self._check_bounds(4); self._sandbox._write_i32(self._target(), v)
    def read_u32(self) -> int: self._check_bounds(4); return self._sandbox._read_u32(self._target())
    def write_u32(self, v: int): self._check_bounds(4); self._sandbox._write_u32(self._target(), v)
    def read_i64(self) -> int: self._check_bounds(8); return self._sandbox._read_i64(self._target())
    def write_i64(self, v: int): self._check_bounds(8); self._sandbox._write_i64(self._target(), v)
    def read_u64(self) -> int: self._check_bounds(8); return self._sandbox._read_u64(self._target())
    def write_u64(self, v: int): self._check_bounds(8); self._sandbox._write_u64(self._target(), v)
    def read_f32(self) -> float: self._check_bounds(4); return self._sandbox._read_f32(self._target())
    def write_f32(self, v: float): self._check_bounds(4); self._sandbox._write_f32(self._target(), v)
    def read_f64(self) -> float: self._check_bounds(8); return self._sandbox._read_f64(self._target())
    def write_f64(self, v: float): self._check_bounds(8); self._sandbox._write_f64(self._target(), v)
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
        self._sandbox._copy(self._target(), source._target(), length)
    def __repr__(self) -> str:
        return f"Pointer(address={hex(self._target())}, offset={self._offset})"

# ================= 块分配器 =================
class BlockAllocator:
    FLAG_IN_USE = 0x00000001

    def __init__(self, sandbox: 'PyMemSandbox', start_offset: int = 0, partition_size: int = 0):
        self._sandbox = sandbox
        self._lock = threading.Lock()
        self._partition_start = start_offset
        self._partition_end = min(start_offset + partition_size, sandbox.size) if partition_size else sandbox.size
        self._partition_size = self._partition_end - self._partition_start
        self._free_list_head = -1
        self._alloc_count = 0
        self._alloc_id_counter = 1
        self._allocations: Dict[int, Dict] = {}
        self._stats = {'total_allocations': 0, 'total_frees': 0, 'peak_memory': 0, 'current_memory': 0}
        if self._partition_size > HEADER_SIZE:
            self._create_initial_free_block()

    def _base(self) -> int:
        return self._sandbox.base_address + self._partition_start

    def _offset_to_addr(self, offset: int) -> int:
        return self._base() + offset if offset >= 0 else 0

    def _addr_to_offset(self, addr: int) -> int:
        return addr - self._base() if addr != 0 else -1

    def _read_header(self, offset: int) -> _BlockHeader:
        addr = self._offset_to_addr(offset)
        data = self._sandbox._read_bytes(addr, HEADER_SIZE)
        return _BlockHeader.unpack(data)

    def _write_header(self, offset: int, header: _BlockHeader):
        addr = self._offset_to_addr(offset)
        self._sandbox._write_bytes(addr, header.pack())

    def _create_initial_free_block(self):
        available = self._partition_size - HEADER_SIZE
        header = _BlockHeader.create_free(available)
        self._write_header(0, header)
        self._free_list_head = 0

    def alloc(self, size: int) -> int:
        with self._lock:
            return self._alloc_impl(size)

    def _alloc_impl(self, size: int) -> int:
        if size <= 0:
            raise AllocationError("Allocation size must be positive", requested_size=size)
        aligned = _align(size, 8)
        prev = -1
        current = self._free_list_head
        while current != -1:
            header = self._read_header(current)
            if not header.is_valid:
                raise MemoryFragmentationError("Memory corruption detected", offset=current)
            if header.size >= aligned:
                remaining = header.size - aligned
                if remaining >= HEADER_SIZE + 8:
                    new_free_offset = current + HEADER_SIZE + aligned
                    new_header = _BlockHeader.create_free(remaining - HEADER_SIZE)
                    new_header.prev = prev
                    new_header.next = header.next
                    header = _BlockHeader.create_used(aligned, self._alloc_id_counter)
                    self._write_header(current, header)
                    self._write_header(new_free_offset, new_header)
                    if prev != -1:
                        ph = self._read_header(prev)
                        ph.next = new_free_offset
                        self._write_header(prev, ph)
                    else:
                        self._free_list_head = new_free_offset
                else:
                    header = _BlockHeader.create_used(header.size, self._alloc_id_counter)
                    self._write_header(current, header)
                    if prev != -1:
                        ph = self._read_header(prev)
                        ph.next = header.next
                        self._write_header(prev, ph)
                    else:
                        self._free_list_head = header.next
                    if header.next != -1:
                        nh = self._read_header(header.next)
                        nh.prev = prev
                        self._write_header(header.next, nh)
                user_addr = self._offset_to_addr(current + HEADER_SIZE)
                self._allocations[self._alloc_id_counter] = {
                    'offset': current, 'addr': user_addr,
                    'size': aligned, 'time': time.time()
                }
                self._alloc_id_counter += 1
                self._alloc_count += 1
                self._stats['total_allocations'] += 1
                self._stats['current_memory'] += aligned
                self._stats['peak_memory'] = max(self._stats['peak_memory'], self._stats['current_memory'])
                return user_addr
            prev = current
            current = header.next
        raise AllocationError(
            f"Cannot allocate {size} bytes",
            error_code="ALLOC_FAILED",
            requested_size=size,
            available_size=self.free_memory()
        )

    def free(self, addr: int):
        with self._lock:
            self._free_impl(addr)

    def _free_impl(self, addr: int):
        if addr == 0:
            return
        header_offset = self._addr_to_offset(addr) - HEADER_SIZE
        if header_offset < 0 or header_offset >= self._partition_size:
            raise UseAfterFreeError(address=addr)
        header = self._read_header(header_offset)
        if header.magic == FREED_MAGIC:
            raise DoubleFreeError(address=addr)
        if not header.is_valid:
            raise MemoryFragmentationError("Memory corruption", offset=header_offset)
        if header.is_free:
            raise DoubleFreeError(address=addr)
        alloc_info = self._allocations.pop(header.alloc_id, None)
        if alloc_info:
            self._stats['current_memory'] -= alloc_info['size']
        header = _BlockHeader.create_free(header.size)
        header.next = self._free_list_head
        if self._free_list_head != -1:
            first = self._read_header(self._free_list_head)
            first.prev = header_offset
            self._write_header(self._free_list_head, first)
        self._free_list_head = header_offset
        self._write_header(header_offset, header)
        self._alloc_count -= 1
        self._stats['total_frees'] += 1

    def free_memory(self) -> int:
        total = 0
        current = self._free_list_head
        while current != -1:
            header = self._read_header(current)
            total += header.size
            current = header.next
        return total

    def stats(self) -> Dict:
        return {
            **self._stats,
            'free_memory': self.free_memory(),
            'partition_size': self._partition_size,
            'active_allocations': self._alloc_count,
        }

# ================= 固定大小内存池 =================
class FixedPool:
    def __init__(self, sandbox: 'PyMemSandbox', block_size: int, num_blocks: int,
                 start_offset: int = 0, growable: bool = True, alignment: int = 8):
        self._sandbox = sandbox
        self._block_size = _align(max(block_size, 8), alignment)
        self._growable = growable
        self._lock = threading.Lock()
        self._header_size = 16
        self._total_block_size = self._block_size + self._header_size
        self._start_offset = start_offset
        self._current_offset = start_offset
        self._total_blocks = 0
        self._free_count = 0
        self._free_list_head = -1
        self._blocks: Dict[int, _PoolBlock] = {}
        self._stats = {
            'total_allocations': 0, 'total_frees': 0,
            'peak_usage': 0, 'current_usage': 0, 'growth_count': 0
        }
        if num_blocks > 0:
            self._add_blocks(num_blocks)

    def _add_blocks(self, count: int) -> int:
        with self._lock:
            total_size = count * self._total_block_size
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
                header = struct.pack('iiii', self._block_size, 0, self._free_list_head, 0)
                self._sandbox._write_bytes(block_addr, header)
                self._free_list_head = block_offset
                self._blocks[block_offset] = _PoolBlock(offset=block_offset)
            self._total_blocks += count
            self._free_count += count
            self._current_offset += total_size
            self._stats['growth_count'] += 1
            return count

    def alloc(self) -> int:
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
            self._free_list_head = block.next_free
            block.is_free = False
            block.alloc_count += 1
            block.last_access = time.time()
            self._free_count -= 1
            header = struct.pack('iiii', self._block_size, 1, -1, 0)
            self._sandbox._write_bytes(addr, header)
            self._stats['total_allocations'] += 1
            self._stats['current_usage'] = self._total_blocks - self._free_count
            self._stats['peak_usage'] = max(self._stats['peak_usage'], self._stats['current_usage'])
            return addr + self._header_size

    def free(self, addr: int):
        with self._lock:
            offset = addr - self._header_size - self._sandbox.base_address
            if offset not in self._blocks:
                raise InvalidBlockError(f"Address {hex(addr)} not in pool", block_address=addr)
            block = self._blocks[offset]
            if block.is_free:
                raise DoubleFreeError(address=addr)
            block.is_free = True
            block.next_free = self._free_list_head
            header = struct.pack('iiii', self._block_size, 0, self._free_list_head, 0)
            self._sandbox._write_bytes(self._sandbox.base_address + offset, header)
            self._free_list_head = offset
            self._free_count += 1
            self._stats['total_frees'] += 1
            self._stats['current_usage'] = self._total_blocks - self._free_count

    def stats(self) -> Dict:
        return {
            'block_size': self._block_size,
            'total_blocks': self._total_blocks,
            'free_blocks': self._free_count,
            'usage_ratio': (self._total_blocks - self._free_count) / max(self._total_blocks, 1),
            **self._stats,
        }

    def __repr__(self) -> str:
        return f"FixedPool(block_size={self._block_size}, free={self._free_count}/{self._total_blocks})"

# ================= 核心沙盒 =================
class PyMemSandbox:
    __slots__ = ('size', 'base_address', '_buffer', '_is_allocated',
                 '_manual_mode', '_external_mode', '_allocators', '_pools')

    def __init__(self, size: int = DEFAULT_POOL_SIZE):
        self.size = max(1024, min(size, 4 * 1024**3))
        self._manual_mode = False
        self._external_mode = False
        self._allocators: Dict[str, BlockAllocator] = {}
        self._pools: Dict[str, Any] = {}
        result = _PlatformAllocator.alloc(self.size)
        if result is None:
            raise AllocationError(f"Failed to allocate {_format_bytes(self.size)}", requested_size=self.size)
        self.base_address = result
        self._buffer = result if _is_windows() else (ctypes.c_byte * self.size).from_address(result)
        self._is_allocated = True

    def __enter__(self) -> 'PyMemSandbox':
        return self

    def __exit__(self, *args) -> bool:
        self.close()
        return False

    def close(self):
        if self._manual_mode or self._external_mode:
            self._is_allocated = False
            return
        if not self._is_allocated:
            return
        _PlatformAllocator.free(self.base_address)
        self._is_allocated = False

    def __del__(self):
        if self._is_allocated and not self._manual_mode:
            self.close()

    def alloc(self, size: int) -> int:
        return self._get_allocator("default").alloc(size)

    def delete(self, addr: int):
        self._get_allocator("default").free(addr)

    def write(self, addr: int, value: int):
        self._write_i32(addr, value)

    def read(self, addr: int) -> int:
        return self._read_i32(addr)

    def pool(self, block_size: int, num_blocks: int) -> FixedPool:
        return self._get_pool("default", lambda: FixedPool(self, block_size, num_blocks))

    def ptr(self, offset: int = 0) -> Pointer:
        return Pointer(self, self.base_address, offset)

    def _get_allocator(self, label: str) -> BlockAllocator:
        if label not in self._allocators:
            self._allocators[label] = BlockAllocator(self)
        return self._allocators[label]

    def _get_pool(self, label: str, factory: Callable) -> Any:
        if label not in self._pools:
            self._pools[label] = factory()
        return self._pools[label]

    def _read_i8(self, addr: int) -> int:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(addr, ctypes.POINTER(ctypes.c_int8)).contents.value
        o = addr - self.base_address
        return int.from_bytes(self._buffer[o:o+1], BYTE_ORDER, signed=True)

    def _write_i8(self, addr: int, v: int):
        if self._manual_mode or self._external_mode:
            ctypes.cast(addr, ctypes.POINTER(ctypes.c_int8)).contents.value = v
            return
        o = addr - self.base_address
        self._buffer[o:o+1] = v.to_bytes(1, BYTE_ORDER, signed=True)

    def _read_i16(self, addr: int) -> int:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(addr, ctypes.POINTER(ctypes.c_int16)).contents.value
        o = addr - self.base_address
        return int.from_bytes(self._buffer[o:o+2], BYTE_ORDER, signed=True)

    def _write_i16(self, addr: int, v: int):
        if self._manual_mode or self._external_mode:
            ctypes.cast(addr, ctypes.POINTER(ctypes.c_int16)).contents.value = v
            return
        o = addr - self.base_address
        self._buffer[o:o+2] = v.to_bytes(2, BYTE_ORDER, signed=True)

    def _read_i32(self, addr: int) -> int:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(addr, ctypes.POINTER(ctypes.c_int32)).contents.value
        o = addr - self.base_address
        return int.from_bytes(self._buffer[o:o+4], BYTE_ORDER, signed=True)

    def _write_i32(self, addr: int, v: int):
        if self._manual_mode or self._external_mode:
            ctypes.cast(addr, ctypes.POINTER(ctypes.c_int32)).contents.value = v
            return
        o = addr - self.base_address
        self._buffer[o:o+4] = v.to_bytes(4, BYTE_ORDER, signed=True)

    def _read_u32(self, addr: int) -> int:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(addr, ctypes.POINTER(ctypes.c_uint32)).contents.value
        o = addr - self.base_address
        return int.from_bytes(self._buffer[o:o+4], BYTE_ORDER, signed=False)

    def _write_u32(self, addr: int, v: int):
        if self._manual_mode or self._external_mode:
            ctypes.cast(addr, ctypes.POINTER(ctypes.c_uint32)).contents.value = v
            return
        o = addr - self.base_address
        self._buffer[o:o+4] = v.to_bytes(4, BYTE_ORDER, signed=False)

    def _read_i64(self, addr: int) -> int:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(addr, ctypes.POINTER(ctypes.c_int64)).contents.value
        o = addr - self.base_address
        return int.from_bytes(self._buffer[o:o+8], BYTE_ORDER, signed=True)

    def _write_i64(self, addr: int, v: int):
        if self._manual_mode or self._external_mode:
            ctypes.cast(addr, ctypes.POINTER(ctypes.c_int64)).contents.value = v
            return
        o = addr - self.base_address
        self._buffer[o:o+8] = v.to_bytes(8, BYTE_ORDER, signed=True)

    def _read_u64(self, addr: int) -> int:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(addr, ctypes.POINTER(ctypes.c_uint64)).contents.value
        o = addr - self.base_address
        return int.from_bytes(self._buffer[o:o+8], BYTE_ORDER, signed=False)

    def _write_u64(self, addr: int, v: int):
        if self._manual_mode or self._external_mode:
            ctypes.cast(addr, ctypes.POINTER(ctypes.c_uint64)).contents.value = v
            return
        o = addr - self.base_address
        self._buffer[o:o+8] = v.to_bytes(8, BYTE_ORDER, signed=False)

    def _read_f32(self, addr: int) -> float:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(addr, ctypes.POINTER(ctypes.c_float)).contents.value
        o = addr - self.base_address
        return struct.unpack('f', self._buffer[o:o+4])[0]

    def _write_f32(self, addr: int, v: float):
        if self._manual_mode or self._external_mode:
            ctypes.cast(addr, ctypes.POINTER(ctypes.c_float)).contents.value = v
            return
        o = addr - self.base_address
        self._buffer[o:o+4] = struct.pack('f', v)

    def _read_f64(self, addr: int) -> float:
        if self._manual_mode or self._external_mode:
            return ctypes.cast(addr, ctypes.POINTER(ctypes.c_double)).contents.value
        o = addr - self.base_address
        return struct.unpack('d', self._buffer[o:o+8])[0]

    def _write_f64(self, addr: int, v: float):
        if self._manual_mode or self._external_mode:
            ctypes.cast(addr, ctypes.POINTER(ctypes.c_double)).contents.value = v
            return
        o = addr - self.base_address
        self._buffer[o:o+8] = struct.pack('d', v)

    def _read_bytes(self, addr: int, length: int) -> bytes:
        if self._manual_mode or self._external_mode:
            return ctypes.string_at(addr, length)
        o = addr - self.base_address
        return bytes(self._buffer[o:o+length])

    def _write_bytes(self, addr: int, data: bytes):
        if self._manual_mode or self._external_mode:
            ctypes.memmove(addr, data, len(data))
            return
        o = addr - self.base_address
        self._buffer[o:o+len(data)] = data

    def _zero(self, addr: int, length: int):
        if self._manual_mode or self._external_mode:
            ctypes.memset(addr, 0, length)
            return
        o = addr - self.base_address
        self._buffer[o:o+length] = b'\x00' * length

    def _fill(self, addr: int, byte_value: int, length: int):
        if self._manual_mode or self._external_mode:
            ctypes.memset(addr, byte_value & 0xFF, length)
            return
        o = addr - self.base_address
        self._buffer[o:o+length] = bytes([byte_value & 0xFF]) * length

    def _copy(self, dst: int, src: int, length: int):
        if self._manual_mode or self._external_mode:
            ctypes.memmove(dst, src, length)
            return
        do = dst - self.base_address
        so = src - self.base_address
        self._buffer[do:do+length] = self._buffer[so:so+length]

    def stats(self) -> Dict:
        return {
            'size': self.size,
            'base_address': hex(self.base_address),
            'allocators': {k: v.stats() for k, v in self._allocators.items()},
            'pools': {k: v.stats() for k, v in self._pools.items()},
        }

    def __repr__(self) -> str:
        return f"PyMemSandbox(size={_format_bytes(self.size)}, base={hex(self.base_address)})"

# ================= 公开API =================
class PyMemForce:
    @staticmethod
    def sandbox(size: int = DEFAULT_POOL_SIZE) -> PyMemSandbox:
        return PyMemSandbox(size)

    @staticmethod
    def buffer(size: int):
        return _GCFreeBuffer(size)

    @staticmethod
    def topology() -> Dict:
        return _cpu_topo.info()

    @staticmethod
    def version() -> str:
        return __version__

def sandbox(size: int = DEFAULT_POOL_SIZE) -> PyMemSandbox:
    return PyMemSandbox(size)

def topology() -> Dict:
    return _cpu_topo.info()

# ================= GC-Free缓冲区 =================
class _GCFreeBuffer:
    def __init__(self, size: int, alignment: int = 4096):
        self.size = _align(size, alignment)
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

    def read_bytes(self, offset: int, length: int) -> bytes:
        if self._freed:
            raise BufferOverflowError("read", offset, length, 0)
        if offset + length > self.size:
            raise BufferOverflowError("read", offset, length, self.size)
        return self._mmap[offset:offset+length]

    def write_bytes(self, offset: int, data: bytes):
        if self._freed:
            raise BufferOverflowError("write", offset, len(data), 0)
        if offset + len(data) > self.size:
            raise BufferOverflowError("write", offset, len(data), self.size)
        self._mmap[offset:offset+len(data)] = data

    def read_i32(self, offset: int) -> int:
        return int.from_bytes(self.read_bytes(offset, 4), BYTE_ORDER, signed=True)

    def write_i32(self, offset: int, value: int):
        self.write_bytes(offset, value.to_bytes(4, BYTE_ORDER, signed=True))

    def read_f32(self, offset: int) -> float:
        return struct.unpack('f', self.read_bytes(offset, 4))[0]

    def write_f32(self, offset: int, value: float):
        self.write_bytes(offset, struct.pack('f', value))

    def read_f64(self, offset: int) -> float:
        return struct.unpack('d', self.read_bytes(offset, 8))[0]

    def write_f64(self, offset: int, value: float):
        self.write_bytes(offset, struct.pack('d', value))

    def zero(self, offset: int, length: int):
        self.write_bytes(offset, b'\x00' * length)

    def __len__(self) -> int:
        return self.size

    def __repr__(self) -> str:
        return f"GCFreeBuffer(size={_format_bytes(self.size)})"

# ================= 演示 =================
if __name__ == "__main__":
    print(f"PyMemForce v{__version__}")
    print(f"CPU: {topology()}")
    print(f"Default Pool: {_format_bytes(DEFAULT_POOL_SIZE)}")

    sb = sandbox(1024 * 1024)
    print(f"\nCreated: {sb}")

    p = sb.alloc(256)
    sb.write(p, 42)
    print(f"Allocated {hex(p)}, value = {sb.read(p)}")
    sb.delete(p)

    pool = sb.pool(64, 10)
    ptrs = [pool.alloc() for _ in range(3)]
    print(f"Pool: {pool}")
    for ptr in ptrs:
        pool.free(ptr)

    sb.close()
    print("\nAll tests passed.")