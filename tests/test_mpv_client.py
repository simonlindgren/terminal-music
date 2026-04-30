import json
import unittest

import lofi


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def messages(self) -> list[dict]:
        out = []
        for chunk in self.sent:
            for line in chunk.splitlines():
                if line.strip():
                    out.append(json.loads(line))
        return out


class MpvClientTests(unittest.TestCase):
    def test_load_sends_loadfile_replace(self) -> None:
        sock = FakeSocket()
        client = lofi.MpvClient(sock)
        client.load("https://hls.example/m3u8")
        self.assertEqual(
            sock.messages(),
            [{"command": ["loadfile", "https://hls.example/m3u8", "replace"]}],
        )

    def test_pause_and_resume_set_pause_property(self) -> None:
        sock = FakeSocket()
        client = lofi.MpvClient(sock)
        client.pause()
        client.resume()
        self.assertEqual(
            sock.messages(),
            [
                {"command": ["set_property", "pause", True]},
                {"command": ["set_property", "pause", False]},
            ],
        )

    def test_stop_sends_stop(self) -> None:
        sock = FakeSocket()
        client = lofi.MpvClient(sock)
        client.stop()
        self.assertEqual(
            sock.messages(),
            [{"command": ["stop"]}],
        )

    def test_messages_are_newline_terminated(self) -> None:
        sock = FakeSocket()
        client = lofi.MpvClient(sock)
        client.stop()
        self.assertTrue(sock.sent[0].endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()
