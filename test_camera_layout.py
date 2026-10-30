import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, mock_open

import camera_layout
import camera_state
import migrate_layout


class LayoutTests(unittest.TestCase):
    def test_mapping_is_posix_and_collapses_nginx_registration(self):
        self.assertEqual(camera_layout.canonical('/var/lib/camera-recorder/a.mp4'),
                         '/srv/camera-monitor/data/recordings/a.mp4')
        self.assertEqual(camera_layout.canonical('/etc/nginx/sites-available/camera-monitor'),
                         camera_layout.canonical('/etc/nginx/sites-enabled/camera-monitor'))
        self.assertEqual(camera_layout.canonical('/etc/systemd/system/camera-ai.service'),
                         '/srv/camera-monitor/config/systemd/system/camera-ai.service')
        self.assertEqual(camera_layout.canonical('/var/lib/camera-ai-other'), '/var/lib/camera-ai-other')
        self.assertEqual(camera_layout.canonical('/srv/immich'), '/srv/immich')

    @unittest.skipIf(os.name=='nt','Linux symlink permissions')
    def test_rename_alias_and_reverse_journal_preserve_bytes_and_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); root=base/'project'; root.mkdir()
            old=base/'old'; old.mkdir(); (old/'clip.mp4').write_bytes(b'original')
            inode=(old/'clip.mp4').stat().st_ino
            transaction=migrate_layout.Transaction(root/'journal')
            with patch.object(migrate_layout,'ROOT',root):
                transaction.move(old,root/'data')
                transaction.write(root/'data/config',b'new')
                self.assertTrue(old.is_symlink())
                self.assertEqual((root/'data/clip.mp4').stat().st_ino,inode)
                transaction.undo()
            self.assertFalse(old.is_symlink()); self.assertTrue(old.is_dir())
            self.assertEqual((old/'clip.mp4').read_bytes(),b'original')
            self.assertEqual((old/'clip.mp4').stat().st_ino,inode)
            self.assertFalse((old/'config').exists())
            self.assertEqual(len(json.loads(transaction.journal.read_text())),2)

    @unittest.skipIf(os.name=='nt','Linux symlink permissions')
    def test_overwritten_config_can_be_restored_before_reverse_move(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); root=base/'project'; root.mkdir()
            old=base/'config'; old.write_bytes(b'selected-source'); old.chmod(0o600)
            transaction=migrate_layout.Transaction(root/'journal')
            with patch.object(migrate_layout,'ROOT',root):
                transaction.move(old,root/'config')
                transaction.write(root/'config',b'new-path')
                transaction.undo()
            self.assertEqual(old.read_bytes(),b'selected-source')
            self.assertEqual(old.stat().st_mode & 0o777,0o600)

    def test_move_rejects_destination_outside_project(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); root=base/'project'; root.mkdir()
            old=base/'media'; old.write_bytes(b'preserved')
            transaction=migrate_layout.Transaction(root/'journal')
            with patch.object(migrate_layout,'ROOT',root):
                with self.assertRaises(ValueError):transaction.move(old,base/'outside')
            self.assertEqual(old.read_bytes(),b'preserved')

    @unittest.skipIf(os.name=='nt','Linux flock')
    def test_srv_data_uses_the_shared_production_media_lock(self):
        opened=mock_open()
        with patch.dict(os.environ,{},clear=False), patch.object(Path,'open',opened), patch('fcntl.flock') as flock:
            with patch.dict(os.environ,{'CAMERA_MEDIA_LOCK':''}):
                with camera_state.media_guard('/srv/camera-monitor/data/recordings'):pass
            opened.assert_called_once_with('rb')
            self.assertEqual(flock.call_count,2)


if __name__=='__main__':unittest.main()
