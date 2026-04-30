import subprocess
import unittest
from types import SimpleNamespace

import lofi


class ResolveStreamUrlTests(unittest.TestCase):
    def test_returns_first_line_of_stdout(self) -> None:
        captured = {}

        def fake_runner(cmd, **kw):
            captured["cmd"] = cmd
            captured["kw"] = kw
            return SimpleNamespace(
                returncode=0,
                stdout="https://hls.example/m3u8\n",
                stderr="",
            )

        url = lofi.resolve_stream_url(
            "https://youtube.com/watch?v=abc",
            runner=fake_runner,
        )
        self.assertEqual(url, "https://hls.example/m3u8")
        self.assertEqual(
            captured["cmd"],
            [
                "yt-dlp",
                "-g",
                "--no-warnings",
                "https://youtube.com/watch?v=abc",
            ],
        )

    def test_nonzero_exit_raises_resolver_error(self) -> None:
        def fake_runner(cmd, **kw):
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="ERROR: video unavailable",
            )

        with self.assertRaises(lofi.ResolverError):
            lofi.resolve_stream_url(
                "https://youtube.com/watch?v=bad",
                runner=fake_runner,
            )

    def test_empty_stdout_raises_resolver_error(self) -> None:
        def fake_runner(cmd, **kw):
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with self.assertRaises(lofi.ResolverError):
            lofi.resolve_stream_url(
                "https://youtube.com/watch?v=empty",
                runner=fake_runner,
            )


if __name__ == "__main__":
    unittest.main()
