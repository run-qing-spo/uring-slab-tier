# uring-slab 控制面：核心数据结构

控制面对上接收和返回 job；对下拆成单 block I/O，再聚合 completion。

## 边界

```text
上层 → 控制面：
  JobMetadata(job_id, keys, primary_slots, store/load)

控制面 → DataEngine：
  BlockIo(job_id, key, primary_slot, secondary_slot, store/load)

DataEngine → 控制面：
  BlockCompletion(job_id, key, success)

控制面 → 上层：
  JobResult(job_id, success)
```

`key` 对 DataEngine 只是原样返回的关联标识。DataEngine 不管理 lookup、
容量或 job 聚合。

## 控制面数据

```cpp
unordered_map<Key, SlotId> key2slot;

unordered_map<Key, SlotId> key2inflight_store;

struct JobState {
    size_t remaining;
    bool success = true;
};
unordered_map<JobId, JobState> jobs;

SlotId next_slot_id = 0;
const size_t slot_capacity;

uint64_t store_failed_jobs = 0;
uint64_t store_failed_blocks = 0;
uint64_t load_failed_jobs = 0;
uint64_t load_failed_blocks = 0;
uint64_t capacity_rejected_jobs = 0;
```

## 状态变化

```text
Store success:
  slot = key2inflight_store.at(key)
  key2inflight_store.erase(key)
  key2slot[key] = slot

Store failure:
  key2inflight_store.erase(key)
  jobs[job_id].success = false

Load success:
  key2slot 保持不变

Load failure:
  key2slot.erase(key)
  jobs[job_id].success = false

每个 BlockCompletion:
  --jobs[job_id].remaining
  remaining == 0 -> 等待 get_finished_jobs 返回并删除
```

`remaining` 只统计实际下发的 block I/O。无需 I/O 的 job 直接保持为 0。
`get_finished_jobs` 扫描 `jobs`，返回并删除其中 `remaining == 0` 的项。

DataEngine 把 `(job_id, key)` 当作不可变关联标签：每个已接受的 `BlockIo`
恰好产生一个 `BlockCompletion`，并原样返回这两个字段。控制面信任这个契约，
不重复保存 key 到 job 的关系。

## 断言

```text
同一 key 不同时存在于 key2slot 和 key2inflight_store
同一 slot 不同时属于两个 key
BlockCompletion 的 job_id 必须存在于 jobs
remaining 不得减到 0 以下
每个 remaining == 0 的 job 只被 get_finished_jobs 返回一次
```

第一版不做容量 eviction，与原生 FS 的保留语义对齐。因此 secondary 不需要
pin、LRU、free-list 或 `touch` 状态。

新 store job 先计算真正需要写入的 key 数量：

```text
next_slot_id + new_key_count > slot_capacity
  => 整个 store job failure

否则
  => 分配连续区间 [next_slot_id, next_slot_id + new_key_count)
  => next_slot_id += new_key_count
```

正式实验必须保证 slab 能容纳该 run 的全部唯一 blocks 并留有余量。若容量仍然
耗尽，新的 store job 明确 failure，不覆盖已有 resident blocks。

slot 分配后永不复用。store/load 失败留下永久空洞；`next_slot_id` 不回退，
因为后续 job 可能已经取得更大的 slot。

## 空洞统计

空洞数不单独维护，直接由现有状态推导：

```text
hole_count =
  next_slot_id
  - key2slot.size()
  - key2inflight_store.size()

hole_ratio =
  hole_count / next_slot_id
```

正式性能 run 要求 I/O failure、capacity rejection 和 `hole_count` 增量均为 0。
故障注入测试允许产生空洞，但每个 case 使用新 slab。

## O_DIRECT 对齐

slab 从文件 offset 0 开始没有问题；0 对任何正对齐值都对齐。slot offset 为：

```text
slab_offset = slot_id * slot_bytes
```

构造时必须从 slab 文件查询 direct-I/O alignment，并验证：

```text
slot_bytes % dio_offset_align == 0
primary_base_address % dio_mem_align == 0
primary_row_stride % dio_mem_align == 0
I/O length % dio_offset_align == 0
slab_file_bytes >= slot_capacity * slot_bytes
```

目标机是 Linux 6.17 + ext4，可使用 `statx(STATX_DIOALIGN)` 取得
`stx_dio_mem_align` 和 `stx_dio_offset_align`，不能只假定为 4096。
