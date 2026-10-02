"""
Builds the text files the client's lobby (the Grand Server browser) asks for.

Each function returns the file's lines, in the format read by the client handler named in its docstring. Paths are where
the client expects each file; it picks the handler by exact path (clientFileReceived in gsClient.cs).
"""

HOME_SCREEN_PATH = "psychoff/homeSc.txt"
ONLINE_PLAYERS_PATH = "psychoff/rankings.txt"
FRIENDS_LIST_PATH = "psychoff/flist.txt"
ACTIVE_GAMES_PATH = "psychoff/activeGames.txt"
FEED_PATH = "psychoff/feed.txt"


def home_screen(headline, text):
    """
    onHomeRec (feedClient.cs): the news headline, the news text until "motd end", the game of the week
    (id TAB player 1 TAB player 2, or empty for a random game), the number of ranking leaders, and the
    leaders as fields in groups separated by "-".
    """
    return [headline] + text.split("\n") + ["motd end", "", "0", "-"]


def online_players(players):
    """
    setSidebarText (gsBrowserGuiScript.cs): the number of players online, then one line per player:
    name TAB level TAB wins TAB losses. A level of "NO" hides the player's stats.
    """
    return [str(len(players))] + ["%s\t%s\t%s\t%s" % (p.username, p.level, p.wins, p.losses) for p in players]


def friends_list():
    """
    onFriendsListRec (friendsClient.cs): a header line the client ignores, then one line per player:
    name TAB is a friend of yours TAB is your friend TAB level TAB wins against them TAB losses against them.
    Friends aren't supported yet, so the list is empty.
    """
    return ["0"]


def active_games(games):
    """
    onActiveGamesRec (activeGames.cs): a header line the client ignores, then one line per game:
    id TAB opponent TAB game mode TAB info TAB hours TAB needs your turn (0 or 1) TAB turn number.
    """
    return [str(len(games))] + ["\t".join(str(field) for field in game) for game in games]


def feed(text):
    """
    onFeedRec (feedClient.cs): markup text, shown under the client's own summary of games waiting for a turn.
    """
    return text.split("\n")


def encode(lines):
    return "\n".join(lines).encode("latin-1")
