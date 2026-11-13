#!/usr/bin/env python3
"""A stand-in for OpenCode in a tmux pane, for the end-to-end tests.

mates reads two things from a Qwen pane: its tmux screen (the input box, its border, the
"esc interrupt" spinner) and OpenCode's sqlite database (sessions, messages, parts). This
draws the screen in OpenCode's layout and writes rows in OpenCode's shape, so real `mates`
commands run against it. What it does with a message is scripted by markers in the text:

  FAKE:done      edit a.txt to "b", write .mates/done.md
  FAKE:question  write .mates/question.md
  FAKE:compact   write a compaction row (a finished reply mates must not read as done) first
  FAKE:hang      keep the spinner up and never reply
  (nothing)      reply and stop, writing no protocol file
  /new           start a new chat (session) with the next message

Typed keys show in the input box; a bracketed paste then Enter submits it.
"""
import json
import os
import re
import select
import sqlite3
import sys
import termios
import time
import tty
import uuid
from pathlib import Path

WORK_S = float(os.environ.get("FAKE_OPENCODE_WORK_S", "1.0"))
DB = Path(os.environ["FAKE_OPENCODE_DB"])
SCHEMA = """
CREATE TABLE IF NOT EXISTS session (id text PRIMARY KEY, project_id text NOT NULL, parent_id text,
    directory text NOT NULL, title text NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL);
CREATE TABLE IF NOT EXISTS message (id text PRIMARY KEY, session_id text NOT NULL,
    time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
CREATE TABLE IF NOT EXISTS part (id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
    time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
"""


def now_ms():
    return int(time.time() * 1000)


class Fake:
    def __init__(self):
        DB.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(DB)
        self.con.executescript(SCHEMA)
        self.cwd = os.getcwd()
        self.sid = None
        self.lines = []  # the chat
        self.typed = ""
        self.working = False

    # --- the database, in OpenCode's shape
    def _row(self, role, text, **extra):
        t = now_ms()
        if self.sid is None:
            self.sid = "ses_" + uuid.uuid4().hex[:24]
            self.con.execute("INSERT INTO session VALUES (?, 'p', NULL, ?, 't', ?, ?)", (self.sid, self.cwd, t, t))
        data = {"role": role, "time": {"created": t}, **extra}
        if role == "assistant":
            data["time"]["completed"] = t
            data.setdefault("finish", "stop")
            data.setdefault("tokens", {"input": 10, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}})
        mid = uuid.uuid4().hex
        self.con.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", (mid, self.sid, t, t, json.dumps(data)))
        self.con.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                         (uuid.uuid4().hex, mid, self.sid, t, t, json.dumps({"type": "text", "text": text})))
        self.con.execute("UPDATE session SET time_updated = ? WHERE id = ?", (t, self.sid))
        self.con.commit()

    # --- the screen, in OpenCode's layout
    def draw(self):
        w = os.get_terminal_size().columns
        rows = os.get_terminal_size().lines
        box = min(w - 4, 70)
        typed = self.typed.replace("\n", " ")[: box - 4]
        fresh = self.sid is None and not typed  # a fresh chat shows the placeholder
        input_row = '  ┃  Ask anything… "Fix a TODO in the codebase"' if fresh else "  ┃  " + typed
        footer = "   ⬝⬝⬝■■■■■  esc interrupt" if self.working else f"   {self.cwd[-40:]}   ctrl+p commands"
        bottom = ["  ┃", input_row, "  ┃", "  ┃  Build · fake-model local", "  ╹" + "▀" * box, footer]
        chat = self.lines[-(rows - len(bottom) - 1):]
        screen = chat + [""] * (rows - len(bottom) - len(chat)) + bottom
        sys.stdout.write("\x1b[H\x1b[2J" + "\r\n".join(s[:w] for s in screen))
        sys.stdout.flush()

    def submit(self, text):
        text = text.strip()
        if text == "/new":
            self.sid, self.lines = None, []
            self.draw()
            return
        self._row("user", text)
        self.lines += ["", "  ┃  " + text.splitlines()[0][:60], ""]
        self.working = True
        self.draw()
        if "FAKE:hang" in text:
            return
        if "FAKE:compact" in text:
            self._row("assistant", "summary", mode="compaction", agent="compaction", summary=True)
        time.sleep(WORK_S)
        mates = Path(self.cwd) / ".mates"
        if "FAKE:done" in text:
            Path(self.cwd, "a.txt").write_text("b\n")
            mates.mkdir(exist_ok=True)
            (mates / "done.md").write_text("a.txt: b\ntest status: passed\n")
        elif "FAKE:question" in text:
            mates.mkdir(exist_ok=True)
            (mates / "question.md").write_text("Which value?\n")
        self._row("assistant", "Done.")
        self.lines += ["     Done.", "", "     ▣  Build · fake-model · 1s"]
        self.working = False
        self.draw()


def main():
    fake = Fake()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        fake.draw()
        buf = b""
        pasting = False
        while True:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                continue
            data = os.read(fd, 65536)
            if not data:
                return
            buf += data
            while buf:
                if pasting:
                    end = buf.find(b"\x1b[201~")
                    if end < 0:
                        fake.typed += buf.decode("utf-8", "replace")
                        buf = b""
                        break
                    fake.typed += buf[:end].decode("utf-8", "replace")
                    buf, pasting = buf[end + 6:], False
                elif buf.startswith(b"\x1b[200~"):
                    buf, pasting = buf[6:], True
                elif buf[:1] in (b"\r", b"\n"):
                    buf = buf[1:]
                    text, fake.typed = fake.typed, ""
                    if text.strip():
                        fake.submit(text)
                elif buf[:1] == b"\x7f":
                    buf, fake.typed = buf[1:], fake.typed[:-1]
                elif buf[:1] == b"\x03":  # Ctrl-C quits, as OpenCode does on a second press
                    return
                elif buf[:1] == b"\x1b":
                    m = re.match(rb"\x1b\[[0-9;]*[A-Za-z~]?", buf)
                    buf = buf[len(m.group(0)) if m else 1:]
                else:
                    fake.typed += buf[:1].decode("utf-8", "replace")
                    buf = buf[1:]
            fake.draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    main()
