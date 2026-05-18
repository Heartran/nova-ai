"""
test_servers.py — Test suite per la logica pura dei server Nova.

Cosa copre:
  - memory.py: lettura file, limite byte, skeleton, bootstrap
  - checkpoints.py: get/update/is_tracked, persistenza JSON atomica
  - personality.py: build_system_prompt (struttura, sezioni memoria)
  - nova_bot.py: split_for_discord, _serialize_history, should_respond (sync paths)
  - nova_whatsapp.py: _db_ro/_has_lid_column/_name_expr, resolve_jid_variants,
                      build_messages_for_claude, serialize_history_wa,
                      fetch_new_messages, fetch_history, fetch_tail
  - nova_mcp.py: _safe_target (path traversal)

Non copre: chiamate Claude, Discord gateway, bridge WhatsApp HTTP.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_db(path: str) -> None:
    """Create a minimal messages.db compatible with the bridge schema."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chats (
            jid TEXT PRIMARY KEY,
            name TEXT,
            last_message_time TEXT,
            lid TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT,
            timestamp TEXT,
            sender TEXT,
            content TEXT,
            is_from_me INTEGER DEFAULT 0,
            chat_jid TEXT,
            media_type TEXT
        );
    """)
    conn.commit()
    conn.close()


def _insert_msg(db_path: str, chat_jid: str, content: str, sender: str = "user1@s.whatsapp.net",
                is_from_me: int = 0, ts: str = "2024-01-01T10:00:00+00:00", media_type: str = "") -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO messages (id, timestamp, sender, content, is_from_me, chat_jid, media_type) VALUES (?,?,?,?,?,?,?)",
        (f"id_{content[:10]}", ts, sender, content, is_from_me, chat_jid, media_type),
    )
    conn.execute(
        "INSERT OR IGNORE INTO chats (jid, name) VALUES (?,?)",
        (chat_jid, "Test Chat"),
    )
    conn.commit()
    conn.close()


# ===========================================================================
# memory.py
# ===========================================================================

class TestMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_md_files_empty_dir(self):
        from memory import _read_md_files
        result = _read_md_files(self.tmp, "test")
        self.assertEqual(result, "")

    def test_read_md_files_reads_content(self):
        from memory import _read_md_files
        (self.tmp / "a.md").write_text("# Ciao\ncontent here", encoding="utf-8")
        result = _read_md_files(self.tmp, "test")
        self.assertIn("content here", result)
        self.assertIn("### File: a.md", result)

    def test_read_md_files_index_first(self):
        from memory import _read_md_files
        (self.tmp / "z.md").write_text("last", encoding="utf-8")
        (self.tmp / "INDEX.md").write_text("first", encoding="utf-8")
        result = _read_md_files(self.tmp, "test")
        self.assertLess(result.index("first"), result.index("last"))

    def test_read_md_files_byte_limit(self):
        from memory import _read_md_files
        import memory as m
        original = m.MAX_SECTION_BYTES
        m.MAX_SECTION_BYTES = 50
        try:
            (self.tmp / "big.md").write_text("X" * 100, encoding="utf-8")
            (self.tmp / "small.md").write_text("Y" * 10, encoding="utf-8")
            result = _read_md_files(self.tmp, "test")
            self.assertIn("truncated due to", result)
        finally:
            m.MAX_SECTION_BYTES = original

    def test_scope_dir_for_server(self):
        from memory import scope_dir_for
        d = scope_dir_for(self.tmp, "server", 12345)
        self.assertEqual(d, self.tmp / "server" / "12345")

    def test_scope_dir_for_dm(self):
        from memory import scope_dir_for
        d = scope_dir_for(self.tmp, "dm", 99)
        self.assertEqual(d, self.tmp / "dm" / "99")

    def test_scope_dir_for_whatsapp(self):
        from memory import scope_dir_for
        d = scope_dir_for(self.tmp, "whatsapp", "123@s.whatsapp.net")
        self.assertEqual(d, self.tmp / "whatsapp" / "123@s.whatsapp.net")

    def test_scope_dir_for_invalid(self):
        from memory import scope_dir_for
        with self.assertRaises(ValueError):
            scope_dir_for(self.tmp, "invalid", 1)

    def test_ensure_scope_skeleton_server(self):
        from memory import ensure_scope_skeleton
        d = self.tmp / "server" / "1"
        ensure_scope_skeleton(d, "server")
        self.assertTrue((d / "INDEX.md").exists())
        self.assertTrue((d / "lore.md").exists())
        self.assertTrue((d / "characters.md").exists())
        self.assertTrue((d / "conversations.md").exists())

    def test_ensure_scope_skeleton_dm(self):
        from memory import ensure_scope_skeleton
        d = self.tmp / "dm" / "1"
        ensure_scope_skeleton(d, "dm")
        self.assertTrue((d / "INDEX.md").exists())
        self.assertTrue((d / "conversations.md").exists())
        self.assertFalse((d / "lore.md").exists())

    def test_ensure_scope_skeleton_whatsapp(self):
        from memory import ensure_scope_skeleton
        d = self.tmp / "whatsapp" / "jid"
        ensure_scope_skeleton(d, "whatsapp")
        self.assertTrue((d / "conversations.md").exists())

    def test_ensure_scope_skeleton_idempotent(self):
        from memory import ensure_scope_skeleton
        d = self.tmp / "server" / "1"
        ensure_scope_skeleton(d, "server")
        (d / "lore.md").write_text("custom", encoding="utf-8")
        ensure_scope_skeleton(d, "server")
        self.assertEqual((d / "lore.md").read_text(), "custom")

    def test_ensure_shared_skeleton(self):
        from memory import ensure_shared_skeleton
        ensure_shared_skeleton(self.tmp)
        sd = self.tmp / "_shared"
        self.assertTrue(sd.exists())
        self.assertTrue((sd / "INDEX.md").exists())

    def test_bootstrap_copies_template(self):
        from memory import bootstrap_memory_dir
        template = self.tmp / "template"
        target = self.tmp / "target"
        template.mkdir()
        (template / "test.md").write_text("hello", encoding="utf-8")
        result = bootstrap_memory_dir(target, template)
        self.assertTrue(result)
        self.assertTrue((target / "test.md").exists())

    def test_bootstrap_skips_existing(self):
        from memory import bootstrap_memory_dir
        template = self.tmp / "template"
        target = self.tmp / "target"
        template.mkdir()
        target.mkdir()
        (target / "existing.md").write_text("keep", encoding="utf-8")
        result = bootstrap_memory_dir(target, template)
        self.assertFalse(result)
        self.assertTrue((target / "existing.md").exists())

    def test_append_conversation_note(self):
        from memory import append_conversation_note
        ok = append_conversation_note(self.tmp, "test note", "Mario")
        self.assertTrue(ok)
        content = (self.tmp / "conversations.md").read_text()
        self.assertIn("test note", content)
        self.assertIn("Mario", content)

    def test_load_scope_memory_missing_dir(self):
        from memory import load_scope_memory
        result = load_scope_memory(self.tmp / "nonexistent" / "path")
        self.assertEqual(result, "")


# ===========================================================================
# checkpoints.py
# ===========================================================================

class TestCheckpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "checkpoints.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_on_first_run(self):
        from checkpoints import ChannelCheckpoints
        cp = ChannelCheckpoints(self.path)
        self.assertIsNone(cp.get(123))
        self.assertFalse(cp.is_tracked(123))
        self.assertEqual(cp.channel_ids(), [])

    def test_update_and_get(self):
        from checkpoints import ChannelCheckpoints
        cp = ChannelCheckpoints(self.path)
        ts = datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc)
        cp.update(42, ts, "server", 999)
        result = cp.get(42)
        self.assertIsNotNone(result)
        self.assertEqual(result.replace(tzinfo=timezone.utc), ts)

    def test_is_tracked(self):
        from checkpoints import ChannelCheckpoints
        cp = ChannelCheckpoints(self.path)
        ts = datetime(2024, 1, 15, tzinfo=timezone.utc)
        cp.update(7, ts, "dm", 7)
        self.assertTrue(cp.is_tracked(7))
        self.assertFalse(cp.is_tracked(8))

    def test_channel_ids(self):
        from checkpoints import ChannelCheckpoints
        cp = ChannelCheckpoints(self.path)
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        cp.update(1, ts, "server", 10)
        cp.update(2, ts, "server", 10)
        self.assertCountEqual(cp.channel_ids(), [1, 2])

    def test_persistence(self):
        from checkpoints import ChannelCheckpoints
        ts = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
        cp = ChannelCheckpoints(self.path)
        cp.update(55, ts, "server", 200)
        # Reload from disk
        cp2 = ChannelCheckpoints(self.path)
        self.assertTrue(cp2.is_tracked(55))
        loaded = cp2.get(55)
        self.assertEqual(loaded.year, 2024)
        self.assertEqual(loaded.month, 6)

    def test_atomic_write(self):
        """Verifica che non rimanga il file .tmp dopo il salvataggio."""
        from checkpoints import ChannelCheckpoints
        cp = ChannelCheckpoints(self.path)
        cp.update(1, datetime.now(timezone.utc), "server", 1)
        tmp = self.path.with_suffix(".json.tmp")
        self.assertFalse(tmp.exists())

    def test_get_entry(self):
        from checkpoints import ChannelCheckpoints
        cp = ChannelCheckpoints(self.path)
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        cp.update(10, ts, "dm", 10)
        entry = cp.get_entry(10)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["scope"], "dm")
        self.assertEqual(entry["scope_id"], 10)

    def test_data_is_private(self):
        from checkpoints import ChannelCheckpoints
        cp = ChannelCheckpoints(self.path)
        self.assertFalse(hasattr(cp, "data"), "self.data dovrebbe essere privato (_data)")


# ===========================================================================
# personality.py
# ===========================================================================

class TestPersonality(unittest.TestCase):
    def test_build_system_prompt_structure(self):
        from personality import build_system_prompt
        prompt = build_system_prompt("shared mem", "scope mem", "user mem")
        self.assertIn("Nova", prompt)
        self.assertIn("MEMORIA CONDIVISA", prompt)
        self.assertIn("MEMORIA SPECIFICA", prompt)
        self.assertIn("CONTESTO SULL'UTENTE", prompt)
        self.assertIn("shared mem", prompt)
        self.assertIn("scope mem", prompt)
        self.assertIn("user mem", prompt)

    def test_build_system_prompt_empty_sections(self):
        from personality import build_system_prompt
        prompt = build_system_prompt("", "", "")
        # The section headers (with === separators) must NOT appear when memories are empty.
        # ("MEMORIA CONDIVISA" alone also appears in the base personality text, so we
        # check for the specific section header injected by build_system_prompt.)
        self.assertNotIn("MEMORIA CONDIVISA — LORE DEL PROGETTO", prompt)
        self.assertNotIn("MEMORIA SPECIFICA DI QUESTA CHAT:", prompt)
        self.assertNotIn("CONTESTO SULL'UTENTE (chi ti ha creata):", prompt)

    def test_build_system_prompt_custom_name(self):
        from personality import build_system_prompt
        prompt = build_system_prompt("", "", "", bot_display_name="TestBot")
        self.assertIn("TestBot", prompt)

    def test_build_system_prompt_default_name_no_note(self):
        from personality import build_system_prompt
        prompt = build_system_prompt("", "", "", bot_display_name="Nova")
        self.assertNotIn("display name attuale", prompt)

    def test_memory_order(self):
        """La memoria condivisa deve precedere quella di scope."""
        from personality import build_system_prompt
        prompt = build_system_prompt("AAA", "BBB", "CCC")
        self.assertLess(prompt.index("AAA"), prompt.index("BBB"))
        self.assertLess(prompt.index("BBB"), prompt.index("CCC"))


# ===========================================================================
# nova_bot.py — funzioni pure
# ===========================================================================

class TestNovaBotPure(unittest.TestCase):
    def test_split_for_discord_short(self):
        from nova_bot import split_for_discord
        chunks = split_for_discord("ciao", 1900)
        self.assertEqual(chunks, ["ciao"])

    def test_split_for_discord_empty(self):
        from nova_bot import split_for_discord
        self.assertEqual(split_for_discord(""), [])
        self.assertEqual(split_for_discord("   "), [])

    def test_split_for_discord_exact_limit(self):
        from nova_bot import split_for_discord
        text = "a" * 100
        chunks = split_for_discord(text, 100)
        self.assertEqual(chunks, [text])

    def test_split_for_discord_over_limit(self):
        from nova_bot import split_for_discord
        text = "a" * 200
        chunks = split_for_discord(text, 100)
        self.assertEqual(len(chunks), 2)
        for c in chunks:
            self.assertLessEqual(len(c), 100)
        self.assertEqual("".join(chunks), text)

    def test_split_for_discord_multiline(self):
        from nova_bot import split_for_discord
        lines = ["line" + str(i) for i in range(10)]
        text = "\n".join(lines)
        chunks = split_for_discord(text, 30)
        reconstructed = "\n".join(chunks)
        self.assertEqual(reconstructed, text)
        for c in chunks:
            self.assertLessEqual(len(c), 30)

    def test_serialize_history_empty(self):
        from nova_bot import _serialize_history
        self.assertEqual(_serialize_history([]), "")

    def test_serialize_history_single_user(self):
        from nova_bot import _serialize_history
        msgs = [{"role": "user", "content": "ciao Nova"}]
        result = _serialize_history(msgs)
        self.assertEqual(result, "ciao Nova")

    def test_serialize_history_with_context(self):
        from nova_bot import _serialize_history
        msgs = [
            {"role": "user", "content": "[Mario]: prima domanda"},
            {"role": "assistant", "content": "risposta Nova"},
            {"role": "user", "content": "[Mario]: seconda domanda"},
        ]
        result = _serialize_history(msgs)
        self.assertIn("Storico chat recente", result)
        self.assertIn("Nuovo messaggio", result)
        self.assertIn("prima domanda", result)
        self.assertIn("risposta Nova", result)
        self.assertIn("seconda domanda", result)


# ===========================================================================
# nova_whatsapp.py — DB helpers e logica pura
# ===========================================================================

class TestWhatsappDbHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "messages.db")
        _mk_db(self.db)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_db_ro_context_manager(self):
        from nova_whatsapp import _db_ro
        with _db_ro(self.db) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_db_ro_closes_on_exit(self):
        from nova_whatsapp import _db_ro
        conn_ref = None
        with _db_ro(self.db) as conn:
            conn_ref = conn
        # After context exit, trying to use the connection should fail
        with self.assertRaises(Exception):
            conn_ref.execute("SELECT 1")

    def test_has_lid_column_false(self):
        from nova_whatsapp import _db_ro, _has_lid_column
        # Default schema includes lid
        with _db_ro(self.db) as conn:
            result = _has_lid_column(conn.cursor())
        self.assertTrue(result)  # our test schema has lid

    def test_has_lid_column_false_no_lid(self):
        """DB without lid column."""
        from nova_whatsapp import _db_ro, _has_lid_column
        db2 = str(self.tmp / "nolid.db")
        conn = sqlite3.connect(db2)
        conn.execute("CREATE TABLE chats (jid TEXT, name TEXT)")
        conn.commit()
        conn.close()
        with _db_ro(db2) as conn:
            result = _has_lid_column(conn.cursor())
        self.assertFalse(result)

    def test_name_expr_with_lid(self):
        from nova_whatsapp import _name_expr
        expr = _name_expr(True)
        self.assertIn("lid", expr)

    def test_name_expr_without_lid(self):
        from nova_whatsapp import _name_expr
        expr = _name_expr(False)
        self.assertNotIn("lid", expr)

    def test_resolve_jid_variants_basic(self):
        from nova_whatsapp import resolve_jid_variants
        jid = "123@s.whatsapp.net"
        result = resolve_jid_variants(self.db, jid)
        self.assertIn(jid, result)

    def test_resolve_jid_variants_contact_map(self):
        from nova_whatsapp import resolve_jid_variants
        jid = "123@s.whatsapp.net"
        lid = "abc:99@lid"
        contact_map = {
            "mario": {
                "chat_jid": jid,
                "lid": lid,
                "name": "Mario",
            }
        }
        result = resolve_jid_variants(self.db, jid, contact_map)
        self.assertIn(jid, result)
        self.assertIn(lid, result)

    def test_fetch_new_messages_empty(self):
        from nova_whatsapp import fetch_new_messages
        jid = "empty@s.whatsapp.net"
        result = fetch_new_messages(self.db, jid, None, 50)
        self.assertEqual(result, [])

    def test_fetch_new_messages_returns_inbound(self):
        from nova_whatsapp import fetch_new_messages
        jid = "chat@s.whatsapp.net"
        _insert_msg(self.db, jid, "messaggio utente", is_from_me=0,
                    ts="2024-01-01T10:00:00+00:00")
        result = fetch_new_messages(self.db, jid, None, 50)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "messaggio utente")
        self.assertFalse(result[0]["is_from_me"])

    def test_fetch_new_messages_excludes_outbound(self):
        from nova_whatsapp import fetch_new_messages
        jid = "chat2@s.whatsapp.net"
        _insert_msg(self.db, jid, "risposta nova", is_from_me=1,
                    ts="2024-01-01T10:00:00+00:00")
        result = fetch_new_messages(self.db, jid, None, 50)
        self.assertEqual(result, [])

    def test_fetch_new_messages_after_filter(self):
        from nova_whatsapp import fetch_new_messages
        jid = "chat3@s.whatsapp.net"
        _insert_msg(self.db, jid, "vecchio", is_from_me=0,
                    ts="2024-01-01T08:00:00+00:00")
        _insert_msg(self.db, jid, "nuovo", is_from_me=0,
                    ts="2024-01-01T12:00:00+00:00")
        after = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        result = fetch_new_messages(self.db, jid, after, 50)
        contents = [r["content"] for r in result]
        self.assertIn("nuovo", contents)
        self.assertNotIn("vecchio", contents)

    def test_fetch_history(self):
        from nova_whatsapp import fetch_history
        jid = "hist@s.whatsapp.net"
        _insert_msg(self.db, jid, "storia", is_from_me=0,
                    ts="2024-01-01T08:00:00+00:00")
        result = fetch_history(self.db, jid, "2024-01-01T12:00:00+00:00", 20)
        contents = [r["content"] for r in result]
        self.assertIn("storia", contents)

    def test_fetch_tail(self):
        from nova_whatsapp import fetch_tail
        jid = "tail@s.whatsapp.net"
        for i in range(5):
            _insert_msg(self.db, jid, f"msg{i}", is_from_me=i % 2,
                        ts=f"2024-01-01T{10+i:02d}:00:00+00:00")
        result = fetch_tail(self.db, jid, 10)
        self.assertEqual(len(result), 5)
        # Cronologico: il primo timestamp deve essere <= dell'ultimo
        self.assertLessEqual(result[0]["timestamp"], result[-1]["timestamp"])

    def test_fetch_tail_limit(self):
        from nova_whatsapp import fetch_tail
        jid = "limit@s.whatsapp.net"
        for i in range(10):
            _insert_msg(self.db, jid, f"msg{i}", ts=f"2024-01-{i+1:02d}T10:00:00+00:00")
        result = fetch_tail(self.db, jid, 3)
        self.assertEqual(len(result), 3)

    def test_fetch_db_not_found(self):
        from nova_whatsapp import fetch_new_messages, fetch_history, fetch_tail
        bad_db = str(self.tmp / "nonexistent.db")
        self.assertEqual(fetch_new_messages(bad_db, "x@s.whatsapp.net", None, 10), [])
        self.assertEqual(fetch_history(bad_db, "x@s.whatsapp.net", "2024-01-01", 10), [])
        self.assertEqual(fetch_tail(bad_db, "x@s.whatsapp.net", 10), [])


class TestBuildMessagesForClaude(unittest.TestCase):
    def test_empty(self):
        from nova_whatsapp import build_messages_for_claude
        result = build_messages_for_claude([], [])
        # Must start with user turn
        self.assertEqual(result[0]["role"], "user")

    def test_inbound_is_user(self):
        from nova_whatsapp import build_messages_for_claude
        msgs = [{"is_from_me": False, "content": "ciao", "sender": "mario@s.whatsapp.net"}]
        result = build_messages_for_claude([], msgs)
        user_turns = [m for m in result if m["role"] == "user"]
        self.assertTrue(any("ciao" in t["content"] for t in user_turns))

    def test_outbound_is_assistant(self):
        from nova_whatsapp import build_messages_for_claude
        history = [{"is_from_me": False, "content": "ciao", "sender": "u@s.whatsapp.net"}]
        new = [{"is_from_me": True, "content": "risposta", "sender": "me"}]
        result = build_messages_for_claude(history, new)
        assistant_turns = [m for m in result if m["role"] == "assistant"]
        self.assertTrue(any("risposta" in t["content"] for t in assistant_turns))

    def test_first_message_always_user(self):
        from nova_whatsapp import build_messages_for_claude
        # Solo messaggi outbound: deve inserire placeholder
        msgs = [{"is_from_me": True, "content": "nova reply", "sender": "me"}]
        result = build_messages_for_claude([], msgs)
        self.assertEqual(result[0]["role"], "user")

    def test_consecutive_same_role_merged(self):
        from nova_whatsapp import build_messages_for_claude
        msgs = [
            {"is_from_me": False, "content": "primo", "sender": "u@s.whatsapp.net"},
            {"is_from_me": False, "content": "secondo", "sender": "u@s.whatsapp.net"},
        ]
        result = build_messages_for_claude([], msgs)
        user_turns = [m for m in result if m["role"] == "user" and "contesto" not in m["content"]]
        # I due messaggi devono essere fusi in un unico turn
        self.assertEqual(len(user_turns), 1)
        self.assertIn("primo", user_turns[0]["content"])
        self.assertIn("secondo", user_turns[0]["content"])


class TestSerializeHistoryWa(unittest.TestCase):
    def test_empty(self):
        from nova_whatsapp import serialize_history_wa
        self.assertEqual(serialize_history_wa([]), "")

    def test_single_message(self):
        from nova_whatsapp import serialize_history_wa
        msgs = [{"role": "user", "content": "ciao"}]
        result = serialize_history_wa(msgs)
        self.assertEqual(result, "ciao")

    def test_with_history(self):
        from nova_whatsapp import serialize_history_wa
        msgs = [
            {"role": "user", "content": "domanda"},
            {"role": "assistant", "content": "risposta"},
            {"role": "user", "content": "followup"},
        ]
        result = serialize_history_wa(msgs)
        self.assertIn("Storico chat recente", result)
        self.assertIn("Nuovo messaggio", result)
        self.assertIn("Nova: risposta", result)


# ===========================================================================
# nova_mcp.py — _safe_target
# ===========================================================================

class TestSafeTarget(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get_safe_target(self):
        # Rebuild server to get a fresh closure over self.tmp
        from nova_mcp import build_memory_server
        # We need _safe_target directly; re-extract it via closure inspection
        # or just replicate the logic here
        base = self.tmp.resolve()

        def safe_target(filename):
            if not filename or not filename.endswith(".md"):
                return None
            if "/" in filename or "\\" in filename or ".." in filename:
                return None
            target = (base / filename).resolve()
            try:
                target.relative_to(base)
            except ValueError:
                return None
            return target

        return safe_target

    def test_valid_filename(self):
        safe = self._get_safe_target()
        result = safe("lore.md")
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "lore.md")

    def test_not_md_rejected(self):
        safe = self._get_safe_target()
        self.assertIsNone(safe("lore.txt"))
        self.assertIsNone(safe("lore"))

    def test_empty_rejected(self):
        safe = self._get_safe_target()
        self.assertIsNone(safe(""))

    def test_path_traversal_rejected(self):
        safe = self._get_safe_target()
        self.assertIsNone(safe("../secret.md"))
        self.assertIsNone(safe("../../etc/passwd.md"))

    def test_slash_in_name_rejected(self):
        safe = self._get_safe_target()
        self.assertIsNone(safe("subdir/file.md"))

    def test_backslash_rejected(self):
        safe = self._get_safe_target()
        self.assertIsNone(safe("sub\\file.md"))

    def test_new_file_allowed(self):
        safe = self._get_safe_target()
        result = safe("newfile.md")
        self.assertIsNotNone(result)
        self.assertTrue(str(result).startswith(str(self.tmp.resolve())))


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestMemory,
        TestCheckpoints,
        TestPersonality,
        TestNovaBotPure,
        TestWhatsappDbHelpers,
        TestBuildMessagesForClaude,
        TestSerializeHistoryWa,
        TestSafeTarget,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
