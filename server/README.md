# Frozen Synapse LAN server

A replacement for the game's offline "Grand Server", so Frozen Synapse can be played on a LAN.

**Current stage:** accepts any username and password, fills the lobby (news, online players, an empty friends list,
no active games, a short feed) and logs all traffic both ways. There is no gameplay yet.

`fsserver.py` handles connections and messages; `lobby.py` builds the lobby's text files.

## Running

Requires Python 3.8 or later, with no dependencies.

```
python3 server/fsserver.py
```

Options: `--host` and `--port` (default `0.0.0.0:28021`), `--log-dir` (default `server/logs`) and `--upload-dir`
(default `server/uploads`). Each run writes a new log file.

## Connecting the game

1. On the login screen, click **ADVANCED**.
2. Enter the server's address (`127.0.0.1` on the same machine) and port `28021`.
3. Use the test button to check the connection, then confirm the server.
4. Log in with any username and password, or create an account. Both are accepted.

The game remembers the address but not the choice to use it, so step 3 is needed on every launch.
