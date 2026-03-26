# coding=utf-8
"""
MemBlock 统一内存模型。

该模型统一接管：
  1. `outer buffer` / `uncache` TileLink 端口
  2. `dcache client` TileLink-C 端口
  3. `mem_to_ooo.intWriteback` 数据校验

当前实现只支持全 load 场景；若观测到 store/AMO/其他未覆盖事务，直接失败。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import random


TL_A_PUT_FULL = 0
TL_A_PUT_PARTIAL = 1
TL_A_GET = 4
TL_A_ACQUIRE_BLOCK = 6
TL_A_ACQUIRE_PERM = 7

TL_C_PROBE_ACK = 4
TL_C_PROBE_ACK_DATA = 5
TL_C_RELEASE = 6
TL_C_RELEASE_DATA = 7

TL_D_ACCESS_ACK = 0
TL_D_ACCESS_ACK_DATA = 1
TL_D_GRANT = 4
TL_D_GRANT_DATA = 5
TL_D_RELEASE_ACK = 6

DEFAULT_CACHELINE_BYTES = 64
DEFAULT_DCACHE_BEAT_BYTES = 32


def _is_connected(signal) -> bool:
    try:
        signal.value
    except AttributeError:
        return False
    return True


def _signal_value(signal, default=None):
    try:
        return int(signal.value)
    except AttributeError:
        return default


@dataclass(frozen=True)
class RobIndex:
    """ROB 索引键。"""

    flag: int
    value: int


@dataclass
class ExpectedLoad:
    """一笔待校验的 load 写回。"""

    rob_idx: RobIndex
    pdest: int
    addr: int
    size: int
    mask: int
    expected_data: int
    expected_int_wen: int = 1
    expect_exception: bool = False


class MemoryModel:
    """MemBlock 全 load 场景统一内存模型。"""

    def __init__(
        self,
        dut,
        outer_a,
        outer_d,
        dcache_a,
        dcache_b,
        dcache_c,
        dcache_d,
        dcache_e,
        writebacks=None,
        outer_delay: int = 2,
        grant_delay: int = 2,
        release_ack_delay: int = 1,
    ) -> None:
        self.dut = dut
        self.outer_a = outer_a
        self.outer_d = outer_d
        self.dcache_a = dcache_a
        self.dcache_b = dcache_b
        self.dcache_c = dcache_c
        self.dcache_d = dcache_d
        self.dcache_e = dcache_e
        self.writebacks = list(writebacks or [])
        self.outer_delay = outer_delay
        self.grant_delay = grant_delay
        self.release_ack_delay = release_ack_delay

        self.memory = {}
        self._cycle = 0
        self._runtime_reset_active = False
        self._next_sink = 1

        self._pending_outer_d = deque()
        self._active_outer_d = None

        self._pending_b = deque()
        self._active_b = None

        self._pending_d = deque()
        self._active_d = None
        self._inflight_grants = {}

        self._expected_loads = defaultdict(deque)
        self.strict_writeback_check = True
        self.completed_loads = 0
        self.writeback_events = 0
        self.failed_transactions = 0

        self.outer_request_count = 0
        self.outer_response_count = 0
        self.dcache_a_request_count = 0
        self.dcache_c_request_count = 0
        self.dcache_e_request_count = 0
        self.dcache_b_response_count = 0
        self.dcache_d_response_count = 0

        self.drive_idle()

    def attach_writebacks(self, writebacks) -> None:
        """绑定 writeback 端口列表。"""

        self.writebacks = list(writebacks)
        self.drive_writeback_ready()

    def drive_idle(self) -> None:
        """驱动模型输出通道为空闲。"""

        self.outer_d.drive_idle()
        self.dcache_b.drive_idle()
        self.dcache_d.drive_idle()
        self.drive_writeback_ready()

    def drive_writeback_ready(self) -> None:
        """保持 writeback ready 恒高。"""

        for bundle in self.writebacks:
            if hasattr(bundle, "set_ready"):
                bundle.set_ready(1)

    def reset_runtime_state(self) -> None:
        """清理运行态，不清空预加载内存内容。"""

        self._pending_outer_d.clear()
        self._active_outer_d = None
        self._pending_b.clear()
        self._active_b = None
        self._pending_d.clear()
        self._active_d = None
        self._inflight_grants.clear()
        self._expected_loads.clear()
        self.drive_idle()

    def reset(self) -> None:
        """完全复位模型，包括预加载内存。"""

        self.memory.clear()
        self._next_sink = 1
        self.completed_loads = 0
        self.writeback_events = 0
        self.failed_transactions = 0
        self.outer_request_count = 0
        self.outer_response_count = 0
        self.dcache_a_request_count = 0
        self.dcache_c_request_count = 0
        self.dcache_e_request_count = 0
        self.dcache_b_response_count = 0
        self.dcache_d_response_count = 0
        self.reset_runtime_state()

    def preload_bytes(self, base_addr: int, data: bytes) -> None:
        """按字节预加载内存。"""

        for offset, value in enumerate(data):
            self.memory[base_addr + offset] = value & 0xFF

    def preload_u64(self, addr: int, value: int) -> None:
        """按小端预加载一个 64-bit 数据。"""

        self.preload_bytes(addr, int(value & ((1 << 64) - 1)).to_bytes(8, "little"))

    def fill_random(
        self,
        addr_start: int,
        addr_end: int,
        seed: int,
        line_bytes: int = DEFAULT_CACHELINE_BYTES,
    ) -> None:
        """按 cacheline 粒度随机填充内存。"""

        if addr_end < addr_start:
            raise ValueError("addr_end 不能小于 addr_start")
        rng = random.Random(seed)
        start = addr_start - (addr_start % line_bytes)
        end = addr_end + ((line_bytes - (addr_end % line_bytes)) % line_bytes)
        for base in range(start, end, line_bytes):
            self.preload_bytes(
                base,
                bytes(rng.getrandbits(8) for _ in range(line_bytes)),
            )

    def read(self, addr: int, size: int) -> int:
        """按小端读取内存。"""

        value = 0
        for offset in range(size):
            value |= (self.memory.get(addr + offset, 0) & 0xFF) << (offset * 8)
        return value

    def read_masked(self, addr: int, mask: int, width_bytes: int = 8) -> int:
        """按字节掩码读取内存并打包到低位。"""

        value = 0
        out_offset = 0
        for byte_idx in range(width_bytes):
            if (mask >> byte_idx) & 0x1:
                byte = self.memory.get(addr + byte_idx, 0) & 0xFF
                value |= byte << (out_offset * 8)
                out_offset += 1
        return value

    def read_cacheline(self, block_addr: int, line_bytes: int = DEFAULT_CACHELINE_BYTES) -> bytes:
        """读取一个 cacheline。"""

        return bytes(self.memory.get(block_addr + idx, 0) & 0xFF for idx in range(line_bytes))

    def expect_load(
        self,
        rob_idx_flag: int,
        rob_idx_value: int,
        pdest: int,
        addr: int,
        size: int = 8,
        mask: int | None = None,
    ) -> ExpectedLoad:
        """登记一笔待校验的 load。"""

        width_mask = (1 << size) - 1
        effective_mask = width_mask if mask is None else mask
        expected = ExpectedLoad(
            rob_idx=RobIndex(flag=rob_idx_flag, value=rob_idx_value),
            pdest=pdest,
            addr=addr,
            size=size,
            mask=effective_mask,
            expected_data=self.read_masked(addr, effective_mask, width_bytes=size),
        )
        self._expected_loads[expected.rob_idx].append(expected)
        return expected

    @property
    def outstanding_expected_count(self) -> int:
        """返回待校验 load 数。"""

        return sum(len(items) for items in self._expected_loads.values())

    @property
    def outstanding_transaction_count(self) -> int:
        """返回未完成 TL 事务数。"""

        return (
            len(self._pending_outer_d)
            + (1 if self._active_outer_d is not None else 0)
            + len(self._pending_b)
            + (1 if self._active_b is not None else 0)
            + len(self._pending_d)
            + (1 if self._active_d is not None else 0)
            + len(self._inflight_grants)
        )

    @property
    def stats(self) -> dict:
        """统计信息。"""

        return {
            "outer_request_count": self.outer_request_count,
            "outer_response_count": self.outer_response_count,
            "dcache_a_request_count": self.dcache_a_request_count,
            "dcache_c_request_count": self.dcache_c_request_count,
            "dcache_e_request_count": self.dcache_e_request_count,
            "dcache_b_response_count": self.dcache_b_response_count,
            "dcache_d_response_count": self.dcache_d_response_count,
            "pending_outer_d_count": len(self._pending_outer_d),
            "pending_b_count": len(self._pending_b),
            "pending_d_count": len(self._pending_d),
            "active_outer_d_count": 1 if self._active_outer_d is not None else 0,
            "active_b_count": 1 if self._active_b is not None else 0,
            "active_d_count": 1 if self._active_d is not None else 0,
            "inflight_grant_count": len(self._inflight_grants),
            "outstanding_expected_count": self.outstanding_expected_count,
            "completed_loads": self.completed_loads,
            "writeback_events": self.writeback_events,
        }

    def enqueue_outer_response(
        self,
        opcode: int = TL_D_ACCESS_ACK_DATA,
        param: int = 0,
        size: int = 3,
        source: int = 0,
        request_source: int | None = None,
        sink: int = 0,
        denied: int = 0,
        data: int = 0,
        corrupt: int = 0,
        delay_cycles: int | None = None,
    ) -> None:
        """兼容接口：手工注入一笔 outer D 响应。"""

        delay = self.outer_delay if delay_cycles is None else delay_cycles
        self._pending_outer_d.append(
            {
                "release_cycle": self._cycle + delay,
                "opcode": opcode,
                "param": param,
                "size": size,
                "source": source,
                "request_source": source if request_source is None else request_source,
                "sink": sink,
                "denied": denied,
                "data": data,
                "corrupt": corrupt,
            }
        )

    def enqueue_b_response(
        self,
        opcode: int = 0,
        param: int = 0,
        size: int = 6,
        source: int = 0,
        address: int = 0,
        mask: int = 0,
        data: int = 0,
        corrupt: int = 0,
        delay_cycles: int | None = None,
    ) -> None:
        """兼容接口：手工注入一笔 B 响应。"""

        delay = self.grant_delay if delay_cycles is None else delay_cycles
        self._pending_b.append(
            {
                "release_cycle": self._cycle + delay,
                "opcode": opcode,
                "param": param,
                "size": size,
                "source": source,
                "address": address,
                "mask": mask,
                "data": data,
                "corrupt": corrupt,
            }
        )

    def enqueue_d_response(
        self,
        opcode: int = TL_D_GRANT_DATA,
        param: int = 0,
        size: int = 6,
        source: int = 0,
        request_channel: str = "a",
        request_source: int | None = None,
        sink: int = 0,
        denied: int = 0,
        echo_is_keyword: int = 0,
        data: int = 0,
        corrupt: int = 0,
        delay_cycles: int | None = None,
    ) -> None:
        """兼容接口：手工注入一笔 D 响应。"""

        delay = self.grant_delay if delay_cycles is None else delay_cycles
        self._pending_d.append(
            {
                "release_cycle": self._cycle + delay,
                "beats": deque(
                    [
                        {
                            "opcode": opcode,
                            "param": param,
                            "size": size,
                            "source": source,
                            "request_channel": request_channel,
                            "request_source": source if request_source is None else request_source,
                            "sink": sink,
                            "denied": denied,
                            "echo_isKeyword": echo_is_keyword,
                            "data": data,
                            "corrupt": corrupt,
                        }
                    ]
                ),
            }
        )

    def on_memory_edge(self, cycle: int) -> None:
        """驱动模型总线接口。"""

        self._cycle = cycle
        self._drive_request_readies()
        if self._in_reset():
            self.drive_idle()
            return

        self._capture_outer_request()
        self._capture_dcache_requests()
        self._service_outer_d()
        self._service_b_channel()
        self._service_d_channel()

    def after_cycle(self) -> None:
        """在每个周期推进后处理 reset 与 writeback。"""

        self.drive_writeback_ready()
        if self._in_reset():
            if not self._runtime_reset_active:
                self.reset_runtime_state()
            self._runtime_reset_active = True
            return

        self._runtime_reset_active = False
        self.check_writebacks()

    def check_writebacks(self) -> None:
        """校验当前拍 writeback。"""

        for bundle in self.writebacks:
            if not getattr(bundle, "connected", lambda name: False)("valid"):
                continue
            if bundle.read("valid", 0) == 0:
                continue
            if bundle.read("ready", 0) == 0:
                continue

            self.writeback_events += 1
            if bundle.connected("isFromLoadUnit") and bundle.read("isFromLoadUnit", 0) == 0:
                continue
            if (
                not bundle.connected("data_0")
                or not bundle.connected("pdest")
                or not bundle.connected("intWen")
                or not bundle.connected("robIdx_flag")
                or not bundle.connected("robIdx_value")
            ):
                continue
            if bundle.read("intWen", 0) == 0:
                continue

            rob_idx = RobIndex(
                flag=bundle.read("robIdx_flag", 0),
                value=bundle.read("robIdx_value", 0),
            )
            if not self._expected_loads[rob_idx]:
                if self.strict_writeback_check:
                    raise AssertionError(f"观测到未登记的 load writeback: robIdx={rob_idx}")
                continue

            expected = self._expected_loads[rob_idx].popleft()
            observed_data = bundle.read("data_0", 0)
            observed_pdest = bundle.read("pdest", expected.pdest)
            observed_int_wen = bundle.read("intWen", expected.expected_int_wen)
            exception_bits = bundle.read_exception_bits()
            if observed_pdest != expected.pdest:
                raise AssertionError(
                    f"load writeback pdest 不匹配: robIdx={rob_idx}, expected={expected.pdest}, observed={observed_pdest}"
                )
            if observed_int_wen != expected.expected_int_wen:
                raise AssertionError(
                    f"load writeback intWen 不匹配: robIdx={rob_idx}, expected={expected.expected_int_wen}, observed={observed_int_wen}"
                )
            if observed_data != expected.expected_data:
                raise AssertionError(
                    "load writeback data 不匹配: "
                    f"robIdx={rob_idx}, addr=0x{expected.addr:x}, "
                    f"expected=0x{expected.expected_data:x}, observed=0x{observed_data:x}"
                )
            if any(exception_bits) and not expected.expect_exception:
                raise AssertionError(
                    f"load writeback 异常位非 0: robIdx={rob_idx}, exceptionVec={exception_bits}"
                )
            self.completed_loads += 1
            if not self._expected_loads[rob_idx]:
                del self._expected_loads[rob_idx]

    def _in_reset(self) -> bool:
        return bool(_signal_value(self.dut.reset, 0) or _signal_value(self.dut.io_reset_backend, 0))

    def _drive_request_readies(self) -> None:
        self.outer_a.ready.value = 1
        self.dcache_a.ready.value = 1
        self.dcache_c.ready.value = 1
        self.dcache_e.ready.value = 1

    def _capture_outer_request(self) -> None:
        if not (self.outer_a.valid.value and self.outer_a.ready.value):
            return

        opcode = int(self.outer_a.bits_opcode.value)
        if opcode != TL_A_GET:
            raise AssertionError(f"outer buffer 仅支持 Get，请求 opcode={opcode}")

        self.outer_request_count += 1
        size = 1 << int(self.outer_a.bits_size.value)
        addr = int(self.outer_a.bits_address.value)
        data = self.read(addr, size)
        self.enqueue_outer_response(
            opcode=TL_D_ACCESS_ACK_DATA,
            size=int(self.outer_a.bits_size.value),
            source=int(self.outer_a.bits_source.value),
            request_source=int(self.outer_a.bits_source.value),
            data=data,
            corrupt=0,
        )

    def _capture_dcache_requests(self) -> None:
        if self.dcache_a.valid.value and self.dcache_a.ready.value:
            self.dcache_a_request_count += 1
            self._handle_dcache_a()

        if self.dcache_c.valid.value and self.dcache_c.ready.value:
            self.dcache_c_request_count += 1
            self._handle_dcache_c()

        if self.dcache_e.valid.value and self.dcache_e.ready.value:
            self.dcache_e_request_count += 1
            self._handle_dcache_e()

    def _handle_dcache_a(self) -> None:
        opcode = int(self.dcache_a.bits_opcode.value)
        if opcode != TL_A_ACQUIRE_BLOCK:
            raise AssertionError(f"load-only 场景仅支持 AcquireBlock，观测到 A.opcode={opcode}")

        block_addr = int(self.dcache_a.bits_address.value) & ~(DEFAULT_CACHELINE_BYTES - 1)
        size = int(self.dcache_a.bits_size.value)
        source = int(self.dcache_a.bits_source.value)
        is_keyword = int(self.dcache_a.bits_echo_isKeyword.value)
        line = self.read_cacheline(block_addr, DEFAULT_CACHELINE_BYTES)
        beats = [
            int.from_bytes(line[idx: idx + DEFAULT_DCACHE_BEAT_BYTES], "little")
            for idx in range(0, DEFAULT_CACHELINE_BYTES, DEFAULT_DCACHE_BEAT_BYTES)
        ]
        if is_keyword:
            beats = list(reversed(beats))

        sink = self._alloc_sink()
        self._inflight_grants[sink] = {"source": source, "opcode": opcode}
        self._pending_d.append(
            {
                "release_cycle": self._cycle + self.grant_delay,
                "beats": deque(
                    [
                        {
                            "opcode": TL_D_GRANT_DATA,
                            "param": 0,
                            "size": size,
                            "source": source,
                            "request_channel": "a",
                            "request_source": source,
                            "sink": sink,
                            "denied": 0,
                            "echo_isKeyword": is_keyword,
                            "data": beat,
                            "corrupt": 0,
                        }
                        for beat in beats
                    ]
                ),
            }
        )

    def _handle_dcache_c(self) -> None:
        opcode = int(self.dcache_c.bits_opcode.value)
        if opcode in (TL_C_PROBE_ACK, TL_C_PROBE_ACK_DATA):
            return
        if opcode in (TL_C_RELEASE, TL_C_RELEASE_DATA):
            self._pending_d.append(
                {
                    "release_cycle": self._cycle + self.release_ack_delay,
                    "beats": deque(
                        [
                            {
                                "opcode": TL_D_RELEASE_ACK,
                                "param": 0,
                                "size": int(self.dcache_c.bits_size.value),
                                "source": int(self.dcache_c.bits_source.value),
                                "request_channel": "c",
                                "request_source": int(self.dcache_c.bits_source.value),
                                "sink": 0,
                                "denied": 0,
                                "echo_isKeyword": int(self.dcache_c.bits_echo_isKeyword.value),
                                "data": 0,
                                "corrupt": 0,
                            }
                        ]
                    ),
                }
            )
            return
        raise AssertionError(f"未支持的 DCache C 通道 opcode={opcode}")

    def _handle_dcache_e(self) -> None:
        sink = int(self.dcache_e.bits_sink.value)
        if sink not in self._inflight_grants:
            raise AssertionError(f"观测到未知的 GrantAck sink={sink}")
        del self._inflight_grants[sink]

    def _service_outer_d(self) -> None:
        if self._active_outer_d is None and self._pending_outer_d:
            if self._pending_outer_d[0]["release_cycle"] <= self._cycle:
                self._active_outer_d = self._pending_outer_d.popleft()

        if self._active_outer_d is None:
            self.outer_d.drive_idle()
            return

        response = self._active_outer_d
        if response["source"] != response["request_source"]:
            raise AssertionError(
                "outer D 通道 source 必须与前序 A 通道请求 source 一致: "
                f"request_source={response['request_source']}, d_source={response['source']}"
            )
        self.outer_d.valid.value = 1
        self.outer_d.bits_opcode.value = response["opcode"]
        self.outer_d.bits_param.value = response["param"]
        self.outer_d.bits_size.value = response["size"]
        self.outer_d.bits_source.value = response["source"]
        self.outer_d.bits_sink.value = response["sink"]
        self.outer_d.bits_denied.value = response["denied"]
        self.outer_d.bits_data.value = response["data"]
        self.outer_d.bits_corrupt.value = response["corrupt"]

        if self.outer_d.ready.value:
            self.outer_response_count += 1
            self._active_outer_d = None

    def _service_b_channel(self) -> None:
        if self._active_b is None and self._pending_b:
            if self._pending_b[0]["release_cycle"] <= self._cycle:
                self._active_b = self._pending_b.popleft()

        if self._active_b is None:
            self.dcache_b.drive_idle()
            return

        response = self._active_b
        self.dcache_b.valid.value = 1
        self.dcache_b.bits_opcode.value = response["opcode"]
        self.dcache_b.bits_param.value = response["param"]
        self.dcache_b.bits_size.value = response["size"]
        self.dcache_b.bits_source.value = response["source"]
        self.dcache_b.bits_address.value = response["address"]
        self.dcache_b.bits_mask.value = response["mask"]
        self.dcache_b.bits_data.value = response["data"]
        self.dcache_b.bits_corrupt.value = response["corrupt"]

        if self.dcache_b.ready.value:
            self.dcache_b_response_count += 1
            self._active_b = None

    def _service_d_channel(self) -> None:
        if self._active_d is None and self._pending_d:
            if self._pending_d[0]["release_cycle"] <= self._cycle:
                self._active_d = self._pending_d.popleft()

        if self._active_d is None:
            self.dcache_d.drive_idle()
            return

        beat = self._active_d["beats"][0]
        if beat.get("request_channel") == "a" and beat["source"] != beat["request_source"]:
            raise AssertionError(
                "dcache D 通道 source 必须与前序 A 通道请求 source 一致: "
                f"request_source={beat['request_source']}, d_source={beat['source']}"
            )
        self.dcache_d.valid.value = 1
        self.dcache_d.bits_opcode.value = beat["opcode"]
        self.dcache_d.bits_param.value = beat["param"]
        self.dcache_d.bits_size.value = beat["size"]
        self.dcache_d.bits_source.value = beat["source"]
        self.dcache_d.bits_sink.value = beat["sink"]
        self.dcache_d.bits_denied.value = beat["denied"]
        self.dcache_d.bits_echo_isKeyword.value = beat["echo_isKeyword"]
        self.dcache_d.bits_data.value = beat["data"]
        self.dcache_d.bits_corrupt.value = beat["corrupt"]

        if self.dcache_d.ready.value:
            self.dcache_d_response_count += 1
            self._active_d["beats"].popleft()
            if not self._active_d["beats"]:
                self._active_d = None

    def _alloc_sink(self) -> int:
        sink = self._next_sink
        self._next_sink = (self._next_sink + 1) & 0x3FF
        if self._next_sink == 0:
            self._next_sink = 1
        return sink
