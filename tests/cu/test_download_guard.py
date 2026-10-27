"""截图下载归属校验: 只允许任务归属用户; 归属无法证明的历史文件拒绝但不删除。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import config
import webchat_api


class FakeRequest:
    def __init__(self, auth=""):
        self.headers = {"Authorization": auth} if auth else {}


class FakeRedis:
    def __init__(self, tokens, owners):
        self.tokens, self.owners = tokens, owners

    async def get(self, key):
        return self.tokens.get(key)

    async def hget(self, key, field):
        return self.owners.get(field)


class DownloadGuardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.evidence = self.folder / "computer_use" / "cu_test_1.jpg"
        self.evidence.parent.mkdir()
        self.evidence.write_bytes(b"\xff\xd8\xff\xd9")
        self.legacy = self.folder / "computer_use" / "legacy_shot.jpg"
        self.legacy.write_bytes(b"\xff\xd8\xff\xd9")
        self.csv = self.folder / "report.csv"
        self.csv.write_text("a,b\n", encoding="utf-8")
        self.redis = FakeRedis(
            {config.key_token("t1"): "alice", config.key_token("t2"): "bob"},
            {"computer_use/cu_test_1.jpg": "alice"})
        self.patch = patch.multiple(webchat_api, get_redis=self.redis_get,
                                    _DOWNLOADS_DIR=self.folder)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def redis_get(self):
        return self.redis

    async def call(self, filename, token="", header=False):
        return await webchat_api.download_file(
            filename, FakeRequest(f"Bearer {token}" if header else ""), "" if header else token)

    async def test_owner_only_and_unproven_history_denied_but_kept(self):
        response = await self.call("computer_use/cu_test_1.jpg", "t1")
        self.assertEqual(Path(response.path), self.evidence)
        with self.assertRaises(HTTPException) as denied:
            await self.call("computer_use/cu_test_1.jpg", "t2")
        self.assertEqual(denied.exception.status_code, 403)
        # 历史文件没有归属记录: 拒绝访问, 但不能删除历史数据。
        with self.assertRaises(HTTPException) as orphan:
            await self.call("computer_use/legacy_shot.jpg", "t1")
        self.assertEqual(orphan.exception.status_code, 403)
        self.assertTrue(self.legacy.exists())

    async def test_auth_header_and_query_token_are_equivalent(self):
        response = await self.call("computer_use/cu_test_1.jpg", "t1", header=True)
        self.assertEqual(Path(response.path), self.evidence)

    async def test_invalid_names_and_missing_or_invalid_tokens(self):
        for name in ("../secret.jpg", "computer_use/../secret.jpg", "x.txt", "computer_use/a"):
            with self.assertRaises(HTTPException) as invalid:
                await self.call(name, "t1")
            self.assertEqual(invalid.exception.status_code, 400, name)
        with self.assertRaises(HTTPException) as anonymous:
            await self.call("computer_use/cu_test_1.jpg")
        self.assertEqual(anonymous.exception.status_code, 401)
        with self.assertRaises(HTTPException) as expired:
            await self.call("computer_use/cu_test_1.jpg", "stale")
        self.assertEqual(expired.exception.status_code, 401)

    async def test_non_evidence_downloads_keep_legacy_behaviour(self):
        response = await self.call("report.csv", "t1")
        self.assertEqual(Path(response.path), self.csv)
        self.assertEqual(response.media_type, "text/csv")

    async def test_owner_lookup_failure_fails_closed(self):
        async def broken(key, field):
            raise RuntimeError("redis down")

        self.redis.hget = broken
        with self.assertRaises(HTTPException) as failed:
            await self.call("computer_use/cu_test_1.jpg", "t1")
        self.assertEqual(failed.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
