import asyncio
import sys

import pytest

from src.scrapers.resources import ResourceBusyError, ResourceLease


@pytest.mark.asyncio
async def test_three_entrypoints_serialize_shared_resource(tmp_path):
    active = 0
    completed = []

    async def entrypoint(index):
        nonlocal active
        async with ResourceLease(tmp_path / "source.lock", wait_seconds=1):
            active += 1
            assert active == 1
            await asyncio.sleep(0.02)
            completed.append(index)
            active -= 1

    await asyncio.gather(*(entrypoint(i) for i in range(3)))
    assert sorted(completed) == [0, 1, 2]


@pytest.mark.asyncio
async def test_lock_is_cross_process_and_crash_releases_it(tmp_path):
    path = tmp_path / "source.lock"
    script = "import fcntl,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(10)"
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, str(path), stdout=asyncio.subprocess.PIPE
    )
    try:
        assert await child.stdout.readline() == b"locked\n"
        with pytest.raises(ResourceBusyError):
            async with ResourceLease(path, wait_seconds=0.03):
                pytest.fail("entered another process's lease")
    finally:
        child.terminate()
        await child.wait()
    async with ResourceLease(path, wait_seconds=0.03):
        pass


@pytest.mark.asyncio
async def test_cancelled_wait_does_not_steal_or_leak_lock(tmp_path):
    path = tmp_path / "source.lock"
    async with ResourceLease(path):
        waiter = asyncio.create_task(ResourceLease(path).acquire())
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    async with ResourceLease(path, wait_seconds=0.03):
        pass
