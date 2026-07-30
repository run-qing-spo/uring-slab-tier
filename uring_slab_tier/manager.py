"""uring-slab secondary tier 的 Python 控制面（纯状态机）。

实现 docs/URING_SLAB_CONTROL_PLANE_MODEL_V1.md 的核心数据结构与状态转移，
以及 docs/URING_SLAB_CONTROL_PLANE_SYSTEM_STORIES_V1.md 的 lookup 三态与
部分失败语义。

边界：

    上层       → 控制面：JobMetadata(job_id, keys, primary_slots, direction)
    控制面     → DataEngine：BlockIo(job_id, key, primary_slot, secondary_slot, direction)
    DataEngine → 控制面：BlockCompletion(job_id, key, success)
    控制面     → 上层：JobResult(job_id, success)

本模块不做任何 I/O：submit_* 返回应下发给 DataEngine 的 BlockIo 列表，
DataEngine 的 BlockCompletion 由调用方通过 on_block_completion() 喂回，
terminal 结果由 get_finished_jobs() 一次性取走（exactly-once）。

约定：

- 单线程使用：所有方法只允许同一个 scheduler 线程调用；
- 第一版无 eviction：slot 由 next_slot_id 单调分配、永不复用，
  store/load 失败留下永久空洞，next_slot_id 不回退；
- DataEngine 契约：每个已接受的 BlockIo 恰好产生一个 BlockCompletion，
  (job_id, key) 原样返回；控制面信任该契约，不重复保存 key→job 关系；
- 运行期可预期失败（in-flight duplicate、容量不足、submit_load 时 key
  已不 resident）不抛异常，而是产生一次 JobResult(success=False)；
- 调用方违反 frozen contract 抛 ContractViolationError，内部账本或
  completion 契约被破坏抛 InvariantViolationError——两者都是 fail-fast，
  出现即判整个实验 run 无效，不得伪装成普通 job failure。
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from enum import Enum, unique

Key = Hashable
JobId = int
SlotId = int


class ControlPlaneError(RuntimeError):
    """控制面错误基类。"""


class ContractViolationError(ControlPlaneError):
    """调用方违反 frozen contract（非法 JobMetadata、非法构造参数等）。"""


class InvariantViolationError(ControlPlaneError):
    """内部账本或 DataEngine completion 契约被破坏；整个实验 run 判无效。"""


class AlignmentError(ControlPlaneError):
    """O_DIRECT 对齐或 slab 几何校验失败（构造期 fail-fast）。"""


@unique
class IoDirection(Enum):
    """数据方向：primary→secondary 为 STORE，secondary→primary 为 LOAD。"""

    STORE = "store"
    LOAD = "load"


@dataclass(frozen=True)
class JobMetadata:
    """上层 → 控制面：一个待执行的 store/load job。

    keys 与 primary_slots 等长且一一对应；keys 在 job 内不得重复；
    job_id 由上游单调生成，在其生命周期内唯一。
    """

    job_id: JobId
    keys: Sequence[Key]
    primary_slots: Sequence[int]
    direction: IoDirection


@dataclass(frozen=True)
class BlockIo:
    """控制面 → DataEngine：单 block I/O。

    (job_id, key) 是 DataEngine 的不可变关联标签，completion 原样返回。
    """

    job_id: JobId
    key: Key
    primary_slot: int
    secondary_slot: SlotId
    direction: IoDirection


@dataclass(frozen=True)
class BlockCompletion:
    """DataEngine → 控制面：单 block I/O 的 terminal 结果。"""

    job_id: JobId
    key: Key
    success: bool


@dataclass(frozen=True)
class JobResult:
    """控制面 → 上层：job 的 terminal 结果。

    success 只在 job 的全部 block 成功时为 True。
    """

    job_id: JobId
    success: bool


@dataclass(frozen=True)
class SlabGeometry:
    """slab 与 primary 布局的 O_DIRECT 几何参数（构造期校验用）。

    dio_mem_align / dio_offset_align 必须来自对 slab 文件的
    statx(STATX_DIOALIGN) 查询（目标机 Linux 6.17 + ext4），不能只假定
    4096；查询本身由 Linux 侧 engine/adapter 完成，这里只做纯校验。

    io_bytes 是单个 block I/O 的传输长度；None 表示等于 slot_bytes。
    """

    slot_bytes: int
    dio_mem_align: int
    dio_offset_align: int
    primary_base_address: int
    primary_row_stride: int
    slab_file_bytes: int
    io_bytes: int | None = None

    def slab_offset(self, slot_id: SlotId) -> int:
        """slot 在 slab 文件内的字节 offset：slot_id * slot_bytes。"""
        return slot_id * self.slot_bytes

    def validate(self, slot_capacity: int) -> None:
        """按 MODEL_V1「O_DIRECT 对齐」一节校验；失败抛 AlignmentError。"""
        io_bytes = self.slot_bytes if self.io_bytes is None else self.io_bytes

        basic = [
            (self.slot_bytes > 0, f"slot_bytes 必须 > 0，得到 {self.slot_bytes}"),
            (self.dio_mem_align > 0, f"dio_mem_align 必须 > 0，得到 {self.dio_mem_align}"),
            (
                self.dio_offset_align > 0,
                f"dio_offset_align 必须 > 0，得到 {self.dio_offset_align}",
            ),
            (
                self.primary_base_address >= 0,
                f"primary_base_address 必须 >= 0，得到 {self.primary_base_address}",
            ),
            (
                self.primary_row_stride > 0,
                f"primary_row_stride 必须 > 0，得到 {self.primary_row_stride}",
            ),
            (
                0 < io_bytes <= self.slot_bytes,
                f"io_bytes 必须在 (0, slot_bytes] 内，得到 {io_bytes}",
            ),
            (slot_capacity >= 1, f"slot_capacity 必须 >= 1，得到 {slot_capacity}"),
        ]
        failed = [msg for ok, msg in basic if not ok]
        if failed:
            raise AlignmentError("; ".join(failed))

        aligned = [
            (
                self.slot_bytes % self.dio_offset_align == 0,
                "slot_bytes % dio_offset_align != 0",
            ),
            (
                self.primary_base_address % self.dio_mem_align == 0,
                "primary_base_address % dio_mem_align != 0",
            ),
            (
                self.primary_row_stride % self.dio_mem_align == 0,
                "primary_row_stride % dio_mem_align != 0",
            ),
            (
                io_bytes % self.dio_offset_align == 0,
                "I/O length % dio_offset_align != 0",
            ),
            (
                self.slab_file_bytes >= slot_capacity * self.slot_bytes,
                f"slab_file_bytes ({self.slab_file_bytes}) 不足 "
                f"slot_capacity * slot_bytes ({slot_capacity * self.slot_bytes})",
            ),
        ]
        failed = [msg for ok, msg in aligned if not ok]
        if failed:
            raise AlignmentError("; ".join(failed))


@dataclass(frozen=True)
class ControlPlaneStats:
    """控制面可观测状态快照。

    正式性能 run 要求 store/load failure、capacity rejection 与
    hole_count 的增量均为 0。
    """

    store_failed_jobs: int
    store_failed_blocks: int
    load_failed_jobs: int
    load_failed_blocks: int
    capacity_rejected_jobs: int
    next_slot_id: SlotId
    resident_keys: int
    inflight_store_keys: int
    pending_jobs: int
    hole_count: int
    hole_ratio: float


@dataclass
class _JobState:
    """一个未收割 job 的账本项。

    MODEL_V1 的 JobState 只有 remaining/success；这里额外记录 direction，
    因为 BlockCompletion 不携带方向，completion 分派与失败计数归属需要它。
    """

    direction: IoDirection
    remaining: int
    success: bool = True


class UringSlabControlPlane:
    """uring-slab 控制面：key/slot/job 账本与 completion 聚合。

    对上接收 JobMetadata、交付 JobResult；对下产出 BlockIo、消费
    BlockCompletion。不做 I/O，也不管理 primary pin/reservation（上游
    根据 JobResult 自行释放）。
    """

    def __init__(
        self,
        slot_capacity: int,
        geometry: SlabGeometry | None = None,
    ) -> None:
        """slot_capacity 为 slab 的 slot 总数。

        geometry 在正式路径必须提供并通过校验（构造期 fail-fast）；
        纯账本测试可省略。正式实验必须保证 slot_capacity 能容纳该 run
        的全部唯一 blocks 并留有余量。
        """
        if slot_capacity < 1:
            raise ContractViolationError(f"slot_capacity 必须 >= 1，得到 {slot_capacity}")
        if geometry is not None:
            geometry.validate(slot_capacity)

        self._slot_capacity = slot_capacity
        self._geometry = geometry
        self._key2slot: dict[Key, SlotId] = {}
        self._key2inflight_store: dict[Key, SlotId] = {}
        self._jobs: dict[JobId, _JobState] = {}
        self._next_slot_id: SlotId = 0

        self._store_failed_jobs = 0
        self._store_failed_blocks = 0
        self._load_failed_jobs = 0
        self._load_failed_blocks = 0
        self._capacity_rejected_jobs = 0

    # ------------------------------------------------------------------
    # 对上接口
    # ------------------------------------------------------------------

    def lookup(self, key: Key) -> bool | None:
        """三态 lookup。

        True：resident，此刻可接受 load（不 pin，submit_load 时必须重查）；
        None：store 在途，会自行收敛，稍后重查；
        False：miss（含从未 store 与 load 失败后被移除的 key）。
        """
        if key in self._key2slot:
            return True
        if key in self._key2inflight_store:
            return None
        return False

    def submit_store(self, job: JobMetadata) -> list[BlockIo]:
        """接收 store job，返回需下发给 DataEngine 的 BlockIo 列表。

        - resident duplicate：过滤物理 I/O，不影响 job success；
        - in-flight duplicate：整 job 立即本地 failure，不分配 slot、
          不下发 I/O，计入 store_failed_jobs；
        - 容量不足（next_slot_id + 新 key 数 > slot_capacity）：整 job
          本地 failure，计入 capacity_rejected_jobs（与 store_failed_jobs
          互斥，不重复计数）；
        - 全部为 resident duplicate 的 job 无需 I/O，直接以 success 等待收割。

        本地 failure 的 job 同样由 get_finished_jobs() 交付 exactly-once
        结果；运行期可预期失败绝不从这里抛出。
        """
        self._admit(job, IoDirection.STORE)

        if any(key in self._key2inflight_store for key in job.keys):
            self._jobs[job.job_id] = _JobState(
                IoDirection.STORE, remaining=0, success=False
            )
            self._store_failed_jobs += 1
            return []

        new_pairs = [
            (key, primary_slot)
            for key, primary_slot in zip(job.keys, job.primary_slots)
            if key not in self._key2slot
        ]

        if self._next_slot_id + len(new_pairs) > self._slot_capacity:
            self._jobs[job.job_id] = _JobState(
                IoDirection.STORE, remaining=0, success=False
            )
            self._capacity_rejected_jobs += 1
            return []

        ios: list[BlockIo] = []
        for key, primary_slot in new_pairs:
            slot = self._next_slot_id
            self._next_slot_id += 1
            self._key2inflight_store[key] = slot
            ios.append(BlockIo(job.job_id, key, primary_slot, slot, IoDirection.STORE))
        self._jobs[job.job_id] = _JobState(IoDirection.STORE, remaining=len(ios))
        return ios

    def submit_load(self, job: JobMetadata) -> list[BlockIo]:
        """接收 load job，返回需下发给 DataEngine 的 BlockIo 列表。

        lookup True 与 submit_load 之间存在时间窗口，必须重新检查全部
        key：任一 key 已不 resident（含仍在 in-flight store）则整 job
        本地 failure、不下发任何 I/O，计入 load_failed_jobs；job 内其余
        key 保持 resident 不变（只有 I/O 失败的 block 才失去可见性）。
        """
        self._admit(job, IoDirection.LOAD)

        if any(key not in self._key2slot for key in job.keys):
            self._jobs[job.job_id] = _JobState(
                IoDirection.LOAD, remaining=0, success=False
            )
            self._load_failed_jobs += 1
            return []

        ios = [
            BlockIo(job.job_id, key, primary_slot, self._key2slot[key], IoDirection.LOAD)
            for key, primary_slot in zip(job.keys, job.primary_slots)
        ]
        self._jobs[job.job_id] = _JobState(IoDirection.LOAD, remaining=len(ios))
        return ios

    def get_finished_jobs(self) -> list[JobResult]:
        """返回并删除所有 remaining == 0 的 job。

        每个 terminal job 只被返回一次；结果顺序没有保证。
        """
        finished_ids = [
            job_id for job_id, state in self._jobs.items() if state.remaining == 0
        ]
        return [
            JobResult(job_id, self._jobs.pop(job_id).success) for job_id in finished_ids
        ]

    def has_pending_jobs(self) -> bool:
        """存在尚未被 get_finished_jobs() 取走的 job（含待收割项）。"""
        return bool(self._jobs)

    # ------------------------------------------------------------------
    # 对下接口
    # ------------------------------------------------------------------

    def on_block_completion(self, completion: BlockCompletion) -> None:
        """消费 DataEngine 的单 block completion，推进 job 与 key/slot 状态。

        store failure 留下的 slot 是永久空洞；load failure 将该 key 从
        key2slot 移除（后续 lookup False），同 job 其他成功 block 保持
        resident。未知 job_id、多余或方向不匹配的 completion 抛
        InvariantViolationError。
        """
        state = self._jobs.get(completion.job_id)
        if state is None:
            raise InvariantViolationError(
                f"BlockCompletion 的 job_id 不存在于 jobs：{completion!r}"
            )
        if state.remaining <= 0:
            raise InvariantViolationError(
                f"job {completion.job_id} 的 remaining 将被减到 0 以下：{completion!r}"
            )

        if state.direction is IoDirection.STORE:
            slot = self._key2inflight_store.get(completion.key)
            if slot is None:
                raise InvariantViolationError(
                    f"store completion 的 key 不在 key2inflight_store：{completion!r}"
                )
            if completion.success:
                if completion.key in self._key2slot:
                    raise InvariantViolationError(
                        f"key 同时存在于 key2slot 与 key2inflight_store：{completion!r}"
                    )
                del self._key2inflight_store[completion.key]
                self._key2slot[completion.key] = slot
            else:
                # slot 成为永久空洞：next_slot_id 不回退，slot 永不复用
                del self._key2inflight_store[completion.key]
                self._mark_job_failed(state)
                self._store_failed_blocks += 1
        else:
            if not completion.success:
                # 并发 load 同一 key 时可能已被先失败的 job 移除，pop 幂等
                self._key2slot.pop(completion.key, None)
                self._mark_job_failed(state)
                self._load_failed_blocks += 1
            # load success：key2slot 保持不变

        state.remaining -= 1

    # ------------------------------------------------------------------
    # 观测与断言
    # ------------------------------------------------------------------

    @property
    def slot_capacity(self) -> int:
        return self._slot_capacity

    @property
    def next_slot_id(self) -> SlotId:
        return self._next_slot_id

    @property
    def geometry(self) -> SlabGeometry | None:
        return self._geometry

    @property
    def hole_count(self) -> int:
        """已分配但既不 resident 也不 in-flight 的 slot 数（永久空洞）。"""
        return (
            self._next_slot_id
            - len(self._key2slot)
            - len(self._key2inflight_store)
        )

    @property
    def hole_ratio(self) -> float:
        """hole_count / next_slot_id；未分配任何 slot 时为 0.0。"""
        if self._next_slot_id == 0:
            return 0.0
        return self.hole_count / self._next_slot_id

    def stats(self) -> ControlPlaneStats:
        """观测快照，供正式 run 前后对账失败与空洞增量。"""
        return ControlPlaneStats(
            store_failed_jobs=self._store_failed_jobs,
            store_failed_blocks=self._store_failed_blocks,
            load_failed_jobs=self._load_failed_jobs,
            load_failed_blocks=self._load_failed_blocks,
            capacity_rejected_jobs=self._capacity_rejected_jobs,
            next_slot_id=self._next_slot_id,
            resident_keys=len(self._key2slot),
            inflight_store_keys=len(self._key2inflight_store),
            pending_jobs=len(self._jobs),
            hole_count=self.hole_count,
            hole_ratio=self.hole_ratio,
        )

    def check_invariants(self) -> None:
        """O(n) 全量断言（MODEL_V1「断言」一节），供测试与故障注入 harness 使用。

        completion 相关断言（job_id 必须存在、remaining 不减到 0 以下、
        terminal 结果只交付一次）在 on_block_completion / get_finished_jobs
        中逐次强制。
        """
        overlap = self._key2slot.keys() & self._key2inflight_store.keys()
        if overlap:
            sample = [repr(key) for key in list(overlap)[:5]]
            raise InvariantViolationError(
                f"key 同时存在于 key2slot 和 key2inflight_store：{sample}"
            )

        slots = list(self._key2slot.values()) + list(
            self._key2inflight_store.values()
        )
        if len(slots) != len(set(slots)):
            raise InvariantViolationError("同一 slot 同时属于两个 key")
        if any(not 0 <= slot < self._next_slot_id for slot in slots):
            raise InvariantViolationError(
                "存在超出已分配区间 [0, next_slot_id) 的 slot"
            )
        if self._next_slot_id > self._slot_capacity:
            raise InvariantViolationError(
                f"next_slot_id ({self._next_slot_id}) 超过 "
                f"slot_capacity ({self._slot_capacity})"
            )
        if any(state.remaining < 0 for state in self._jobs.values()):
            raise InvariantViolationError("存在 remaining < 0 的 job")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _admit(self, job: JobMetadata, expected: IoDirection) -> None:
        """校验 frozen contract 前置条件；违反即 fail-fast，不转成 job failure。"""
        if job.direction is not expected:
            raise ContractViolationError(
                f"job {job.job_id} 的 direction 是 {job.direction}，"
                f"与提交入口 {expected} 不符"
            )
        if len(job.keys) < 1:
            raise ContractViolationError(f"job {job.job_id} 的 keys 为空")
        if len(job.keys) != len(job.primary_slots):
            raise ContractViolationError(
                f"job {job.job_id} 的 keys ({len(job.keys)}) 与 "
                f"primary_slots ({len(job.primary_slots)}) 长度不等"
            )
        if len(set(job.keys)) != len(job.keys):
            raise ContractViolationError(f"job {job.job_id} 内存在重复 key")
        if job.job_id in self._jobs:
            raise ContractViolationError(
                f"job_id {job.job_id} 已存在于 jobs（上游必须单调唯一）"
            )

    def _mark_job_failed(self, state: _JobState) -> None:
        # 每个失败 job 只计数一次（首次由 True 翻转为 False 时）
        if state.success:
            state.success = False
            if state.direction is IoDirection.STORE:
                self._store_failed_jobs += 1
            else:
                self._load_failed_jobs += 1
