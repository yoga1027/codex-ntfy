"""Share the desktop Project Manager's project and conversation assignments."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3

DEFAULT_DATABASE = (
    Path.home() / ".config" / "Code" / "User" / "globalStorage"
    / "local-tools.codex-project-manager" / "projects.sqlite"
)


class ProjectLabels:
    def __init__(self, config):
        self.names = {}
        self.roots = []
        self.assignments = {}
        self.project_roots = {}
        self.database = Path(config.get("project_manager_db", DEFAULT_DATABASE)).expanduser().absolute()
        try:
            with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                db.execute("BEGIN")
                roots = db.execute("""
                    SELECT projects.id, projects.name, roots.path
                    FROM projects JOIN roots ON roots.project_id=projects.id
                    ORDER BY projects.id, roots.rowid
                """).fetchall()
                assignments = db.execute("SELECT thread_id, project_id FROM assignments").fetchall()
        except sqlite3.Error:
            return
        self.names = {project: name for project, name, _ in roots}
        for project, _, path in roots:
            if os.path.isabs(path):
                self.project_roots.setdefault(project, []).append(path)
        self.roots = sorted(((path, project) for project, _, path in roots if os.path.isabs(path)),
                            key=lambda pair: len(pair[0]), reverse=True)
        self.assignments = dict(assignments)

    def project_id(self, thread, cwd):
        assigned = self.assignments.get(thread)
        if assigned in self.names:
            return assigned
        if isinstance(cwd, str) and os.path.isabs(cwd):
            directory = os.path.realpath(cwd)
            for root, project in self.roots:
                if os.path.commonpath((root, directory)) == os.path.normpath(root):
                    return project
        return ""

    def label(self, thread, cwd):
        return self.names.get(self.project_id(thread, cwd), "未归类")

    def assign(self, thread, project):
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=rw", uri=True, timeout=2)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            if not project:
                db.execute("DELETE FROM assignments WHERE thread_id=?", (thread,))
                return True
            if db.execute("SELECT 1 FROM roots WHERE project_id=?", (project,)).fetchone() is None:
                return False
            db.execute("""
                INSERT INTO assignments (thread_id, project_id) VALUES (?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET project_id=excluded.project_id
            """, (thread, project))
            return True

    def rename(self, project, name):
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=rw", uri=True, timeout=2)) as db, db:
            return bool(db.execute("UPDATE projects SET name=? WHERE id=?", (name, project)).rowcount)
