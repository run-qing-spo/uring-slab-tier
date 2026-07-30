"""vLLM 的 uring-slab secondary tier。

- Python 控制面（纯状态机账本，见 manager.py）；
- C++ io_uring DataEngine（见 csrc/，pybind 模块 _uring_slab_engine）；
- vllm_tier.py 是 SecondaryTierManager adapter 兼绑定层，把控制面产出的
  block 四元组喂给 DataEngine，并把 DataEngine 的 completion 喂回控制面。

设计依据：

- docs/URING_SLAB_CONTROL_PLANE_MODEL_V1.md
- docs/URING_SLAB_CONTROL_PLANE_SYSTEM_STORIES_V1.md
- docs/URING_SLAB_DATA_ENGINE_DESIGN_V1.md
"""

from .manager import (
    BlockAssignment,
    ContractViolationError,
    ControlPlaneError,
    ControlPlaneStats,
    InvariantViolationError,
    IoDirection,
    JobId,
    JobMetadata,
    JobResult,
    Key,
    SlotId,
    UringSlabControlPlane,
)

__all__ = [
    "BlockAssignment",
    "ContractViolationError",
    "ControlPlaneError",
    "ControlPlaneStats",
    "InvariantViolationError",
    "IoDirection",
    "JobId",
    "JobMetadata",
    "JobResult",
    "Key",
    "SlotId",
    "UringSlabControlPlane",
]
