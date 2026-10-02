#!/usr/bin/env python3
"""
Minimal Frozen Synapse Grand Server for LAN play.

Accepts any login, answers the lobby's requests and logs all traffic both ways, so the protocol can
be studied against a real client. Messages are tab-separated lines ending in "\n"; file bodies follow
a "writeFile" line as raw bytes.
"""
import argparse
import asyncio
import collections
import itertools
import logging
import secrets
import time
import zlib
from pathlib import Path

import accounts
import games
import lobby

log = logging.getLogger("fsserver")

MOTD_HEADLINE = "Welcome to the LAN server"
MOTD_TEXT = "This server replaces the offline Grand Server.\nCreate a game against any player name to play."
FEED_TEXT = "\nWelcome to the LAN server."


def compress(data):
    """
    Compresses a file body the way the original server did: raw deflate, without a zlib header.
    """
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


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
        self.salt = ""
        # Like the client, send nothing else while a file waits for its fileFinished.
        self.outgoing = collections.deque()
        self.file_in_flight = None
        self.merge_after_submit = False

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
            for match in self.server.store.matches.values():
                if self.username and match.merging == self.username:
                    log.warning("Match %d: %s disconnected while merging, will retry", match.mtid, self.username)
                    match.merging = None
            self.writer.close()

    # Shown in the online players list (lobby.online_players).
    @property
    def level(self):
        return self.server.store.level(self.username)

    @property
    def wins(self):
        return self.server.store.record(self.username)[0]

    @property
    def losses(self):
        return self.server.store.record(self.username)[1]

    def send_line(self, *fields):
        self.outgoing.append(("line", "\t".join(str(f) for f in fields)))
        self.flush()

    def send_command(self, name, *args):
        """
        Calls clientCmd<name>(args...) on the client.
        """
        self.send_line("textcom", "command", name, *args)

    def send_file(self, path, data):
        """
        Sends a file the client saves at path. Files are always compressed, like the original server's.
        """
        self.outgoing.append(("file", path, data))
        self.flush()

    def send_lobby_file(self, path, lines):
        self.send_file(path, lobby.encode(lines))

    def send_active_games(self):
        rows = []
        for m in self.server.store.for_player(self.username):
            if m.finished:
                continue
            needs_turn = int(not m.has_submitted(self.username))
            rows.append((m.mtid, m.opponent_of(self.username), m.game_mode, m.info, 0, needs_turn, m.turn))
        self.send_lobby_file(lobby.ACTIVE_GAMES_PATH, lobby.active_games(rows))

    def flush(self):
        while self.outgoing and self.file_in_flight is None:
            item = self.outgoing.popleft()
            if item[0] == "line":
                log.info("%s S>C %r", self.peer, item[1])
                self.writer.write((item[1] + "\n").encode("latin-1"))
            else:
                path, data = item[1], item[2]
                body = compress(data)
                header = "writeFile\t%s\t%d\tgz\t%d" % (path, len(body), len(data))
                log.info("%s S>C %r (%r)", self.peer, header, data[:200].decode("latin-1"))
                self.writer.write((header + "\n").encode("latin-1") + body)
                self.file_in_flight = path

    async def handle_line(self, text):
        fields = text.split("\t")
        kind = fields[0].lower()  # TorqueScript compares strings case-insensitively
        if kind == "textcom":
            self.handle_textcom(fields[1:])
        elif kind == "writefile":
            await self.receive_file(fields)
        elif kind == "filefinished":
            path = fields[1] if len(fields) > 1 else ""
            if path != self.file_in_flight:
                log.warning("%s fileFinished for %r, but %r was in flight", self.peer, path, self.file_in_flight)
            self.file_in_flight = None
            self.flush()
        elif text:
            log.warning("%s unknown message type %r", self.peer, fields[0])

    def handle_textcom(self, fields):
        kind = fields[0].lower() if fields else ""
        if kind == "prelogon":
            self.salt = secrets.token_hex(8)
            self.send_command("SaltRec", self.salt, "")
        elif kind == "login":
            # login TAB name TAB MD5(salt + MD5(password)) TAB client version
            username, client_hash, version = (fields + ["", "", ""])[1:4]
            if self.server.accounts.enabled:
                name = self.server.accounts.check_login(username, self.salt, client_hash)
                if name is None:
                    log.info("%s login as %r refused: wrong name or password", self.peer, username)
                    self.send_command("LoginFailed", "Wrong username or password.")
                    return
                username = name
                log.info("%s login as %r, client version %s", self.peer, username, version)
            else:
                log.info("%s login as %r, client version %s (no accounts file, any password accepted)", self.peer,
                         username, version)
            self.log_in(username)
        elif kind == "newaccount":
            self.create_account(fields[1])
        elif kind == "servercheck":
            # The ADVANCED dialog's connection test.
            self.send_command("ServerRespond", len(self.server.players()))
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
        name = name.lower()
        if name in ("ping", "setmyos", "setclientingamestatus", "setdarkstatus", "selectsteamsessionid",
                    "setawaystatus"):
            # The client pings every 8 seconds and expects no reply: a Ping command from the server makes
            # it ping again immediately. The status updates need no reply either.
            pass
        elif name == "requesthomescreen":
            self.send_lobby_file(lobby.HOME_SCREEN_PATH, lobby.home_screen(MOTD_HEADLINE, MOTD_TEXT))
        elif name == "refreshpeopleonline":
            self.send_lobby_file(lobby.ONLINE_PLAYERS_PATH, lobby.online_players(self.server.players()))
        elif name == "requestfriends":
            self.send_lobby_file(lobby.FRIENDS_LIST_PATH, lobby.friends_list())
        elif name == "requestfeed":
            self.send_lobby_file(lobby.FEED_PATH, lobby.feed(FEED_TEXT))
        elif name == "playquickmatch":
            self.play_quick_match(*(args + ["", ""])[:2])
        elif name == "selectmt":
            self.select_match(args[0] if args else "")
        elif name == "mtended":
            self.end_match(*(args + ["", ""])[:2])
        elif name == "requestgamepagenew":
            self.send_game_page(args[0] if args else "")
        elif name == "getcommentsforgame":
            match = self.server.store.get(args[0] if args else "")
            self.send_lobby_file(lobby.COMMENTS_PATH, lobby.comments(match.mtid if match else 0,
                                                                     match.comments if match else []))
        elif name == "rategame":
            self.rate_game(*(args + ["", ""])[:2])
        elif name == "getlevelfor":
            player = args[0] if args else self.username
            self.send_command("levelFor", player, self.server.store.level(player))
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
        # The original server pushed these after login without being asked. It also sent HasDLCStatus, but
        # that isn't sent here: the client stores the paid DLC as a local flag that HasDLCStatus 1 switches on
        # (and quits for a restart), and the original server only did that after checking the purchase with
        # Steam. Without it, the client keeps the DLC status it already has.
        self.send_command("setMyStats", self.level)
        self.send_active_games()
        # Merge turns that are waiting, for example because the last merging client disconnected.
        for match in self.server.store.for_player(username):
            if len(match.submitted) == 2:
                self.start_merge(match, after_submit=False)

    def play_quick_match(self, opponent, mode_name):
        """
        A challenge from the Create Game dialog. The client generates the map and uploads the new match, like the
        original server's generateGameMapFromClient asked it to.
        """
        if mode_name not in games.QUICK_MATCH_MODES:
            self.send_command("Error", "This server doesn't support the game mode %s yet." % mode_name)
        elif opponent.lower() == self.username.lower():
            self.send_command("Error", "You can't challenge yourself.")
        elif '"' in opponent or "\\" in opponent:
            self.send_command("Error", "Invalid opponent name.")
        elif self.server.accounts.enabled and self.server.accounts.find(opponent) is None:
            self.send_command("Error", "There is no player called %s on this server." % opponent)
        else:
            if self.server.accounts.enabled:
                opponent = self.server.accounts.find(opponent)  # The name as written in the accounts file.
            log.info("%s quick match: %s vs %s, %s", self.peer, self.username, opponent, mode_name)
            self.send_command("Eval", games.create_match_script(mode_name, self.username, opponent, self.upload_path()))

    def select_match(self, mtid):
        """
        Sends a match to the client as psychoff/recMT.enc, with a header describing it for this player.
        """
        match = self.server.store.get(mtid)
        if match is None or not match.side_of(self.username):
            self.send_command("Error", "Game %s doesn't exist." % mtid)
            return
        data = self.server.store.base_file(match.mtid, match.turn).read_bytes()
        header = self.server.store.client_header(match, self.username)
        self.send_file("psychoff/recMT.enc", games.replace_enc_header(data, header))

    def end_match(self, mtid, score):
        """
        mtEnded TAB match ID TAB score, sent by both players' clients when a match ends. The score is from player 1's
        point of view (the game mode's getResult): positive if player 1 won, negative if player 2 won.
        """
        store = self.server.store
        match = store.get(mtid)
        if match is None or not match.side_of(self.username):
            log.warning("%s mtEnded for unknown match %r", self.peer, mtid)
            return
        try:
            score = float(score)
        except ValueError:
            log.warning("%s mtEnded with an invalid score %r", self.peer, score)
            return
        if match.finished:
            if score != match.score:
                log.warning("Match %d: %s reported score %s, but %s was reported first", match.mtid, self.username,
                            score, match.score)
            return
        match.finished = True
        match.score = score
        store.save()
        log.info("Match %d finished with score %s: %s", match.mtid, score,
                 "%s won" % match.winner if match.winner else "a draw")

    def send_game_page(self, mtid):
        """
        The game page in the browser (clientCmdGamePageInfoRec in gamePageClient.cs).
        """
        match = self.server.store.get(mtid)
        if match is None:
            self.send_command("gamePageNotExist", mtid)
            return
        self.send_command("GamePageInfoRec", match.mtid, match.player1, match.player2, match.created,
                          int(match.finished), match.score, "", match.game_mode, len(match.likes), match.turn, "", "",
                          "")

    def rate_game(self, mtid, rating):
        """
        The Like button: a rating above 0 likes the game, anything else unlikes it.
        """
        match = self.server.store.get(mtid)
        if match is None:
            self.send_command("gamePageNotExist", mtid)
            return
        likes = [p for p in match.likes if p.lower() != self.username.lower()]
        liked = rating not in ("", "0") and not rating.startswith("-")
        match.likes = likes + [self.username] if liked else likes
        self.server.store.save()
        self.send_command("GameRated", 1 if liked else 0)

    def start_merge(self, match, after_submit):
        """
        Asks this client to merge both players' turns into the next turn (see games.merge_turns_script). The
        result comes back as an MT_MERGED upload.
        """
        if match.merging:
            return
        store = self.server.store
        log.info("%s merging turn %d of match %d", self.peer, match.turn, match.mtid)
        match.merging = self.username
        self.merge_after_submit = after_submit
        base = store.base_file(match.mtid, match.turn).read_bytes()
        self.send_file(games.CLIENT_MERGE_BASE, base)
        self.send_file(games.CLIENT_MERGE_OUT, base)
        self.send_file(games.CLIENT_MERGE_P1, store.turn_file(match.mtid, match.turn, 1).read_bytes())
        self.send_file(games.CLIENT_MERGE_P2, store.turn_file(match.mtid, match.turn, 2).read_bytes())
        self.send_command("Eval", games.merge_turns_script(match, self.upload_path()))

    def upload_path(self):
        """
        Where the client uploads games and turns, named after the session ID like the original client did.
        """
        return "psychoff/grandServer/rec/rec%s.enc" % self.session_id

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

        if data is not None and path.lower().startswith("psychoff/grandserver/rec/") and path.lower().endswith(".enc"):
            self.handle_upload(data)
        elif data is not None and path.lower().endswith(".cmt"):
            self.add_comment(data)
        elif path.lower().endswith(".steamreg") and data is not None:
            # Account creation on Steam builds: username TAB password TAB email TAB Steam ticket.
            self.create_account(data.decode("latin-1").split("\t")[0].strip())

    def add_comment(self, data):
        """
        A game page comment (addGameComment in gamePageClient.cs): the match ID, then the comment's lines.
        """
        lines = data.decode("latin-1").replace("\r", "").split("\n")
        while lines and lines[-1] == "":
            lines.pop()
        match = self.server.store.get(lines[0].strip()) if lines else None
        if match is None or len(lines) < 2:
            log.warning("%s comment for unknown match or empty: %r", self.peer, lines[:2])
            return
        match.comments.append([self.username, games.tm_date(), lines[1:]])
        self.server.store.save()
        log.info("%s commented on match %d", self.peer, match.mtid)

    def create_account(self, username):
        """
        The game's Create Account dialog. Without an accounts file any name is accepted; with one, the admin
        adds accounts to it instead.
        """
        if self.server.accounts.enabled:
            log.info("%s new account %r refused: accounts are managed in %s", self.peer, username,
                     self.server.accounts.path)
            self.send_command("Error", "Accounts can't be created from the game on this server. "
                                       "Ask the server admin to add one for you.")
        else:
            log.info("%s new account %r (no accounts file, accepted)", self.peer, username)
            self.log_in(username)


    def handle_upload(self, data):
        """
        Handles an uploaded .enc file by its header: a new match (MT_INIT) or a submitted turn (MT_TURN).
        """
        header = games.read_enc_header(data)
        version = int.from_bytes(data[:4], "little")
        log.info("%s upload header (version %d): %r", self.peer, version, header)
        kind = header[0]
        store = self.server.store
        if kind == "MT_INIT":
            # MT_INIT TAB player 1 TAB player 2 TAB info TAB game mode TAB turn limit TAB ...
            player1, player2, info, game_mode, turn_limit = (header + [""] * 6)[1:6]
            match = store.create(player1, player2, game_mode, info, turn_limit, data)
            log.info("%s created match %d: %s vs %s, %s", self.peer, match.mtid, player1, player2, game_mode)
            self.send_command("MTAccepted", match.mtid)
            opponent = self.server.session_for(match.opponent_of(self.username))
            if opponent:
                opponent.send_command("NotifyNewGame", match.mtid, self.username, game_mode, 0, info)
        elif kind == "MT_TURN":
            match = store.get(header[1])
            if match is None or not match.side_of(self.username):
                log.warning("%s turn for unknown match %r", self.peer, header[1])
                self.send_command("Error", "Game %s doesn't exist." % header[1])
                return
            side = match.side_of(self.username)
            store.turn_file(match.mtid, match.turn, side).write_bytes(data)
            if not match.has_submitted(self.username):
                match.submitted.append(self.username)
            store.save()
            log.info("%s turn %d of match %d submitted by %s", self.peer, match.turn, match.mtid, self.username)
            self.send_command("TurnFiled", match.mtid)
            opponent = self.server.session_for(match.opponent_of(self.username))
            if opponent:
                opponent.send_command("OpponentCommitTurn", match.mtid, 0)
            if len(match.submitted) == 2:
                self.start_merge(match, after_submit=True)
        elif kind == "MT_MERGED":
            # MT_MERGED TAB match ID TAB turn: the result of start_merge.
            match = store.get(header[1])
            turn = int(header[2]) if len(header) > 2 and header[2].isdigit() else -1
            if match is None or match.turn != turn or len(match.submitted) != 2:
                log.warning("%s stale merge result for match %r turn %r, ignored", self.peer, header[1], turn)
                return
            store.base_file(match.mtid, turn + 1).write_bytes(data)
            match.turn += 1
            match.submitted = []
            match.merging = None
            store.save()
            log.info("%s match %d advanced to turn %d", self.peer, match.mtid, match.turn)
            for username in (match.player1, match.player2):
                session = self.server.session_for(username)
                if session is None:
                    continue
                if session is self and self.merge_after_submit:
                    # The second player to submit sees the result straight away.
                    self.send_command("TurnFiledAndAdvanced", match.mtid)
                else:
                    session.send_command("NotifyTurn", match.mtid, match.opponent_of(username))
        else:
            log.warning("%s unhandled upload type %r", self.peer, kind)


class GrandServer:
    def __init__(self, upload_dir, data_dir, accounts_file, cancel_kick):
        self.upload_dir = upload_dir
        self.accounts = accounts.Accounts(accounts_file)
        self.store = games.MatchStore(data_dir)
        self.cancel_kick = cancel_kick
        self.sessions = set()
        self.session_ids = itertools.count(1000)

    def players(self):
        """
        Sessions that have logged in.
        """
        return sorted((s for s in self.sessions if s.username), key=lambda s: s.username.lower())

    def session_for(self, username):
        for s in self.sessions:
            if s.username and s.username.lower() == username.lower():
                return s
        return None

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
    parser.add_argument("--data-dir", type=Path, default=here / "data", help="Where matches are stored.")
    parser.add_argument("--accounts", type=Path, default=here / "accounts.txt",
                        help="Accounts file, one 'name password' per line. Without it, anyone can log in.")
    parser.add_argument("--no-cancel-kick", action="store_true",
                        help="Don't cancel the client's 60 second kick timer after login.")
    parser.add_argument("--verbose", action="store_true", help="Also log the client's keepalive pings.")
    args = parser.parse_args()

    args.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = args.log_dir / ("fsserver-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_file, encoding="utf-8")])

    server = GrandServer(args.upload_dir, args.data_dir, args.accounts, not args.no_cancel_kick)
    if server.accounts.enabled:
        log.info("Accounts are read from %s", args.accounts)
    else:
        log.warning("No accounts file at %s: anyone can log in with any name and password", args.accounts)
    listener = await asyncio.start_server(server.on_connect, args.host, args.port)
    log.info("Listening on %s:%d, logging to %s", args.host, args.port, log_file)
    async with listener:
        await listener.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
