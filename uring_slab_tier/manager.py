"""uring-slab secondary tier 的 Python 控制面（纯状态机）。

实现 docs/URING_SLAB_CONTROL_PLANE_MODEL_V1.md 的核心数据结构与状态转移，
以及 docs/URING_SLAB_CONTROL_PLANE_SYSTEM_STORIES_V1.md 的 lookup 三态与
部分失败语义。

边界（DataEngine 已接入，见 csrc/ 与 vllm_tier.py 绑定层）：

    上层       → 控制面：JobMetadata(job_id, keys, primary_slots, direction)
    控制面     → 绑定层：submit_* 返回四元组
                 (job_id, key, primary_slot, secondary_slot)，逐条喂 engine.submit_*
    DataEngine → 控制面：complete_block(job_id, key, success)
    控制面     → 上层：JobResult(job_id, success)

控制面只管账本，不做 I/O、不 import engine：submit_* 分配 slot 并把每个待办
block 以四元组返回，绑定层据此调用 DataEngine；DataEngine 的 completion 元组
由绑定层逐条经 complete_block() 喂回；terminal 结果由 get_finished_jobs()
一次性取走（exactly-once）。四元组不用 dataclass 包装——它只是 slot 分配的
出口，direction 由绑定层按调用入口自行路由。

约定：

- 单线程使用：所有方法只允许同一个 scheduler 线程调用；
- 第一版无 eviction：slot 由 next_slot_id 单调分配、永不复用，
  store/load 失败留下永久空洞，next_slot_id 不回退；
- DataEngine 契约：每个已接受的 block 恰好产生一个 completion，
  (job_id, key) 原样返回；控制面信任该契约；
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

# 控制面 → 绑定层的单 block 待办：(job_id, key, primary_slot, secondary_slot)。
# direction 不入元组，由绑定层按 submit_store / submit_load 入口自行路由。
BlockAssignment = tuple[JobId, Key, int, SlotId]


class ControlPlaneError(RuntimeError):
    """控制面错误基类。"""


class ContractViolationError(ControlPlaneError):
    """调用方违反 frozen contract（非法 JobMetadata、非法构造参数等）。"""


class InvariantViolationError(ControlPlaneError):
    """内部账本或 DataEngine completion 契约被破坏；整个实验 run 判无效。"""


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
    """控制面 → 上层：job 的 terminal 结果（全部 block 成功才为 True）。"""

    job_id: JobId
    success: bool


@dataclass(frozen=True)
class ControlPlaneStats:
    """控制面可观测状态快照。

    正式性能 run 要求 store/load failure、capacity rejection 均为 0 增量。
    """

    store_failed_jobs: int
    load_failed_jobs: int
    capacity_rejected_jobs: int
    next_slot_id: SlotId
    resident_keys: int
    inflight_store_keys: int
    pending_jobs: int


@dataclass
class _JobState:
    """一个未收割 job 的账本项。

    记录 direction，因为 completion 不携带方向，complete_block 的分派与
    失败计数归属需要它。
    """

    direction: IoDirection
    remaining: int
    success: bool = True


class UringSlabControlPlane:
    """uring-slab 控制面：key/slot/job 账本与 completion 聚合。

    对上接收 JobMetadata、交付 JobResult；对下产出 block 四元组、消费
    complete_block。不做 I/O，也不 import engine。
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

    def submit_store(self, job: JobMetadata) -> list[BlockAssignment]:
        """接收 store job，返回需下发给 DataEngine 的 block 四元组列表。

        - resident duplicate：过滤物理 I/O，不影响 job success；
        - in-flight duplicate：整 job 立即本地 failure，不分配 slot、不下发
          I/O，计入 store_failed_jobs；
        - 容量不足（next_slot_id + 新 key 数 > slot_capacity）：整 job 本地
          failure，计入 capacity_rejected_jobs（与 store_failed_jobs 互斥）；
        - 全部为 resident duplicate 的 job 无需 I/O，返回 []、直接以 success
          等待收割。

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

        assignments: list[BlockAssignment] = []
        for key, primary_slot in new_pairs:
            slot = self._next_slot_id
            self._next_slot_id += 1
            self._key2inflight_store[key] = slot
            assignments.append((job.job_id, key, primary_slot, slot))
        self._jobs[job.job_id] = _JobState(IoDirection.STORE, remaining=len(assignments))
        return assignments

    def submit_load(self, job: JobMetadata) -> list[BlockAssignment]:
        """接收 load job，返回需下发给 DataEngine 的 block 四元组列表。

        lookup True 与 submit_load 之间存在时间窗口，必须重新检查全部 key：
        任一 key 已不 resident（含仍在 in-flight store）则整 job 本地 failure、
        不下发任何 I/O，计入 load_failed_jobs；job 内其余 key 保持 resident。
        """
        self._admit(job, IoDirection.LOAD)

        if any(key not in self._key2slot for key in job.keys):
            self._jobs[job.job_id] = _JobState(
                IoDirection.LOAD, remaining=0, success=False
            )
            self._load_failed_jobs += 1
            return []

        assignments = [
            (job.job_id, key, primary_slot, self._key2slot[key])
            for key, primary_slot in zip(job.keys, job.primary_slots)
        ]
        self._jobs[job.job_id] = _JobState(IoDirection.LOAD, remaining=len(assignments))
        return assignments

    def complete_block(self, job_id: JobId, key: Key, success: bool) -> None:
        """消费 DataEngine 的单 block 结果，推进 job 与 key/slot 状态。

        绑定层把引擎 completion 元组 (job_id, key, success, error_code) 逐条
        转进来（error_code 已在绑定层折进 success，这里不需要）。

        store failure 留下的 slot 是永久空洞；load failure 将该 key 从
        key2slot 移除（后续 lookup False），同 job 其他成功 block 保持
        resident。未知 job_id、多余或方向不匹配的 completion 抛
        InvariantViolationError。
        """
        state = self._jobs.get(job_id)
        if state is None:
            raise InvariantViolationError(
                f"completion 的 job_id {job_id} 不存在于 jobs（key={key!r}）"
            )
        if state.remaining <= 0:
            raise InvariantViolationError(
                f"job {job_id} 的 remaining 将被减到 0 以下（key={key!r}）"
            )

        if state.direction is IoDirection.STORE:
            slot = self._key2inflight_store.get(key)
            if slot is None:
                raise InvariantViolationError(
                    f"store completion 的 key 不在 key2inflight_store：{key!r}"
                )
            if success:
                if key in self._key2slot:
                    raise InvariantViolationError(
                        f"key 同时存在于 key2slot 与 key2inflight_store：{key!r}"
                    )
                del self._key2inflight_store[key]
                self._key2slot[key] = slot
            else:
                # slot 成为永久空洞：next_slot_id 不回退，slot 永不复用
                del self._key2inflight_store[key]
                self._mark_job_failed(state)
        else:
            if not success:
                # 并发 load 同一 key 时可能已被先失败的 job 移除，pop 幂等
                self._key2slot.pop(key, None)
                self._mark_job_failed(state)
            # load success：key2slot 保持不变

        state.remaining -= 1

    def get_finished_jobs(self) -> list[JobResult]:
        """返回并删除所有 remaining == 0 的 job（每个只交付一次）。"""
        finished_ids = [
            job_id for job_id, state in self._jobs.items() if state.remaining == 0
        ]
        return [
            JobResult(job_id, self._jobs.pop(job_id).success) for job_id in finished_ids
        ]

    def has_pending_jobs(self) -> bool:
        """存在尚未被 get_finished_jobs() 取走的 job（含待收割项）。"""
        return bool(self._jobs)

    def clear_residency(self) -> None:
        """在 drain 并收割全部 job 后清空 resident 账本。

        物理 slab 不清零；清空 key→slot 关系后 lookup 会立即 miss，slot
        分配游标回到 0，使后续 store 从头覆盖旧数据。
        """
        if self._key2inflight_store:
            raise RuntimeError("存在 in-flight store，不能清空 resident 账本")
        if self._jobs:
            raise RuntimeError("存在未收割 job，不能清空 resident 账本")

        self._key2slot.clear()
        self._next_slot_id = 0

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

    def _mark_job_failed(self, state: _JobState) -> None:
        # 每个失败 job 只计数一次（首次由 True 翻转为 False 时）
        if state.success:
            state.success = False
            if state.direction is IoDirection.STORE:
                self._store_failed_jobs += 1
            else:
                self._load_failed_jobs += 1
