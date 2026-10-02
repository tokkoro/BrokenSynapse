# Frozen Synapse LAN server

Frozen Synapse's official online server ("Grand Server") has shut down, which took the multiplayer with it. This is a
small replacement server you can run yourself, so you and your friends can play multiplayer matches again over a LAN
or the internet.

It works with the unmodified game: no patches, no mods. One person runs the server, and everyone points their game at
it from the login screen.

## What works

- Logging in with accounts you set up
- The online lobby: news, who's online, your active games
- Challenging another player from **Create Game**, in Extermination, Secure, Disputed, Hostage and Charge, Light or Dark
- Playing a match turn by turn, with fog of war, until it ends
- Win/loss records and levels, game pages, comments, likes, in-game chat with your opponent, deleting a game

Not available: timed turns, tournaments, the Dark campaign, rankings, the friends list, one-turn ("endplay") games,
co-op, and activating the Red DLC. Extermination is the most tested mode, and chat, comments, likes and deleting games
are new; please report problems with any of them.

## Running the server

You need [Python](https://www.python.org/downloads/) 3.8 or later. Nothing else needs installing.

1. **Create the accounts.** Copy `accounts.example.txt` to `accounts.txt` in the same folder, and write one player
   per line: a name and a password, separated by a space.

   ```
   tomi hunter2
   joel Secret99
   ```

   Passwords may only contain letters and digits, and names are not case-sensitive. Give each friend their name and
   password. You can edit the file while the server runs; changes apply at the next login.

2. **Start the server** from the folder that contains `fsserver.py`:

   ```
   python3 fsserver.py
   ```

   On Windows, use `py fsserver.py`. It prints `Listening on 0.0.0.0:28021` and keeps running until you press
   Ctrl+C. Matches are saved in the `data` folder, so you can stop and restart it at any time.

3. **Let your friends reach it** (see below).

Without an `accounts.txt`, the server lets anyone log in with any name and password. That's handy for a quick test on
your own machine, but always create the file before others can connect.

## Letting friends connect

The server listens on TCP port **28021**. Each player needs an address and port that reach your machine:

| Where the players are | Address to use | What you need to do |
| --- | --- | --- |
| Same computer as the server | `127.0.0.1` | Nothing |
| Same home network (LAN) | Your computer's LAN IP, e.g. `192.168.1.20` | Allow port 28021/TCP in your firewall |
| Anywhere, via [Tailscale](https://tailscale.com) (recommended) | Your Tailscale IP, e.g. `100.101.102.103` | Everyone installs Tailscale and joins your network |
| Anywhere, via [ngrok](https://ngrok.com) | The address ngrok shows, e.g. `0.tcp.eu.ngrok.io` and its port | Run `ngrok tcp 28021` next to the server |

Tailscale is the simplest safe option for a group of friends: the address never changes and the server isn't exposed
to the internet. With ngrok the address changes every time ngrok restarts (on the free plan), and anyone who finds it
can try to connect, so keep `accounts.txt` in place.

## Connecting the game (every player)

1. Start Frozen Synapse and go to the online login screen.
2. Click **ADVANCED**.
3. Enter the server address and port from the table above.
4. Use the test button: it should say the server responded. Then confirm the server.
5. Log in with the name and password the server admin gave you.

The game remembers the address, but you need to repeat steps 2 to 4 (confirming the server) every time you start the
game. Creating an account from the game doesn't work; ask the admin to add you to `accounts.txt`.

## Playing

- Click **Create Game**, type your opponent's name exactly as it appears in `accounts.txt` (case doesn't matter), pick
  a game mode, and confirm. Your game generates the map, which can freeze it for a moment, and then opens the match.
- Your opponent gets a challenge popup if they're online, or finds the match in their active games next time.
- Plan your turn and submit it. When both players have submitted, the second player's game works out the result and
  sends it to the server. Both players then see the outcome and plan the next turn.
- You don't need to be online at the same time: play your turn whenever you like.

If a turn doesn't advance even though both players submitted, the second player probably quit the game right after
submitting. The server finishes the turn automatically the next time either player logs in.

## Troubleshooting

- **"Could not contact server" or the test button times out:** check that the server is running, that the address
  and port are right, and that the firewall allows port 28021.
- **"Login Failed: Wrong username or password":** check the name and password in `accounts.txt` on the server.
- **"There is no player called …":** the opponent's name must be in `accounts.txt`.
- **Something gets stuck on "Contacting server":** check the server's log for warnings.

Logs that help with bug reports:

- The server writes a new log file for every run in the `logs` folder.
- The game writes `console.log`: on Linux in `~/.local/share/FrozenSynapse/`.

## Good to know

- Passwords never travel over the network; the game sends a salted hash instead. The connection itself isn't
  encrypted, though, so don't reuse a password you use anywhere else.
- To keep things safe, the server only accepts uploads from logged-in players, refuses files over 400,000 bytes, and
  only lets players create matches they play in.
- The server uses a feature of the game that lets it run short scripts on the players' computers: it asks the game to
  generate maps and to work out turn results with the game's own code. It sends nothing else, but only connect to
  servers run by people you trust.
- Matches live in the server's `data` folder. Back it up if you care about your history.
