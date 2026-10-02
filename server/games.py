"""
Matches ("multiturns", or MTs, in the game's code), their storage and the .enc files that describe them.

An .enc file starts with a 4-byte version, a 1-byte header length and a tab-separated header, followed by the encounter
itself. The header says what the file is for: an upload's purpose (MT_INIT, MT_TURN, ...) or, in a recMT.enc sent to a
client, the state of the match for that player.
"""
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("fsserver")

# Quick match mode name: (game mode, turn limit, visibility mode, client map generator). Taken from the original
# server's generate<Dark|Light><Mode>Game functions in campaignClient.cs.
QUICK_MATCH_MODES = {
    "Extermination": ("Extermination", 8, 0, "mfcGenExtermination"),
    "Dark Extermination": ("Extermination", 8, 1, "mfcGenExtermination"),
    "Secure": ("Secure2", 6, 0, "mfcGenSecure"),
    "Dark Secure": ("Secure2", 12, 2, "mfcGenSecure"),
    "Disputed": ("Disputed", 16, 0, "mfcGenDisputed"),
    "Dark Disputed": ("Disputed", 16, 1, "mfcGenDisputed"),
    "Hostage": ("HostageCamp", 8, 0, "mfcGenHostage"),
    "Dark Hostage": ("HostageCamp", 8, 2, "mfcGenHostage"),
    "Charge": ("Charge", 6, 0, "mfcGenCharge"),
    "Dark Charge": ("Charge", 6, 2, "mfcGenCharge"),
    "Upload": ("Upload", 15, 0, "mfcGenUpload"),
    "Dark Upload": ("Upload", 15, 1, "mfcGenUpload"),
}

# Game modes that bid on their first turn (their getBiddingPhase returns 1 for turn 0).
BIDDING_MODES = {"Secure", "Secure2", "SecureCamp", "Charge"}

# Where the client builds a new match before uploading it.
CLIENT_INIT_FILE = "psychoff/lanMatchInit.enc"

# Where the client merges two turns: the turn's base state and both players' submitted turns go in, the merged
# result comes out.
CLIENT_MERGE_BASE = "psychoff/lanMergeBase.enc"
CLIENT_MERGE_P1 = "psychoff/lanMergeP1.enc"
CLIENT_MERGE_P2 = "psychoff/lanMergeP2.enc"
CLIENT_MERGE_OUT = "psychoff/lanMergeOut.enc"


def tm_date(timestamp=None):
    """
    A date as the client's getPrettyDate (helper.cs) reads it: the fields of a C struct tm, years since 1900 and a
    0-based month.
    """
    t = time.localtime(timestamp)
    return "%d %d %d %d %d" % (t.tm_year - 1900, t.tm_mon - 1, t.tm_mday, t.tm_hour, t.tm_min)


def read_enc_header(data):
    """
    Returns the header fields of an .enc file.
    """
    length = data[4]
    return data[5:5 + length].decode("latin-1").split("\t")


def replace_enc_header(data, fields):
    """
    Returns a copy of an .enc file with a new header.
    """
    header = "\t".join(str(f) for f in fields).encode("latin-1")
    if len(header) > 255:
        raise ValueError("an .enc header is limited to 255 bytes, got %d" % len(header))
    return data[:4] + bytes([len(header)]) + header + data[5 + data[4]:]


def torque_string(s):
    """
    Quotes a string for use in TorqueScript source.
    """
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def create_match_script(mode_name, player1, player2, upload_path):
    """
    TorqueScript, sent with Eval, that makes a client generate a new match and upload it with an MT_INIT header.
    It does what the original server's startCampMatch did with a map from generateMFC (campaignClient.cs).
    """
    game_mode, turn_limit, vis_mode, generator = QUICK_MATCH_MODES[mode_name]
    f = torque_string(CLIENT_INIT_FILE)
    header = " TAB ".join([torque_string("MT_INIT"), torque_string(player1), torque_string(player2), '""',
                           torque_string(game_mode), str(turn_limit), "-1", "-1"])
    return " ".join([
        "setRandomSeed();",
        "$lanEnc = %s();" % generator,
        "clearMPCommands();",
        'addMPCommand("$gameMode %s");' % game_mode,
        'addMPCommand("$turnLimit %d");' % turn_limit,
        "$lanTurnLength = makeValidTurnLength(5000);",
        'addMPCommand("$turnLength " @ $lanTurnLength);',
        'addMPCommand("$turnLengthLimit -1");',
        'addMPCommand("$currentDollars 0");',
        'addMPCommand("$manager.visMode %d");' % vis_mode,
        'addMPCommand("$teamInitInfo[1] " @ $lanEnc.getNUnitsForTeam(1) SPC $lanEnc.getUnitValueForTeam(1));',
        'addMPCommand("$teamInitInfo[2] " @ $lanEnc.getNUnitsForTeam(2) SPC $lanEnc.getUnitValueForTeam(2));',
        "$lanEnc.saveEncounter(%s);" % f,
        "createInitConfFile(%s, %s, $lanTurnLength, %s);" % (f, f, header),
        "$lanEnc.dignify();",
        "$lanEnc.delete();",
        "sendFileToGS(%s, %s, 1);" % (f, torque_string(upload_path)),
    ])


def merge_turns_script(match, upload_path):
    """
    TorqueScript, sent with Eval after the CLIENT_MERGE_* files, that makes a client merge both players' turns into
    the next turn and upload it with an "MT_MERGED <mtid> <turn>" header.

    It does what hotseatSubmitTurn (hotseat.cs) does: bidding modes run their processBiddingTurn during the bidding
    phase, everything else goes through the engine's collateTurnFiles. processBiddingTurn reads the base state from
    $hotseatBaseFile when given "hotseat" as the match ID (the original server read it from its own storage), so
    that global points at the base file for the call.
    """
    p1, p2, base, out = (torque_string(f) for f in (CLIENT_MERGE_P1, CLIENT_MERGE_P2, CLIENT_MERGE_BASE,
                                                    CLIENT_MERGE_OUT))
    if match.game_mode in BIDDING_MODES and match.turn == 0:
        merge = [
            "$lanOldHotseatBase = $hotseatBaseFile;",
            "$hotseatBaseFile = %s;" % base,
            '%s.processBiddingTurn("hotseat", %s, %s, %d, %s);' % (match.game_mode, p1, p2, match.turn, out),
            "$hotseatBaseFile = $lanOldHotseatBase;",
        ]
    else:
        # The output is sent as a copy of the base state, like hotseat's $hotseatBaseFile.
        merge = ["collateTurnFiles(%s, %s, %s);" % (p1, p2, out)]
    return " ".join(merge + [
        'writeEncHeader(%s, "MT_MERGED" TAB %d TAB %d);' % (out, match.mtid, match.turn),
        "sendFileToGS(%s, %s, 1);" % (out, torque_string(upload_path)),
    ])


class Match:
    def __init__(self, mtid, player1, player2, game_mode, info, turn_limit, turn=0, submitted=None, finished=False,
                 score=0, created=None, comments=None, likes=None):
        self.mtid = mtid
        self.player1 = player1
        self.player2 = player2
        self.game_mode = game_mode
        self.info = info
        self.turn_limit = turn_limit
        self.turn = turn
        self.submitted = submitted or []  # Players who have submitted the current turn.
        self.merging = None  # The player whose client is merging the current turn. Not saved.
        self.finished = finished
        # The final score, from player 1's point of view: positive if player 1 won, negative if player 2 won.
        self.score = score
        self.created = created or tm_date()
        self.comments = comments or []  # [name, tm_date, [lines]]
        self.likes = likes or []  # Players who like this game.

    @property
    def winner(self):
        if not self.finished or self.score == 0:
            return None
        return self.player1 if self.score > 0 else self.player2

    def side_of(self, username):
        if username.lower() == self.player1.lower():
            return 1
        if username.lower() == self.player2.lower():
            return 2
        return 0

    def opponent_of(self, username):
        return self.player2 if self.side_of(username) == 1 else self.player1

    def has_submitted(self, username):
        return username.lower() in (p.lower() for p in self.submitted)

    def to_json(self):
        return dict(mtid=self.mtid, player1=self.player1, player2=self.player2, game_mode=self.game_mode,
                    info=self.info, turn_limit=self.turn_limit, turn=self.turn, submitted=self.submitted,
                    finished=self.finished, score=self.score, created=self.created, comments=self.comments,
                    likes=self.likes)


class MatchStore:
    """
    Matches, saved as data_dir/matches.json plus one folder of .enc files per match.
    """
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.index_file = data_dir / "matches.json"
        self.matches = {}
        self.next_id = 1
        if self.index_file.exists():
            saved = json.loads(self.index_file.read_text())
            self.next_id = saved["next_id"]
            for m in saved["matches"]:
                self.matches[m["mtid"]] = Match(**m)
        log.info("Loaded %d matches from %s", len(self.matches), data_dir)

    def save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.index_file.write_text(json.dumps(
            dict(next_id=self.next_id, matches=[m.to_json() for m in self.matches.values()]), indent=1))

    def match_dir(self, mtid):
        return self.data_dir / ("mt%d" % mtid)

    def base_file(self, mtid, turn):
        """
        The match state at the start of a turn.
        """
        return self.match_dir(mtid) / ("turn%d.enc" % turn)

    def turn_file(self, mtid, turn, side):
        """
        A player's submitted turn.
        """
        return self.match_dir(mtid) / ("turn%d_p%d.enc" % (turn, side))

    def create(self, player1, player2, game_mode, info, turn_limit, data):
        match = Match(self.next_id, player1, player2, game_mode, info, turn_limit)
        self.next_id += 1
        self.match_dir(match.mtid).mkdir(parents=True, exist_ok=True)
        self.base_file(match.mtid, 0).write_bytes(data)
        self.matches[match.mtid] = match
        self.save()
        return match

    def record(self, username):
        """
        A player's (wins, losses) over finished matches.
        """
        played = [m for m in self.for_player(username) if m.winner]
        wins = sum(1 for m in played if m.winner.lower() == username.lower())
        return wins, len(played) - wins

    def level(self, username):
        """
        A stand-in for the original server's levels, which aren't known: 1 plus the number of wins.
        """
        return 1 + self.record(username)[0]

    def head_to_head(self, player1, player2):
        """
        (player 1's wins, player 2's wins) in finished matches between the two.
        """
        winners = [m.winner.lower() for m in self.for_player(player1) if m.winner and m.side_of(player2)]
        return winners.count(player1.lower()), winners.count(player2.lower())

    def client_header(self, match, username):
        """
        The 26 header fields of a recMT.enc for this player, as read by loadMTStage2 (mtInGame.cs).
        """
        opponent = match.opponent_of(username)
        bidding = 1 if match.game_mode in BIDDING_MODES and match.turn == 0 and not match.finished else 0
        p1_record, p2_record = self.record(match.player1), self.record(match.player2)
        return [
            match.mtid, "", opponent, match.side_of(username), match.turn,
            int(match.has_submitted(username)), match.info, bidding, int(match.finished),
            0, "",  # spectating, player being spectated
            0,  # declined
            len(match.likes),
            "%d %d" % self.head_to_head(match.player1, match.player2),
            "%d %d" % p1_record, "%d %d" % p2_record,
            0, 0,  # player 1 and 2 ranks
            self.level(match.player1), self.level(match.player2),
            match.player1, match.player2,
            match.score,
            0, 0,  # timed turns, turn time
            int(match.has_submitted(opponent)),
        ]

    def get(self, mtid):
        try:
            return self.matches.get(int(mtid))
        except ValueError:
            return None

    def for_player(self, username):
        return [m for m in self.matches.values() if m.side_of(username)]
