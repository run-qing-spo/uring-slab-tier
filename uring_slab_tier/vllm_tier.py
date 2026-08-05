"""vLLM v0.24.0 SecondaryTierManager adapter + uring-slab DataEngine 绑定层。

两件事：

1. adapter：把 SecondaryTierManager 的调用翻译成控制面调用（参数原样传递、
   不传漏）；
2. 绑定层：把控制面 submit_* 产出的 block 四元组逐条喂给 C++ DataEngine，
   并把 DataEngine 的 completion 逐条经 complete_block 喂回控制面。

数据流：

    scheduler → submit_store/load → 控制面分配 slot、返回四元组
                                  → engine.submit_store/load 逐 block 下发
    engine 完成 → poll_completions() → 控制面 complete_block → get_finished_jobs

上游锁定 commit：ee0da84ab9e04ac7610e28580af62c365e898389（v0.24.0）。
所有方法都在 scheduler 线程调用；lookup 纯同步。DataEngine 自带 owner 线程
独占 io_uring，completion 通过 poll/drain 拉取，本 adapter 只在单一 scheduler
线程里泵送，不引入额外并发。运行期可预期失败（duplicate、容量不足、submit
时 key 已失效、engine 队列满回 EAGAIN）都转成 JobResult(success=False)。

key 账本键：v0.24.0 的 OffloadKey 明确是 bytes，DataEngine 按 bytes 收发并
原样回传，控制面亦以它为账本键。_encode_key 是唯一卡点，只做严格类型校验：
非 bytes 即上游契约被破坏，fail-fast。
"""

from vllm.logger import init_logger
from vllm.v1.kv_offload.base import OffloadKey, ReqContext, RequestOffloadingContext
from vllm.v1.kv_offload.tiering.base import (
    JobMetadata,
    JobResult,
    SecondaryTierManager,
)

from uring_slab_tier.manager import (
    ContractViolationError,
    IoDirection,
    JobMetadata as CpJobMetadata,
    UringSlabControlPlane,
)

try:
    from uring_slab_tier import _uring_slab_engine
except ImportError as exc:  # 扩展未编译（如非 Linux 开发机）
    _uring_slab_engine = None
    _ENGINE_IMPORT_ERROR: ImportError | None = exc
else:
    _ENGINE_IMPORT_ERROR = None

logger = init_logger(__name__)


def _encode_key(key: OffloadKey) -> bytes:
    """校验 OffloadKey 为 bytes 并原样返回（DataEngine 与控制面共用的账本键）。

    v0.24.0 的 OffloadKey 明确是 bytes；DataEngine 按 bytes 收发并原样回传，
    控制面亦以它为账本键。非 bytes 即上游契约被破坏，fail-fast，绝不用
    repr() 之类兜底把类型错误糊成一个能跑但对不上账的 key。
    """
    if isinstance(key, bytes):
        return key
    raise ContractViolationError(
        f"OffloadKey 必须是 bytes（v0.24.0 契约），得到 {type(key).__name__}"
    )


class UringSlabSecondaryTierManager(SecondaryTierManager):
    """uring-slab secondary tier：控制面账本 + C++ io_uring DataEngine。

    secondary_tiers 配置示例：

        {
          "type": "uring_slab",
          "disk_bytes_to_use": 107374182400,
          "slab_path": "/mnt/nvme/uring_slab.bin",
          "total_qd": 128,
          "pending_capacity": 4096
        }

    slot_bytes 取 primary_kv_view.strides[0]；slot_capacity =
    disk_bytes_to_use // slot_bytes；slab 恰好铺 slot_capacity 个 slot。
    """

    def __init__(
        self,
        offloading_spec,
        primary_kv_view: memoryview,
        tier_type: str,
        disk_bytes_to_use: int,
        slab_path: str,
        total_qd: int = 128,
        pending_capacity: int = 4096,
        delay_miss_one_step: bool = False,
    ) -> None:
        super().__init__(offloading_spec, primary_kv_view, tier_type)

        assert primary_kv_view.strides is not None, (
            "primary_kv_view.strides cannot be None"
        )
        if not isinstance(disk_bytes_to_use, int) or disk_bytes_to_use <= 0:
            raise ValueError(
                f"disk_bytes_to_use 必须是正整数字节数，得到 {disk_bytes_to_use!r}"
            )
        if not slab_path:
            raise ValueError("slab_path 不能为空")
        if _uring_slab_engine is None:
            raise RuntimeError(
                "_uring_slab_engine 未编译——需在目标 Linux 上构建 csrc/"
            ) from _ENGINE_IMPORT_ERROR

        self._slot_bytes: int = primary_kv_view.strides[0]
        # slot_capacity < 1（预算小于一个 slot）时控制面构造抛错，启动失败
        slot_capacity = disk_bytes_to_use // self._slot_bytes
        self._cp = UringSlabControlPlane(slot_capacity=slot_capacity)
        self._delay_miss_one_step = delay_miss_one_step
        self._misses_seen_this_step: set[tuple[str, bytes]] = set()
        self._misses_ready: set[tuple[str, bytes]] = set()
        # slab 恰好铺 slot_capacity 个 slot：是 slot_bytes 的整数倍，满足引擎
        # 「slab_bytes % block_size == 0」约束，且与控制面 slot 数一致
        slab_bytes = slot_capacity * self._slot_bytes
        self._engine = _uring_slab_engine.DataEngine(
            primary_kv_view,
            slab_path,
            slab_bytes,
            total_qd=total_qd,
            pending_capacity=pending_capacity,
        )
        # drain_jobs 提前收割的 terminal 结果，等下一次 get_finished_jobs 交付
        self._pending_results: list = []
        self._closed = False
        logger.info(
            "uring-slab tier 已创建：slot_bytes=%d, slot_capacity=%d, slab_path=%s",
            self._slot_bytes,
            slot_capacity,
            slab_path,
        )

    # ------------------------------------------------------------------
    # 查询 · 热度
    # ------------------------------------------------------------------

    def lookup(self, key: OffloadKey, req_context: ReqContext) -> bool | None:
        """纯同步三态：True=resident；None=store 在途；False=miss。"""
        encoded_key = _encode_key(key)
        result = self._cp.lookup(encoded_key)
        if not self._delay_miss_one_step or result is not False:
            return result
        miss_key = (req_context.req_id, encoded_key)
        if miss_key in self._misses_ready:
            return False
        self._misses_seen_this_step.add(miss_key)
        return None

    # touch：继承基类 no-op —— v1 无 eviction/LRU，无热度可更新。

    # ------------------------------------------------------------------
    # 异步 job
    # ------------------------------------------------------------------

    def submit_store(self, job_metadata: JobMetadata) -> None:
        self._submit(job_metadata, expected_promotion=False)

    def submit_load(self, job_metadata: JobMetadata) -> None:
        self._submit(job_metadata, expected_promotion=True)

    def _submit(self, job_metadata: JobMetadata, expected_promotion: bool) -> None:
        if self._closed:
            raise ContractViolationError("shutdown 之后不得再提交 job")
        if bool(job_metadata.is_promotion) is not expected_promotion:
            raise ContractViolationError(
                f"job {job_metadata.job_id} 的 is_promotion="
                f"{job_metadata.is_promotion} 与调用入口不符"
            )

        cp_job = CpJobMetadata(
            job_id=job_metadata.job_id,
            keys=tuple(_encode_key(k) for k in job_metadata.keys),
            primary_slots=tuple(int(b) for b in job_metadata.block_ids),
            direction=IoDirection.LOAD if expected_promotion else IoDirection.STORE,
        )
        # 控制面分配 slot 并返回 (job_id, key, primary_slot, secondary_slot)；
        # 逐 block 下发给 DataEngine。key 已是 bytes，引擎收发一致。
        if expected_promotion:
            assignments = self._cp.submit_load(cp_job)
            for job_id, key, primary_slot, secondary_slot in assignments:
                self._engine.submit_load(job_id, key, primary_slot, secondary_slot)
        else:
            assignments = self._cp.submit_store(cp_job)
            for job_id, key, primary_slot, secondary_slot in assignments:
                self._engine.submit_store(job_id, key, primary_slot, secondary_slot)

    def _pump_completions(self) -> None:
        """把 DataEngine 已完成的 block 逐条喂回控制面。"""
        for job_id, key, success, _error_code in self._engine.poll_completions():
            self._cp.complete_block(job_id, key, success)

    def get_finished_jobs(self) -> list[JobResult]:
        self._pump_completions()
        results = self._pending_results + self._cp.get_finished_jobs()
        self._pending_results = []
        return [
            JobResult(job_id=result.job_id, success=result.success)
            for result in results
        ]

    # ------------------------------------------------------------------
    # request / step 通知
    # ------------------------------------------------------------------

    def on_new_request(self, req_context: ReqContext) -> RequestOffloadingContext:
        """固定 BLOCK_LEVEL（RequestOffloadingContext 的默认 policy）。"""
        return RequestOffloadingContext()

    def on_request_finished(self, req_context: ReqContext) -> None:
        if not self._delay_miss_one_step:
            return
        req_id = req_context.req_id
        self._misses_seen_this_step = {
            item for item in self._misses_seen_this_step if item[0] != req_id
        }
        self._misses_ready = {
            item for item in self._misses_ready if item[0] != req_id
        }

    def on_schedule_end(self) -> None:
        """实验开关：使本轮首次观测到的 miss 在下一轮可见。"""
        if self._delay_miss_one_step:
            self._misses_ready.update(self._misses_seen_this_step)
            self._misses_seen_this_step.clear()

    def has_pending_work(self) -> bool:
        return (
            bool(self._pending_results)
            or self._cp.has_pending_jobs()
            or self._engine.has_pending_work()
        )

    # ------------------------------------------------------------------
    # drain · 关闭
    # ------------------------------------------------------------------

    def drain_jobs(self) -> None:
        """等所有已提交 block terminal，再把结果收进待交付缓冲。

        engine.drain() 阻塞至所有已接受 block 完成，并消费式返回尚未 poll 的
        completion；逐条喂回控制面后，本地失败与 I/O 完成的 job 都已 terminal，
        由之后的 get_finished_jobs() 交付（exactly-once 不变）。
        """
        for job_id, key, success, _error_code in self._engine.drain():
            self._cp.complete_block(job_id, key, success)
        self._pending_results.extend(self._cp.get_finished_jobs())
        if self._cp.has_pending_jobs():
            raise RuntimeError(
                "uring-slab tier drain 后仍有未 terminal job（completion 契约异常）"
            )

    def clear_residency(self) -> bool:
        """清空 secondary resident 账本；调用方必须先完成 drain。"""
        if self._engine.has_pending_work():
            raise RuntimeError("DataEngine 仍有 pending work，不能清空 resident 账本")
        self._cp.clear_residency()
        return True

    def shutdown(self) -> None:
        """阻止新提交并停引擎：engine.shutdown() 完成已接受任务并 join owner。"""
        self._closed = True
        self._engine.shutdown()
        pending = self._cp.stats().pending_jobs
        if pending:
            logger.warning(
                "uring-slab tier shutdown 时仍有 %d 个未收割 job", pending
            )

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------

    @property
    def control_plane(self) -> UringSlabControlPlane:
        """实验 harness 由此对接控制面账本。"""
        return self._cp

    @property
    def data_engine(self):
        """实验 harness 由此对接 DataEngine（QD、pending 观测）。"""
        return self._engine
