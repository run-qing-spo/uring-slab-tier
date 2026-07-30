"""uring-slab tier 的 vLLM 接入入口（spec_module_path 指向本模块）。

vLLM 启动配置（kv_connector_extra_config）示例：

    {
      "spec_name": "UringSlabOffloadingSpec",
      "spec_module_path": "uring_slab_tier.vllm_spec",
      "cpu_bytes_to_use": 4294967296,
      "secondary_tiers": [
        {
          "type": "uring_slab",
          "disk_bytes_to_use": 107374182400,
          "slab_path": "/mnt/nvme/uring_slab.bin",
          "total_qd": 128,
          "pending_capacity": 4096
        }
      ]
    }

vLLM 的 OffloadingSpecFactory 按 spec_module_path import 本模块并取
spec_name 对应的类；import 的副作用是向 SecondaryTierFactory 注册
"uring_slab" tier type。全程不修改 vLLM source tree。

⚠️ slab_path 的唯一性由部署方负责：每个 DP rank / vLLM 实例必须配置各自
独立的 slab_path（如加 instance_id / rank 后缀）。DataEngine 构造时对
slab 文件执行 ftruncate(0) 且不加文件锁，多个实例共享同一路径会互相截断、
静默损坏数据；且 V1 控制面索引不持久化，本就不能共享同一个 slab。本 tier
不代为去重路径。
"""

from vllm.v1.kv_offload.tiering.factory import SecondaryTierFactory
from vllm.v1.kv_offload.tiering.spec import TieringOffloadingSpec


class UringSlabOffloadingSpec(TieringOffloadingSpec):
    """与 TieringOffloadingSpec 行为完全一致。

    存在的唯一目的：给 spec_name 一个从本包加载的名字，使 vLLM 走
    spec_module_path 导入本模块，从而完成 uring_slab tier 注册。
    """


# 不允许同名注册幂等：模块体每进程只执行一次，register_tier 正常只跑一次。
# 若同一进程内出现第二次 "uring_slab" 注册，要么是配置把两个不同实现挂到
# 同名，要么是异常的重复 import——都是 bug。SecondaryTierFactory 对重复 name
# 本就抛 ValueError，这里不捕获，让它当场炸。
SecondaryTierFactory.register_tier(
    "uring_slab",
    "uring_slab_tier.vllm_tier",
    "UringSlabSecondaryTierManager",
)
