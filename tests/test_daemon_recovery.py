from __future__ import annotations

from dataclasses import replace
import os
import tempfile
import unittest
from pathlib import Path
from typing import Sequence
from unittest import mock

from ophelia.daemon.config import DaemonConfig
from ophelia.daemon.recovery import (
    apply_clean_host_restore,
    clean_host_restore_plan,
    create_host_backup,
    host_backup_plan,
    latest_backup_status,
)
from ophelia.daemon.observations import collect_host_observation
from ophelia.daemon.store import DaemonStore
from ophelia.execution import IntegrityError, SQLiteOperationJournal
from ophelia.execution.subprocesses import ProcessResult


class FakeAgeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, argv: Sequence[str], **_: object) -> ProcessResult:
        command = list(argv)
        self.commands.append(command)
        output = Path(command[command.index("--output") + 1])
        source = Path(command[-1])
        output.write_bytes(source.read_bytes())
        return ProcessResult(tuple(command), 0, "success", "", "", False, False, 1)


class HostRecoveryTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[DaemonConfig, SQLiteOperationJournal, Path]:
        runtime = root / "runtime"
        destination = root / "backups"
        destination.mkdir(mode=0o700)
        journal = SQLiteOperationJournal.beneath_runtime_root(runtime)
        config = DaemonConfig(
            host_id="host_recovery-test",
            runtime_root=runtime,
            socket_path=root / "daemon.sock",
            allowed_uids=(os.geteuid(),),
            allowed_manifest_roots=(),
            recovery_backup_roots=(destination,),
            recovery_age_recipient="age1fixture",
            recovery_max_bytes=64 * 1024 * 1024,
            recovery_freshness_seconds=3600,
        )
        store = DaemonStore(journal)
        store.register_host(
            host_id=config.host_id,
            capabilities={"backup": True},
            agent_version="fixture",
            protocol_version=1,
        )
        store.set_host_control(
            config.host_id, maintenance_mode=True, drained=True
        )
        content = runtime / "apps" / "sample" / "state.txt"
        content.parent.mkdir(mode=0o700, parents=True)
        content.write_text("durable state\n", encoding="utf-8")
        (content.parent / "current.txt").symlink_to("state.txt")
        return config, journal, destination

    def test_encrypted_backup_and_clean_host_restore_are_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config, journal, destination = self._fixture(root)
            runner = FakeAgeRunner()
            with mock.patch(
                "ophelia.daemon.recovery.shutil.which", return_value="/usr/bin/age"
            ):
                plan = host_backup_plan(
                    config,
                    journal,
                    backup_id="backup_fixture",
                    destination_root=destination,
                )
                self.assertTrue(plan["can_apply"])
                receipt = create_host_backup(
                    config,
                    journal,
                    backup_id="backup_fixture",
                    destination_root=destination,
                    confirmation=plan["confirmation_token"],
                    runner=runner,
                )
                replay_plan = host_backup_plan(
                    config,
                    journal,
                    backup_id="backup_fixture",
                    destination_root=destination,
                )
                replay = create_host_backup(
                    config,
                    journal,
                    backup_id="backup_fixture",
                    destination_root=destination,
                    confirmation=replay_plan["confirmation_token"],
                    runner=runner,
                )
                self.assertTrue(replay["replayed"])
                self.assertEqual("current", latest_backup_status(config)["status"])

                identity = root / "age-identity.txt"
                identity.write_text("AGE-SECRET-KEY-fixture\n", encoding="utf-8")
                identity.chmod(0o600)
                target = root / "restored"
                target.mkdir(mode=0o700)
                restore_plan = clean_host_restore_plan(
                    backup_root=Path(receipt["backup_root"]),
                    identity_file=identity,
                    target_runtime_root=target,
                    maximum_bytes=config.recovery_max_bytes,
                )
                restored = apply_clean_host_restore(
                    restore_plan,
                    restore_plan["confirmation_token"],
                    runner=runner,
                    owner_name=None,
                    require_root=False,
                )

            self.assertEqual("succeeded", restored["status"])
            self.assertEqual(
                "durable state\n",
                (target / "apps" / "sample" / "state.txt").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertTrue((target / "apps" / "sample" / "current.txt").is_symlink())
            self.assertFalse((target / "identity").exists())
            self.assertTrue((target / "host-state" / "clean-host-restore.json").is_file())
            self.assertEqual(2, len(runner.commands))

    def test_backup_requires_drained_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config, journal, destination = self._fixture(root)
            DaemonStore(journal).set_host_control(
                config.host_id, maintenance_mode=False, drained=False
            )
            with mock.patch(
                "ophelia.daemon.recovery.shutil.which", return_value="/usr/bin/age"
            ):
                blocked = host_backup_plan(
                    config,
                    journal,
                    backup_id="backup_blocked",
                    destination_root=destination,
                )
            self.assertFalse(blocked["can_apply"])
            self.assertIn("host_must_be_drained_and_in_maintenance", blocked["blockers"])

            inside_runtime = config.runtime_root / "backups"
            inside_runtime.mkdir(mode=0o700)
            overlapping = replace(
                config, recovery_backup_roots=(inside_runtime,)
            )
            DaemonStore(journal).set_host_control(
                config.host_id, maintenance_mode=True, drained=True
            )
            with mock.patch(
                "ophelia.daemon.recovery.shutil.which", return_value="/usr/bin/age"
            ):
                overlap_plan = host_backup_plan(
                    overlapping,
                    journal,
                    backup_id="backup_overlap",
                    destination_root=inside_runtime,
                )
            self.assertFalse(overlap_plan["can_apply"])
            self.assertIn(
                "backup_target_overlaps_runtime_root", overlap_plan["blockers"]
            )

    def test_restore_rejects_an_occupied_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config, journal, destination = self._fixture(root)
            runner = FakeAgeRunner()
            identity = root / "identity"
            identity.write_text("fixture")
            identity.chmod(0o600)
            target = root / "restored-occupied"
            target.mkdir()
            (target / "state.txt").write_text("occupied")
            with mock.patch(
                "ophelia.daemon.recovery.shutil.which", return_value="/usr/bin/age"
            ):
                backup_plan = host_backup_plan(
                    config,
                    journal,
                    backup_id="backup_occupied",
                    destination_root=destination,
                )
                receipt = create_host_backup(
                    config,
                    journal,
                    backup_id="backup_occupied",
                    destination_root=destination,
                    confirmation=backup_plan["confirmation_token"],
                    runner=runner,
                )
                restore_plan = clean_host_restore_plan(
                    backup_root=Path(receipt["backup_root"]),
                    identity_file=identity,
                    target_runtime_root=target,
                )
            self.assertFalse(restore_plan["can_apply"])
            self.assertIn(
                "target_runtime_root_must_be_absent_or_empty",
                restore_plan["blockers"],
            )

    def test_observations_are_durable_bounded_and_integrity_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config, journal, _ = self._fixture(root)
            store = DaemonStore(journal)
            for active_apps in range(3):
                observation = collect_host_observation(
                    config, journal, active_apps=active_apps
                )
                store.record_observation(
                    config.host_id, observation, retention=2
                )
            latest = store.latest_observation(config.host_id)
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(2, latest["observation"]["activity"]["active_apps"])
            connection = journal._connect()
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM host_observations"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(2, count)
            journal.integrity_check()
            with journal._transaction() as connection:
                connection.execute(
                    "UPDATE host_observations SET payload_json = ? WHERE observation_id = ?",
                    ('{"tampered":true}', latest["observation_id"]),
                )
            with self.assertRaises(IntegrityError):
                journal.integrity_check()

    def test_restore_plan_detects_encrypted_object_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config, journal, destination = self._fixture(root)
            runner = FakeAgeRunner()
            identity = root / "identity"
            identity.write_text("fixture")
            identity.chmod(0o600)
            with mock.patch(
                "ophelia.daemon.recovery.shutil.which", return_value="/usr/bin/age"
            ):
                plan = host_backup_plan(
                    config,
                    journal,
                    backup_id="backup_tampered",
                    destination_root=destination,
                )
                receipt = create_host_backup(
                    config,
                    journal,
                    backup_id="backup_tampered",
                    destination_root=destination,
                    confirmation=plan["confirmation_token"],
                    runner=runner,
                )
                object_path = Path(receipt["backup_root"]) / "recovery.tar.gz.age"
                object_path.write_bytes(object_path.read_bytes() + b"tampered")
                restore = clean_host_restore_plan(
                    backup_root=Path(receipt["backup_root"]),
                    identity_file=identity,
                    target_runtime_root=root / "restored-tampered",
                )
            self.assertFalse(restore["can_apply"])
            self.assertIn("encrypted_object_integrity_failed", restore["blockers"])


if __name__ == "__main__":
    unittest.main()
