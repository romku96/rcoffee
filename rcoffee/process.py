"""
Stateless rclone-based process that cross-copies remote and local content upon start, then watches local and remote for
changes, and reactively syncs them, resolving possible conflicts. Rclone itself has to be installed (see
https://rclone.org/install/) and a remote set up first (see https://rclone.org/remote_setup/).
"""

import asyncio
import json
import logging
import subprocess
from asyncio.subprocess import Process as Subprocess
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Coroutine, Optional, Union

from watchfiles import awatch

log = logging.getLogger(__name__)

SubprocessCoro = Coroutine[Any, Any, Subprocess]


def _run_cmd(*cmd) -> SubprocessCoro:
    assert isinstance(cmd[0], str)
    return asyncio.create_subprocess_exec(cmd[0], *cmd[1:], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@dataclass
class Process:
    """Synchronizes a remote and local directory, watching for changes and polling for updates."""

    remote_path: str
    local_path: Path
    modify_window: str
    batch_cooldown: timedelta
    poll_interval: timedelta

    _local_changed: bool = field(default=False, init=False)
    _remote_changed: bool = field(default=False, init=False)

    def _run_rclone(self, command: str, *args) -> SubprocessCoro:
        return _run_cmd("rclone", command, "-vv", *args)

    def _transfer(self, command: str, source: Union[str, Path], dest: Union[str, Path]) -> SubprocessCoro:
        return self._run_rclone(command, "--update", f"--modify-window={self.modify_window}", source, dest)

    def _sync_update(self, source: str, dest: str) -> SubprocessCoro:
        return self._transfer("sync", source, dest)

    def _copy_update(self, source: Union[str, Path], dest: Union[str, Path]) -> SubprocessCoro:
        return self._transfer("copy", source, dest)

    async def _cross_copy(self) -> None:
        log.info("Starting cross-copy...")
        await self._copy_update(self.remote_path, self.local_path)
        await self._copy_update(self.local_path, self.remote_path)
        log.info("Cross-copy complete")

    async def _dedupe(self) -> None:
        log.info("Starting dedupe...")
        await self._run_rclone("dedupe", "--dedupe-mode", "newest", self.remote_path)
        log.info("Dedupe complete")

    async def _fetch_remote_state(self) -> Any:
        stdout_reader = (await self._run_rclone("lsjson", "--recursive", self.remote_path)).stdout
        stdout_content = await stdout_reader.read() if stdout_reader else None
        return sorted(json.loads(stdout_content) if stdout_content else None, key=lambda item: item["Path"])

    async def _watch_local(self):

        async for _ in awatch(self.local_path, recursive=True):
            log.info("Local changes detected")
            self._local_changed = True

    async def _poll_remote(self) -> None:
        last_state: Optional[Any] = None

        while True:
            new_state = await self._fetch_remote_state()

            if new_state != last_state:
                # json.dump(last_state, open("last_state.json", "w", encoding="utf-8"), indent=2)
                # json.dump(new_state, open("new_state.json", "w", encoding="utf-8"), indent=2)

                log.info("Remote changes detected")
                self._remote_changed = True
                last_state = new_state

            await asyncio.sleep(self.poll_interval.total_seconds())

    async def _sync(self):

        while True:

            while not (self._local_changed or self._remote_changed):
                await asyncio.sleep(0)

            log.info("Batching changes...")

            need_push = False
            need_pull = False

            while self._local_changed or self._remote_changed:
                need_push |= self._local_changed
                need_pull |= self._remote_changed
                self._local_changed, self._remote_changed = False, False
                log.info("Changes detected, sleeping for %ss...", self.batch_cooldown.total_seconds())
                await asyncio.sleep(self.batch_cooldown.total_seconds())

            log.info("Batching complete, processing batched changes...")

            await self._dedupe()

            if need_push and need_pull:
                log.info("Both local and remote have changed")
                await self._cross_copy()
            elif need_push:
                log.info("Pushing local changes...")
                await self._sync_update(self.local_path, self.remote_path)
            elif need_pull:
                log.info("Pulling remote changes...")
                await self._sync_update(self.remote_path, self.local_path)
            else:
                assert False, "Unreachable"

            log.info("Sync complete")

            await self._dedupe()

    async def run_async(self):
        """
        Asynchronously cross-copies remote and local content upon start, then watches local and remote for changes, and
        reactively syncs them, resolving possible conflicts. Rclone itself has to be installed (see
        https://rclone.org/install/) and a remote set up first (see https://rclone.org/remote_setup/).
        """
        await self._cross_copy()

        # TODO: Replace with TaskGroup from Python 3.11
        await asyncio.gather(
            self._watch_local(),
            self._poll_remote(),
            self._sync(),
        )

    def run(self):
        """
        Synchronously cross-copies remote and local content upon start, then watches local and remote for changes, and
        reactively syncs them, resolving possible conflicts. Rclone itself has to be installed (see
        https://rclone.org/install/) and a remote set up first (see https://rclone.org/remote_setup/).
        """
        logging.basicConfig(level=logging.INFO)
        asyncio.run(self.run_async())
