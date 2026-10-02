"""
Player accounts, kept in a plain text file the server admin edits by hand.

Each line is a name and a password separated by whitespace, for example "tomi hunter2". Lines starting with # are
comments. Names are matched case-insensitively and passwords must be letters and digits only. The file is read again on
every login, so changes apply without restarting the server.
"""
import hashlib
import logging

log = logging.getLogger("fsserver")


def md5_hex(text):
    return hashlib.md5(text.encode("latin-1")).hexdigest()


class Accounts:
    def __init__(self, path):
        self.path = path
        self._cache = None  # (modification time, accounts)

    @property
    def enabled(self):
        """
        Without an accounts file, anyone can log in with any name.
        """
        return self.path.exists()

    def load(self):
        """
        Returns {lower-case name: (name, password)}. The file is parsed again only after it changes.
        """
        mtime = self.path.stat().st_mtime_ns
        if self._cache and self._cache[0] == mtime:
            return self._cache[1]
        accounts = {}
        for number, line in enumerate(self.path.read_text(encoding="latin-1").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2 or not parts[1].isalnum() or not parts[1].isascii():
                log.warning("%s line %d: expected a name and an alphanumeric password, skipped", self.path, number)
                continue
            accounts[parts[0].lower()] = (parts[0], parts[1])
        log.info("Read %d accounts from %s", len(accounts), self.path)
        self._cache = (mtime, accounts)
        return accounts

    def find(self, name):
        """
        Returns the account's name as written in the file, or None.
        """
        account = self.load().get(name.lower())
        return account[0] if account else None

    def check_login(self, name, salt, client_hash):
        """
        Checks a login hash, MD5(salt + MD5(password)) in hex (clientCmdSaltRec in gsClient.cs). Returns the
        account's name as written in the file, or None.
        """
        account = self.load().get(name.lower())
        if account is None:
            return None
        inner = md5_hex(account[1])
        # frozen.py used upper-case hex for the inner hash against the original server. The client hashes in native
        # code, so lower case is accepted too.
        for candidate in (inner.upper(), inner):
            if md5_hex(salt + candidate).lower() == client_hash.lower():
                return account[0]
        return None
