"""slab resident 账本清理测试。"""

import pytest

from uring_slab_tier.manager import (
    IoDirection,
    JobMetadata,
    UringSlabControlPlane,
)


def _store_job(job_id: int, key: bytes) -> JobMetadata:
    return JobMetadata(
        job_id=job_id,
        keys=(key,),
        primary_slots=(0,),
        direction=IoDirection.STORE,
    )


def _load_job(job_id: int, key: bytes) -> JobMetadata:
    return JobMetadata(
        job_id=job_id,
        keys=(key,),
        primary_slots=(0,),
        direction=IoDirection.LOAD,
    )


def test_clear_residency_makes_old_key_miss_and_reuses_slot_zero() -> None:
    control_plane = UringSlabControlPlane(slot_capacity=2)

    assignments = control_plane.submit_store(_store_job(1, b"old"))
    assert assignments[0][3] == 0
    control_plane.complete_block(1, b"old", success=True)
    assert control_plane.get_finished_jobs()[0].success
    assert control_plane.lookup(b"old") is True

    control_plane.clear_residency()

    assert control_plane.lookup(b"old") is False
    assert control_plane.stats().resident_keys == 0
    assert control_plane.stats().next_slot_id == 0
    assert control_plane.submit_load(_load_job(2, b"old")) == []
    assert not control_plane.get_finished_jobs()[0].success

    assignments = control_plane.submit_store(_store_job(3, b"new"))
    assert assignments[0][3] == 0


def test_clear_residency_rejects_inflight_store() -> None:
    control_plane = UringSlabControlPlane(slot_capacity=1)
    control_plane.submit_store(_store_job(1, b"key"))

    with pytest.raises(RuntimeError, match="in-flight store"):
        control_plane.clear_residency()


def test_clear_residency_rejects_unharvested_job() -> None:
    control_plane = UringSlabControlPlane(slot_capacity=1)
    control_plane.submit_load(_load_job(1, b"missing"))

    with pytest.raises(RuntimeError, match="未收割 job"):
        control_plane.clear_residency()
