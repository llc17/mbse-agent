from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week11.src.recovery import RecoveryIdentityError
from week11.src.store import Store, WEEK11_ROOT, canonical_json, digest_json, utc_now


def identity(machine: str = "a" * 64, boot: str = "11111111-1111-1111-1111-111111111111") -> dict[str, str]:
    return {
        "schema_version": "v10-recovery-context-1",
        "identity_scheme": "windows-machineguid-sha256+boot-environment-guid",
        "machine_digest": machine,
        "boot_identifier": boot,
    }


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="recovery-test-", dir=WEEK11_ROOT)
        self.store = Store(Path(self.temp.name) / "state.sqlite")
        self.session = "session-a"
        self.project = "project-a"
        self.proposal = "proposal-a"
        self.store.ensure_session(self.session)
        now = utc_now()
        empty_digest = digest_json({})
        confirmation = {"spec_digest": empty_digest, "displayed_digest": empty_digest}
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO projects(project_id,name,created_at,updated_at) VALUES(?,?,?,?)",
                (self.project, "Recovery", now, now),
            )
            db.execute(
                "UPDATE sessions SET current_project_id=?,updated_at=? WHERE session_id=?",
                (self.project, now, self.session),
            )
            db.execute(
                """INSERT INTO proposals(proposal_id,session_id,project_id,operation,
                baseline_version_id,project_revision,spec_digest,spec_json,display_json,
                displayed_digest,state,confirmation_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.proposal, self.session, self.project, "create", None, 0,
                 empty_digest, "{}", "{}", empty_digest, "approved",
                 canonical_json(confirmation), now, now),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create(self, *, request: str = "request-a") -> dict:
        with patch("week11.src.store.capture_recovery_context", return_value=identity()):
            task, created = self.store.create_task(self.proposal, self.session, request)
        self.assertTrue(created)
        return task

    def change_state(self, task_id: str, state: str, *, error: str = "old error") -> None:
        with self.store.transaction() as db:
            db.execute(
                """UPDATE tasks SET state=?,progress=?,worker_pid=424242,
                worker_owner='old-owner',result_json=?,error=? WHERE task_id=?""",
                (state, state, json.dumps({"old": True}), error, task_id),
            )

    def test_create_records_context_before_spawn_and_reused_request_does_not_backfill(self) -> None:
        task = self.create()
        with self.store.reading() as db:
            row = db.execute("SELECT * FROM recovery_context WHERE task_id=?", (task["task_id"],)).fetchone()
        self.assertEqual(identity()["boot_identifier"], row["boot_identifier"])
        with patch("week11.src.store.capture_recovery_context", side_effect=AssertionError("must not capture")):
            reused, created = self.store.create_task(self.proposal, self.session, "request-a")
        self.assertFalse(created)
        self.assertEqual(task["task_id"], reused["task_id"])

    def test_new_task_is_refused_if_identity_cannot_be_recorded(self) -> None:
        with patch("week11.src.store.capture_recovery_context", side_effect=RecoveryIdentityError("denied")):
            with self.assertRaisesRegex(RecoveryIdentityError, "denied"):
                self.store.create_task(self.proposal, self.session, "request-a")
        with self.store.reading() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])

    def test_different_boot_recovers_uncertain_and_preserves_task_payload(self) -> None:
        task = self.create()
        self.change_state(task["task_id"], "uncertain")
        now = utc_now()
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("v000099", self.project, 99, "fixture-run", "c" * 64, None,
                 "create", "passed", "ready", "fixture/version", "d" * 64, now),
            )
            db.execute(
                "UPDATE projects SET revision=7,current_version_id='v000099',selected_version_id='v000099' WHERE project_id=?",
                (self.project,),
            )
        before = self.store.task(task["task_id"])
        before_project = self.store.project(self.project)
        before_versions = self.store.versions(self.project)
        before_proposal = self.store.proposal(self.proposal)
        current = identity(boot="22222222-2222-2222-2222-222222222222")
        with patch("week11.src.store.capture_recovery_context", return_value=current):
            inspection = self.store.inspect_task_recovery(task["task_id"])
            self.assertEqual(
                {"task_id", "project_id", "status", "eligible", "reason", "digest",
                 "stored_context_digest", "observed_context_digest"},
                set(inspection),
            )
            self.assertTrue(inspection["eligible"])
            recovered = self.store.resolve_after_reboot(task["task_id"], self.session, inspection["digest"])
        self.assertEqual("interrupted", recovered["state"])
        for field in ("version_id", "result", "error", "worker_pid", "worker_owner"):
            self.assertEqual(before[field], recovered[field])
        self.assertEqual(before_project, self.store.project(self.project))
        self.assertEqual(before_versions, self.store.versions(self.project))
        self.assertEqual(before_proposal, self.store.proposal(self.proposal))
        with self.store.reading() as db:
            audit = db.execute("SELECT * FROM recovery_audit WHERE task_id=?", (task["task_id"],)).fetchall()
        self.assertEqual(1, len(audit))
        with self.assertRaisesRegex(Exception, "recovery audit is immutable"):
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE recovery_audit SET prior_state='forged' WHERE task_id=?",
                    (task["task_id"],),
                )
        with patch("week11.src.store.capture_recovery_context", return_value=current):
            self.assertFalse(self.store.inspect_task_recovery(task["task_id"])["eligible"])
            with self.assertRaisesRegex(ValueError, "not recoverable"):
                self.store.resolve_after_reboot(task["task_id"], self.session, inspection["digest"])

    def test_running_with_stale_owner_is_eligible_after_reboot(self) -> None:
        task = self.create()
        self.change_state(task["task_id"], "running")
        current = identity(boot="33333333-3333-3333-3333-333333333333")
        with patch("week11.src.store.capture_recovery_context", return_value=current):
            inspection = self.store.inspect_task_recovery(task["task_id"])
            recovered = self.store.resolve_after_reboot(task["task_id"], self.session, inspection["digest"])
        self.assertTrue(inspection["eligible"])
        self.assertEqual("old-owner", recovered["worker_owner"])

    def test_missing_record_same_boot_other_machine_and_terminal_are_refused(self) -> None:
        task = self.create()
        self.change_state(task["task_id"], "uncertain")
        with patch("week11.src.store.capture_recovery_context", return_value=identity()):
            same_boot = self.store.inspect_task_recovery(task["task_id"])
        self.assertFalse(same_boot["eligible"])
        self.assertIn("current boot", same_boot["reason"])

        other_machine = identity(machine="b" * 64, boot="22222222-2222-2222-2222-222222222222")
        with patch("week11.src.store.capture_recovery_context", return_value=other_machine):
            changed_machine = self.store.inspect_task_recovery(task["task_id"])
        self.assertFalse(changed_machine["eligible"])
        self.assertIn("different machine", changed_machine["reason"])

        self.change_state(task["task_id"], "failed")
        later_boot = identity(boot="22222222-2222-2222-2222-222222222222")
        with patch("week11.src.store.capture_recovery_context", return_value=later_boot):
            terminal = self.store.inspect_task_recovery(task["task_id"])
        self.assertFalse(terminal["eligible"])
        self.assertIn("not recoverable", terminal["reason"])

        with self.store.transaction() as db:
            db.execute("DROP TRIGGER recovery_context_no_delete")
            db.execute("DELETE FROM recovery_context WHERE task_id=?", (task["task_id"],))
        with patch("week11.src.store.capture_recovery_context", return_value=identity()):
            result = self.store.inspect_task_recovery(task["task_id"])
        self.assertFalse(result["eligible"])
        self.assertIn("predates", result["reason"])

        with patch("week11.src.store.capture_recovery_context", side_effect=AssertionError("must not capture")):
            # Existing requests remain idempotent and cannot repair the deleted record.
            _, created = self.store.create_task(self.proposal, self.session, "request-a")
        self.assertFalse(created)

    def test_cross_session_project_and_changed_digest_are_refused(self) -> None:
        task = self.create()
        self.change_state(task["task_id"], "cancel_requested")
        current = identity(boot="44444444-4444-4444-4444-444444444444")
        with patch("week11.src.store.capture_recovery_context", return_value=current):
            inspection = self.store.inspect_task_recovery(task["task_id"])
            with self.assertRaisesRegex(ValueError, "another session"):
                self.store.resolve_after_reboot(task["task_id"], "session-b", inspection["digest"])
            self.store.create_project(self.session, "Other")
            with self.assertRaisesRegex(ValueError, "not selected"):
                self.store.resolve_after_reboot(task["task_id"], self.session, inspection["digest"])
            self.store.open_project(self.session, self.project)
            with self.assertRaisesRegex(ValueError, "inspection changed"):
                self.store.resolve_after_reboot(task["task_id"], self.session, "f" * 64)
        self.assertEqual("cancel_requested", self.store.task(task["task_id"])["state"])


if __name__ == "__main__":
    unittest.main()
