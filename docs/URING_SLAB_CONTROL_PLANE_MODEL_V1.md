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
容量、pin 或 job 聚合。

## 控制面数据

```cpp
unordered_map<Key, SlotId> key2slot;

unordered_map<Key, SlotId> key2inflight_store;

enum class PinKind {
    LOOKUP,
    LOAD,
};
unordered_map<Key, PinKind> key2pin;

struct JobState {
    size_t remaining;
    bool success = true;
};
unordered_map<JobId, JobState> jobs;

list<SlotId> free_slots;
```

## 状态变化

```text
Store success:
  slot = key2inflight_store.at(key)
  key2inflight_store.erase(key)
  key2slot[key] = slot

Store failure:
  slot = key2inflight_store.at(key)
  key2inflight_store.erase(key)
  free_slots.push(slot)
  jobs[job_id].success = false

Lookup True:
  key2pin[key] = LOOKUP

submit_load:
  key2pin[key] = LOAD

Load success:
  key2pin.erase(key)

Load failure:
  key2pin.erase(key)
  slot = key2slot.at(key)
  key2slot.erase(key)
  free_slots.push(slot)
  jobs[job_id].success = false

每个 BlockCompletion:
  --jobs[job_id].remaining
  remaining == 0 -> 等待 get_finished_jobs 返回并删除
```

`on_schedule_end` 删除没有转成 `LOAD` 的 `LOOKUP` pins，因为 secondary
lookup `True` 后，primary reservation 仍可能失败。

`remaining` 只统计实际下发的 block I/O。无需 I/O 的 job 直接保持为 0。
`get_finished_jobs` 扫描 `jobs`，返回并删除其中 `remaining == 0` 的项。

DataEngine 把 `(job_id, key)` 当作不可变关联标签：每个已接受的 `BlockIo`
恰好产生一个 `BlockCompletion`，并原样返回这两个字段。控制面信任这个契约，
不重复保存 key 到 job 的关系。

## 断言

```text
同一 key 不同时存在于 key2slot 和 key2inflight_store
同一 slot 不同时属于两个 key
key2pin 中的 key 必须存在于 key2slot
BlockCompletion 的 job_id 必须存在于 jobs
remaining 不得减到 0 以下
每个 remaining == 0 的 job 只被 get_finished_jobs 返回一次
```

eviction 是必需能力；其 LRU/Clock 等 victim 选择结构等策略冻结后再补入。
无论选择哪种策略，inflight store slots 都不进入 victim 集合，pin 只保护
resident slots。
