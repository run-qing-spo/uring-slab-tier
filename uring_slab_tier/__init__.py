"""vLLM 的 uring-slab secondary tier。

当前只包含 Python 控制面（submit 侧账本，见 manager.py）；C++ DataEngine
与 completion 回路后续接入。设计依据：

- docs/URING_SLAB_CONTROL_PLANE_MODEL_V1.md
- docs/URING_SLAB_CONTROL_PLANE_SYSTEM_STORIES_V1.md
"""

from .manager import (
    ContractViolationError,
    ControlPlaneError,
    ControlPlaneStats,
    IoDirection,
    JobId,
    JobMetadata,
    JobResult,
    Key,
    SlotId,
    UringSlabControlPlane,
)

__all__ = [
    "ContractViolationError",
    "ControlPlaneError",
    "ControlPlaneStats",
    "IoDirection",
    "JobId",
    "JobMetadata",
    "JobResult",
    "Key",
    "SlotId",
    "UringSlabControlPlane",
]
