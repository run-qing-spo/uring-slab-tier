# uring-slab secondary：scheduler 流程与部分失败

范围：vLLM `v0.24.0`、`BLOCK_LEVEL`、单个 secondary tier。

## 正常 scheduler 流程

1. 新请求到达  
   `on_new_request` 返回 `BLOCK_LEVEL`。candidate 不维护 request 状态时，
   `on_request_finished` 无事可做。

2. Store  
   GPU→primary store 完成后，上层 pin primary blocks，登记 store job，再调用
   `submit_store`。secondary 异步写入 slab。

3. Lookup  
   scheduler 查询 key：
   - `False`：本 tier miss；
   - `None`：稍后重查；
   - `True`：上层立即预留 primary target，本次对外仍返回 `None`，并暂存
     promotion。

   lookup hit 本身不 pin secondary block；真正 `submit_load` 时必须重新检查。

4. Step 结束  
   上层先收割一次 completion，再把本 step 暂存的 promotion 合成 load job，
   登记后调用 `submit_load`。

5. Completion  
   `get_finished_jobs` 每个 scheduler step 最多实际收割一次，入口可以是
   lookup、prepare_load 或 step 结束：
   - store completion：上层解除 primary source pins；
   - load success：上层把全部 primary targets 标为 ready；
   - load failure：上层撤销全部 primary target reservations。

6. Reset 与 shutdown  
   reset 会先 drain、收割 completion，再重置 primary。candidate 的 shutdown
   也必须等待自己的 I/O 停止，因为上层随后会释放 primary memory。

## 部分失败

### Store

一个 store job 可以包含多个 blocks：

- 等所有已提交 I/O 结束；
- 成功写完的 block 保留为 resident；
- 失败 block 不进入 resident；
- 任一 block 失败则 job completion 为 failure；
- 不回滚已经成功的 secondary blocks。

上层对 store success/failure 都只做同一件事：解除这个 job 的全部 primary
source pins。

### Load

一个 load job 也可以包含多个 blocks：

- 等所有已提交 I/O 结束；
- 任一 block 失败则整个 job completion 为 failure；
- 上层对全部 keys 执行失败收敛，撤销全部 primary target reservations；
- 已经搬到 primary 的部分 bytes 不再有记录，下一次 promotion 必须整批重做。

失败的 secondary block 不能继续无条件 lookup `True`，否则永久 I/O 错误会形成：

```text
lookup True → submit_load → failure → lookup True → ...
```

最小进展规则是：失败 block 后续 lookup `False`；同 job 中读取成功的 secondary
blocks 可以继续保留。

实现上不必额外缓存字面上的 `False`：失败时把该 block 从可读索引移除，或将其
标记为 invalid，lookup 再由此返回 `False`。因此 DataEngine completion 必须能
指出具体失败的 block；控制面对上层仍只聚合成一个 job-level success/failure。

## 必须成立

```text
block 有 active job  => 不可逐出
job terminal         => 该 job 已无 I/O
每个 accepted job    => 恰好一个 completion
store 部分失败       => 保留成功 blocks，job failure
load 部分失败        => primary 整批失败，job failure
```
