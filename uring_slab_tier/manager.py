"""uring-slab secondary tier 的 Python 控制面（当前形态：submit 侧账本）。

实现 docs/URING_SLAB_CONTROL_PLANE_MODEL_V1.md 的 submit 侧数据结构、
lookup 三态与 submit 的本地失败语义。

当前裁剪形态（重要）——completion 回路、BlockIo/BlockCompletion 边界、
O_DIRECT 几何校验（SlabGeometry）与全量断言尚未接入，将在 DataEngine
落地时连同 on_block_completion 一并补回。因此现在：

- submit_store 会分配 slot 并把 key 记为 in-flight，但没有 completion 使其
  转 resident。lookup 对已 store 的 key 持续返回 None（在途），submit_load
  因 key 不 resident 而每次本地失败——store→resident→load 需要 DataEngine。
- 有真实 I/O 的 job（remaining > 0）永远停在 pending，直到 DataEngine 绑定。

边界：

    上层   → 控制面：JobMetadata(job_id, keys, primary_slots, direction)
    控制面 → 上层：JobResult(job_id, success)

约定：

- 单线程使用：所有方法只允许同一个 scheduler 线程调用；
- 第一版无 eviction：slot 由 next_slot_id 单调分配、永不复用；
- 运行期可预期失败（in-flight duplicate、容量不足、submit_load 时 key
  不 resident）不抛异常，而是产生一次 JobResult(success=False)；
- 调用方违反 frozen contract 抛 ContractViolationError（fail-fast）。
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
class JobResult:
    """控制面 → 上层：job 的 terminal 结果。"""

    job_id: JobId
    success: bool


@dataclass(frozen=True)
class ControlPlaneStats:
    """控制面可观测状态快照（submit 侧）。"""

    store_failed_jobs: int
    load_failed_jobs: int
    capacity_rejected_jobs: int
    next_slot_id: SlotId
    resident_keys: int
    inflight_store_keys: int
    pending_jobs: int


@dataclass
class _JobState:
    """一个未收割 job 的账本项。"""

    remaining: int
    success: bool = True


class UringSlabControlPlane:
    """uring-slab 控制面：key/slot/job 账本（submit 侧）。

    对上接收 JobMetadata、交付 JobResult；不做 I/O。completion 回路由
    DataEngine 落地时接入，届时有真实 I/O 的 job 才能变 terminal。
    """

    def __init__(self, slot_capacity: int) -> None:
        """slot_capacity 为 slab 的 slot 总数（构造期 fail-fast）。"""
        if slot_capacity < 1:
            raise ContractViolationError(f"slot_capacity 必须 >= 1，得到 {slot_capacity}")

        self._slot_capacity = slot_capacity
        self._key2slot: dict[Key, SlotId] = {}
        self._key2inflight_store: dict[Key, SlotId] = {}
        self._jobs: dict[JobId, _JobState] = {}
        self._next_slot_id: SlotId = 0

        self._store_failed_jobs = 0
        self._load_failed_jobs = 0
        self._capacity_rejected_jobs = 0

    # ------------------------------------------------------------------
    # 对上接口
    # ------------------------------------------------------------------

    def lookup(self, key: Key) -> bool | None:
        """三态 lookup：True=resident；None=store 在途；False=miss。"""
        if key in self._key2slot:
            return True
        if key in self._key2inflight_store:
            return None
        return False

    def submit_store(self, job: JobMetadata) -> None:
        """接收 store job，登记 slot 分配与 in-flight 账本。

        - in-flight duplicate：整 job 立即本地 failure，计入 store_failed_jobs；
        - 容量不足（next_slot_id + 新 key 数 > slot_capacity）：整 job 本地
          failure，计入 capacity_rejected_jobs；
        本地 failure 的 job 同样由 get_finished_jobs() 交付；可预期失败不抛出。
        """
        self._admit(job, IoDirection.STORE)

        if any(key in self._key2inflight_store for key in job.keys):
            self._jobs[job.job_id] = _JobState(remaining=0, success=False)
            self._store_failed_jobs += 1
            return

        new_keys = [key for key in job.keys if key not in self._key2slot]

        if self._next_slot_id + len(new_keys) > self._slot_capacity:
            self._jobs[job.job_id] = _JobState(remaining=0, success=False)
            self._capacity_rejected_jobs += 1
            return

        for key in new_keys:
            self._key2inflight_store[key] = self._next_slot_id
            self._next_slot_id += 1
        self._jobs[job.job_id] = _JobState(remaining=len(new_keys))

    def submit_load(self, job: JobMetadata) -> None:
        """接收 load job；任一 key 不 resident 则整 job 本地 failure。"""
        self._admit(job, IoDirection.LOAD)

        if any(key not in self._key2slot for key in job.keys):
            self._jobs[job.job_id] = _JobState(remaining=0, success=False)
            self._load_failed_jobs += 1
            return

        self._jobs[job.job_id] = _JobState(remaining=len(job.keys))

    def get_finished_jobs(self) -> list[JobResult]:
        """返回并删除所有 remaining == 0 的 job（每个只交付一次）。"""
        finished_ids = [
            job_id for job_id, state in self._jobs.items() if state.remaining == 0
        ]
        return [
            JobResult(job_id, self._jobs.pop(job_id).success) for job_id in finished_ids
        ]

    def has_pending_jobs(self) -> bool:
        """存在尚未被 get_finished_jobs() 取走的 job。"""
        return bool(self._jobs)

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------

    @property
    def slot_capacity(self) -> int:
        return self._slot_capacity

    def stats(self) -> ControlPlaneStats:
        """观测快照，供正式 run 前后对账失败增量。"""
        return ControlPlaneStats(
            store_failed_jobs=self._store_failed_jobs,
            load_failed_jobs=self._load_failed_jobs,
            capacity_rejected_jobs=self._capacity_rejected_jobs,
            next_slot_id=self._next_slot_id,
            resident_keys=len(self._key2slot),
            inflight_store_keys=len(self._key2inflight_store),
            pending_jobs=len(self._jobs),
        )

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
