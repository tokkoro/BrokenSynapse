#!/usr/bin/env python3
"""
Minimal Frozen Synapse Grand Server for LAN play.

Stage 1: accepts any login and logs all traffic both ways, so the protocol can be studied
against a real client. Messages are tab-separated lines ending in "\n"; file bodies follow a
"writeFile" line as raw bytes.
"""
import argparse
import asyncio
import itertools
import logging
import secrets
import time
import zlib
from pathlib import Path

log = logging.getLogger("fsserver")


def decompress(data):
    """
    Decompresses a file body sent with the "gz" flag. Returns the data and the format that worked.
    """
    for name, wbits in (("raw deflate", -zlib.MAX_WBITS), ("zlib", zlib.MAX_WBITS), ("gzip", 16 + zlib.MAX_WBITS)):
        try:
            return zlib.decompress(data, wbits), name
        except zlib.error:
            pass
    return None, None


class ClientSession:
    def __init__(self, server, reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        host, port = writer.get_extra_info("peername")[:2]
        self.peer = "%s:%d" % (host, port)
        self.username = None
        self.session_id = None

    async def run(self):
        log.info("%s connected", self.peer)
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    break
                text = line.decode("latin-1").rstrip("\r\n")
                # The client's ping string already ends in "\n" and sendQ adds another, so every ping is
                # followed by an empty line.
                is_ping = text == "" or text.lower().startswith("textcom\tcommand\tping\t")
                log.log(logging.DEBUG if is_ping else logging.INFO, "%s C>S %r", self.peer, text)
                await self.handle_line(text)
                await self.writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError) as e:
            log.info("%s connection lost: %r", self.peer, e)
        finally:
            log.info("%s disconnected (%s)", self.peer, self.username or "not logged in")
            self.server.sessions.discard(self)
            self.writer.close()

    def send_line(self, *fields):
        text = "\t".join(str(f) for f in fields)
        log.info("%s S>C %r", self.peer, text)
        self.writer.write((text + "\n").encode("latin-1"))

    def send_command(self, name, *args):
        """
        Calls clientCmd<name>(args...) on the client.
        """
        self.send_line("textcom", "command", name, *args)

    async def handle_line(self, text):
        fields = text.split("\t")
        kind = fields[0].lower()  # TorqueScript compares strings case-insensitively
        if kind == "textcom":
            self.handle_textcom(fields[1:])
        elif kind == "writefile":
            await self.receive_file(fields)
        elif kind == "filefinished":
            pass  # The client confirms a file we sent. Nothing is sent yet.
        elif text:
            log.warning("%s unknown message type %r", self.peer, fields[0])

    def handle_textcom(self, fields):
        kind = fields[0].lower() if fields else ""
        if kind == "prelogon":
            self.send_command("SaltRec", secrets.token_hex(8), "")
        elif kind == "login":
            username = fields[1]
            version = fields[3] if len(fields) > 3 else "?"
            log.info("%s login as %r, client version %s (any password accepted)", self.peer, username, version)
            self.log_in(username)
        elif kind == "newaccount":
            log.info("%s new account %r (accepted)", self.peer, fields[1])
            self.log_in(fields[1])
        elif kind == "servercheck":
            # The ADVANCED dialog's connection test.
            self.send_command("ServerRespond", len([s for s in self.server.sessions if s.username]))
        elif kind == "lostpassword":
            self.send_command("Info", "Password recovery is not supported on this server.")
        elif kind == "command" and len(fields) > 1:
            self.handle_command(fields[1], list(fields[2:]))
        else:
            log.warning("%s unhandled textcom %r", self.peer, fields)

    def handle_command(self, name, args):
        # commandToServer() always pads to 15 argument slots.
        while args and args[-1] == "":
            args.pop()
        if name.lower() == "ping":
            # The client pings every 8 seconds and expects no reply: a Ping command from the server makes it
            # ping again immediately.
            pass
        else:
            log.info("%s unhandled command %s %r", self.peer, name, args)

    def log_in(self, username):
        self.username = username
        self.session_id = next(self.server.session_ids)
        self.send_line("textcom", "loggedIn", self.session_id, username)
        if self.server.cancel_kick:
            # The client schedules kick() on its own connection 60 seconds after connecting, and nothing
            # in the client cancels it. Cancel it from here.
            self.send_command("Eval", "cancel($serverCon.kickSched);")

    async def receive_file(self, fields):
        """
        Reads a file body sent after a writeFile line, saves it and confirms it with fileFinished.
        """
        path = fields[1]
        size = int(fields[2])
        compressed = len(fields) > 3 and fields[3].lower() == "gz"
        body = await self.reader.readexactly(size)
        log.info("%s received %s: %d bytes%s", self.peer, path, size, ", compressed" if compressed else "")

        stem = "%s_%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), self.username or self.peer.replace(":", "_"),
                             path.replace("/", "_"))
        self.server.upload_dir.mkdir(parents=True, exist_ok=True)
        (self.server.upload_dir / (stem + ".raw")).write_bytes(body)
        data = body
        if compressed:
            data, fmt = decompress(body)
            if data is None:
                log.warning("%s could not decompress %s", self.peer, path)
            else:
                expected = fields[4] if len(fields) > 4 else "?"
                log.info("%s decompressed %s as %s: %d bytes (header says %s)", self.peer, path, fmt, len(data),
                         expected)
                (self.server.upload_dir / stem).write_bytes(data)
        self.send_line("fileFinished", path)

        if path.lower().endswith(".steamreg") and data is not None:
            # Account creation on Steam builds: username TAB password TAB email TAB Steam ticket.
            username = data.decode("latin-1").split("\t")[0].strip()
            log.info("%s new Steam account %r (accepted)", self.peer, username)
            self.log_in(username)


class GrandServer:
    def __init__(self, upload_dir, cancel_kick):
        self.upload_dir = upload_dir
        self.cancel_kick = cancel_kick
        self.sessions = set()
        self.session_ids = itertools.count(1000)

    async def on_connect(self, reader, writer):
        session = ClientSession(self, reader, writer)
        self.sessions.add(session)
        await session.run()


async def main():
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Minimal Frozen Synapse Grand Server.")
    parser.add_argument("--host", default="0.0.0.0", help="Address to listen on (default: all interfaces).")
    parser.add_argument("--port", type=int, default=28021, help="Port to listen on (default: 28021).")
    parser.add_argument("--log-dir", type=Path, default=here / "logs", help="Where session logs are written.")
    parser.add_argument("--upload-dir", type=Path, default=here / "uploads", help="Where received files are saved.")
    parser.add_argument("--no-cancel-kick", action="store_true",
                        help="Don't cancel the client's 60 second kick timer after login.")
    parser.add_argument("--verbose", action="store_true", help="Also log the client's keepalive pings.")
    args = parser.parse_args()

    args.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = args.log_dir / ("fsserver-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_file, encoding="utf-8")])

    server = GrandServer(args.upload_dir, not args.no_cancel_kick)
    listener = await asyncio.start_server(server.on_connect, args.host, args.port)
    log.info("Listening on %s:%d, logging to %s", args.host, args.port, log_file)
    async with listener:
        await listener.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
