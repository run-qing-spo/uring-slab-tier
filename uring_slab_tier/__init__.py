"""vLLM 的 uring-slab secondary tier。

当前只包含 Python 控制面（纯状态机，见 manager.py）；C++ DataEngine 与
vLLM SecondaryTierManager adapter 后续接入。设计依据：

- docs/URING_SLAB_CONTROL_PLANE_MODEL_V1.md
- docs/URING_SLAB_CONTROL_PLANE_SYSTEM_STORIES_V1.md
"""

from .manager import (
    AlignmentError,
    BlockCompletion,
    BlockIo,
    ContractViolationError,
    ControlPlaneError,
    ControlPlaneStats,
    InvariantViolationError,
    IoDirection,
    JobId,
    JobMetadata,
    JobResult,
    Key,
    SlabGeometry,
    SlotId,
    UringSlabControlPlane,
)

__all__ = [
    "AlignmentError",
    "BlockCompletion",
    "BlockIo",
    "ContractViolationError",
    "ControlPlaneError",
    "ControlPlaneStats",
    "InvariantViolationError",
    "IoDirection",
    "JobId",
    "JobMetadata",
    "JobResult",
    "Key",
    "SlabGeometry",
    "SlotId",
    "UringSlabControlPlane",
]
