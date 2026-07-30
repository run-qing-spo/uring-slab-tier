"""uring-slab tier 的 vLLM 接入入口（spec_module_path 指向本模块）。

vLLM 启动配置（kv_connector_extra_config）示例：

    {
      "spec_name": "UringSlabOffloadingSpec",
      "spec_module_path": "uring_slab_tier.vllm_spec",
      "cpu_bytes_to_use": 4294967296,
      "secondary_tiers": [
        {"type": "uring_slab", "disk_bytes_to_use": 107374182400}
      ]
    }

vLLM 的 OffloadingSpecFactory 按 spec_module_path import 本模块并取
spec_name 对应的类；import 的副作用是向 SecondaryTierFactory 注册
"uring_slab" tier type。全程不修改 vLLM source tree。
"""

from vllm.v1.kv_offload.tiering.factory import SecondaryTierFactory
from vllm.v1.kv_offload.tiering.spec import TieringOffloadingSpec


class UringSlabOffloadingSpec(TieringOffloadingSpec):
    """与 TieringOffloadingSpec 行为完全一致。

    存在的唯一目的：给 spec_name 一个从本包加载的名字，使 vLLM 走
    spec_module_path 导入本模块，从而完成 uring_slab tier 注册。
    """


try:
    SecondaryTierFactory.register_tier(
        "uring_slab",
        "uring_slab_tier.vllm_tier",
        "UringSlabSecondaryTierManager",
    )
except ValueError:
    # 已注册（同进程内重复 import 入口），保持幂等
    pass
