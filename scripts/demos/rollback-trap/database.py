"""The migrations' side of orders-service's database (demo 20): pg8000 from a
package, and the table schema_migrations, which lists what a database has run.

    db = database.Database(database.connect(database.driver(folder), url))
    db.applied()                      {file: sha256}
    db.apply(name, sql, sha256, build)

The driver is the one the package vendors (build.py installs requirements.txt
into it), so the tools host needs none of its own.
"""

import hashlib
import importlib
import ssl
import sys
import time
import urllib.parse

TABLE = """CREATE TABLE IF NOT EXISTS schema_migrations (
  file text PRIMARY KEY,
  sha256 text NOT NULL,
  build text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
)"""


def driver(folder):
    """pg8000.native from an unpacked package (or any folder that holds it)."""
    folder = str(folder)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    return importlib.import_module("pg8000.native")


def connect(native, url):
    parts = urllib.parse.urlsplit(url)
    return native.Connection(user=urllib.parse.unquote(parts.username or ""),
                             password=urllib.parse.unquote(parts.password or ""), host=parts.hostname,
                             port=parts.port or 5432, database=parts.path.lstrip("/"),
                             ssl_context=ssl.create_default_context(), timeout=30)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def message(error):
    """Postgres's own message for a failed statement, else the error."""
    details = error.args[0] if error.args else None
    return details.get("M", str(error)) if isinstance(details, dict) else str(error)


class Database:
    def __init__(self, connection):
        self.connection = connection

    def applied(self):
        """{file: sha256} of the migrations this database has run."""
        self.connection.run(TABLE)
        return {file: digest for file, digest in self.connection.run("SELECT file, sha256 FROM schema_migrations")}

    def apply(self, name, sql, digest, build):
        """One file in a transaction of its own, recorded with its SHA-256 and build."""
        self.connection.run("BEGIN")
        try:
            self.connection.run(sql)
            self.connection.run("INSERT INTO schema_migrations (file, sha256, build) VALUES (:file, :digest, :build)",
                                file=name, digest=digest, build=str(build))
            self.connection.run("COMMIT")
        except Exception:
            try:
                self.connection.run("ROLLBACK")
            except Exception:
                pass  # the connection went: Postgres rolls the transaction back itself
            raise

    def close(self):
        try:
            self.connection.close()
        except Exception:
            pass


def migrate(db, files, build, out=print, where="the database"):
    """Apply the files the database hasn't run, in name order. A file it ran
    whose SHA-256 has changed since stops everything before anything runs.
    Returns the names applied."""
    applied = db.applied()
    changed = [name for name in sorted(files) if name in applied and applied[name] != sha256(files[name])]
    if changed:
        raise ChangedMigration(f"{changed[0]} changed after {where} ran it: it recorded SHA-256 "
                               f"{applied[changed[0]][:12]}, the package has {sha256(files[changed[0]])[:12]}. "
                               "Not applying anything.")
    ran = [name for name in sorted(files) if name in applied]
    out(f"{where} has run {', '.join(ran) or 'no migration yet'}")
    todo = [name for name in sorted(files) if name not in applied]
    if not todo:
        out("Nothing to apply")
    for name in todo:
        started = time.monotonic()
        try:
            db.apply(name, files[name].decode(), sha256(files[name]), build)
        except Exception as error:
            raise MigrationFailed(f"{name} failed in {where}: {message(error)}. Nothing of it was applied.") from error
        out(f"Applied {name} in {time.monotonic() - started:.1f}s")
    return todo


class ChangedMigration(Exception):
    pass


class MigrationFailed(Exception):
    pass
