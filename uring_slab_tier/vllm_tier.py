"""vLLM v0.24.0 SecondaryTierManager 到 uring-slab 控制面的 adapter。

只做一件事：把 SecondaryTierManager 的调用翻译成控制面调用，参数原样
传递、不传漏。不做 I/O，不经手 BlockIo——BlockIo/BlockCompletion 是
控制面与 DataEngine 之间的边界，与本 adapter 无关。

上游锁定 commit：ee0da84ab9e04ac7610e28580af62c365e898389（v0.24.0）。
所有方法都在 scheduler 线程调用；lookup 纯同步（索引就在控制面内存里），
submit_* 只做有界元数据工作，运行期可预期失败（in-flight duplicate、
容量不足、submit 时 key 已失效）由控制面转成一次
JobResult(success=False)，绝不从 submit_* 抛出。
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
    JobResult as CpJobResult,
    UringSlabControlPlane,
)

logger = init_logger(__name__)


class UringSlabSecondaryTierManager(SecondaryTierManager):
    """uring-slab secondary tier（当前形态：控制面账本，DataEngine 后续绑定）。

    secondary_tiers 配置示例：

        {"type": "uring_slab", "disk_bytes_to_use": 107374182400}

    slot_bytes 取 primary_kv_view.strides[0]（一个 offloaded block 的对齐后
    字节跨度），slot_capacity = disk_bytes_to_use // slot_bytes。
    """

    def __init__(
        self,
        offloading_spec,
        primary_kv_view: memoryview,
        tier_type: str,
        disk_bytes_to_use: int,
    ) -> None:
        super().__init__(offloading_spec, primary_kv_view, tier_type)

        assert primary_kv_view.strides is not None, (
            "primary_kv_view.strides cannot be None"
        )
        if not isinstance(disk_bytes_to_use, int) or disk_bytes_to_use <= 0:
            raise ValueError(
                f"disk_bytes_to_use 必须是正整数字节数，得到 {disk_bytes_to_use!r}"
            )
        self._slot_bytes: int = primary_kv_view.strides[0]
        # slot_capacity < 1（预算小于一个 slot）时控制面构造抛错，启动失败
        self._cp = UringSlabControlPlane(
            slot_capacity=disk_bytes_to_use // self._slot_bytes
        )
        # drain_jobs 提前收割的 terminal 结果，等下一次 get_finished_jobs 交付
        self._pending_results: list[CpJobResult] = []
        self._closed = False
        logger.info(
            "uring-slab tier 已创建：slot_bytes=%d, slot_capacity=%d",
            self._slot_bytes,
            self._cp.slot_capacity,
        )

    # ------------------------------------------------------------------
    # 查询 · 热度
    # ------------------------------------------------------------------

    def lookup(self, key: OffloadKey, req_context: ReqContext) -> bool | None:
        """纯同步三态：True=resident；None=store 在途；False=miss。"""
        return self._cp.lookup(key)

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
            keys=tuple(job_metadata.keys),
            primary_slots=tuple(int(b) for b in job_metadata.block_ids),
            direction=IoDirection.LOAD if expected_promotion else IoDirection.STORE,
        )
        # 当前控制面只登记账本、不产出 BlockIo；下发给 DataEngine 的 I/O 与
        # completion 回路由后续 engine 绑定层接入
        if expected_promotion:
            self._cp.submit_load(cp_job)
        else:
            self._cp.submit_store(cp_job)

    def get_finished_jobs(self) -> list[JobResult]:
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

    # on_request_finished：继承基类 no-op —— 无 per-request 状态。
    # on_schedule_end：继承基类 no-op —— lookup 同步、无延迟提交。

    def has_pending_work(self) -> bool:
        return bool(self._pending_results) or self._cp.has_pending_jobs()

    # ------------------------------------------------------------------
    # drain · 关闭
    # ------------------------------------------------------------------

    def drain_jobs(self) -> None:
        """等所有已提交 job terminal。

        先把已 terminal 的结果收进待交付缓冲（仍由之后的
        get_finished_jobs() 交付，exactly-once 不变）；收完后控制面仍有
        job，说明存在在途 block I/O——当前无 DataEngine 绑定，无人能使其
        terminal，fail-fast。engine 绑定层就绪后由其接管真正的阻塞等待。
        """
        self._pending_results.extend(self._cp.get_finished_jobs())
        if self._cp.has_pending_jobs():
            raise RuntimeError(
                "uring-slab tier 无法 drain：存在在途 block I/O，"
                "但 DataEngine 尚未绑定"
            )

    def shutdown(self) -> None:
        """阻止新提交；当前形态无线程/fd 可关，残留 job 仅告警。"""
        self._closed = True
        pending = self._cp.stats().pending_jobs
        if pending:
            logger.warning(
                "uring-slab tier shutdown 时仍有 %d 个未收割 job"
                "（无 DataEngine 形态下在途 I/O 不会再 terminal）",
                pending,
            )

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------

    @property
    def control_plane(self) -> UringSlabControlPlane:
        """DataEngine 绑定层与实验 harness 由此对接控制面。"""
        return self._cp
