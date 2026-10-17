import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from paperlab_local.incremental import sync_now
from paperlab_local.protocol import handshake
from paperlab_local.store import Store


class Reply:
    def __init__(self, data): self.data = data
    def json(self): return self.data
    def raise_for_status(self): pass


class Server:
    def __init__(self):
        self.sequence = 0
        self.batch_sizes = []
        self.reject = False
    def get(self, url, **kwargs):
        if url.endswith('/handshake'): return Reply(handshake())
        return Reply({'changes': [], 'latest_sequence': self.sequence, 'has_more': False})
    def post(self, url, json, **kwargs):
        self.batch_sizes.append(len(json['changes']))
        accepted, rejected = [], []
        for i, change in enumerate(json['changes']):
            if self.reject and i > 0:
                rejected.append({'change_id': change['change_id'], 'error': 'fixture rejection'})
            else:
                self.sequence += 1
                accepted.append({'change_id': change['change_id'], 'server_sequence': self.sequence})
        return Reply({'accepted': accepted, 'rejected': rejected})


class SyncBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cfg = SimpleNamespace(root=root, db_path=root/'data/papers.db', pdf_root=root)
        self.server = Server()
        @contextmanager
        def connection():
            yield SimpleNamespace(base_url='http://127.0.0.1:12345', session=self.server, transport='ssh')
        self.env = patch.dict(os.environ, {'PAPERLAB_DEVICE_TOKEN': 'fixture', 'PAPERLAB_SYNC_SERVER': '0'})
        self.env.start(); self.addCleanup(self.env.stop)
        mock = patch('paperlab_local.transport.sync_connection', connection)
        mock.start(); self.addCleanup(mock.stop)

    def add_papers(self, count):
        with Store(self.cfg.db_path, backup_on_migrate=False) as store:
            for index in range(count):
                store.upsert_candidate({'title': f'Batch paper {index}'}, commit=False)
            store.conn.commit()

    def test_drains_more_than_500_pending_changes(self):
        self.add_papers(1001)
        result = sync_now(self.cfg, log=lambda *_: None)
        self.assertEqual(result['pushed'], 1001)
        self.assertEqual(self.server.batch_sizes, [500, 500, 1])
        self.assertEqual(result['transport'], 'ssh')
        with Store(self.cfg.db_path, backup_on_migrate=False) as store:
            self.assertEqual(len(store.pending_changes()), 0)

    def test_partial_rejection_preserves_unaccepted_edits_for_retry(self):
        self.add_papers(3)
        self.server.reject = True
        with self.assertRaisesRegex(RuntimeError, 'rejected'):
            sync_now(self.cfg, log=lambda *_: None)
        with Store(self.cfg.db_path, backup_on_migrate=False) as store:
            self.assertEqual(len(store.pending_changes()), 2)
            self.assertIn('rejected', store.conn.execute('SELECT last_error FROM sync_runtime').fetchone()[0])
        self.server.reject = False
        self.assertEqual(sync_now(self.cfg, log=lambda *_: None)['pushed'], 2)

    def test_failed_transport_is_recorded_without_losing_edits(self):
        self.add_papers(1)
        with patch('paperlab_local.transport.sync_connection', side_effect=RuntimeError('SSH unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'SSH unavailable'):
                sync_now(self.cfg, log=lambda *_: None)
        with Store(self.cfg.db_path, backup_on_migrate=False) as store:
            self.assertEqual(len(store.pending_changes()), 1)
            self.assertEqual(store.conn.execute('SELECT last_error FROM sync_runtime').fetchone()[0], 'SSH unavailable')
