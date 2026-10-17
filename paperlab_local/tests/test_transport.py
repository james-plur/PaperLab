import io
import os
import signal
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from paperlab_local.transport import _stop_tunnel, _wait_for_tunnel, sync_configured, sync_connection


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_ssh_is_default_and_needs_explicit_host(self):
        os.environ['PAPERLAB_SYNC_URL'] = 'https://old-gateway.example'
        with self.assertRaisesRegex(ValueError, 'PAPERLAB_SSH_HOST'):
            with sync_connection():
                self.fail('must not silently use the HTTP endpoint')

    def test_ssh_is_loopback_only_ignores_proxies_and_cleans_up(self):
        os.environ.update(PAPERLAB_SSH_HOST='research-server', HTTPS_PROXY='http://invalid:1')
        process = MagicMock()
        process.poll.return_value = None
        with patch('paperlab_local.transport.shutil.which', return_value='/usr/bin/ssh'), \
             patch('paperlab_local.transport.subprocess.Popen', return_value=process) as start, \
             patch('paperlab_local.transport._wait_for_tunnel'):
            with self.assertRaisesRegex(RuntimeError, 'request failed'):
                with sync_connection() as connection:
                    self.assertEqual(connection.transport, 'ssh')
                    self.assertFalse(connection.session.trust_env)
                    self.assertTrue(connection.base_url.startswith('http://127.0.0.1:'))
                    command = start.call_args.args[0]
                    self.assertEqual(command[-1], 'research-server')
                    self.assertIn('StrictHostKeyChecking=yes', command)
                    self.assertIn('UserKnownHostsFile=' + os.path.expanduser('~/.ssh/known_hosts'), command)
                    self.assertIn('ForkAfterAuthentication=no', command)
                    self.assertRegex(command[-2], r'^127\.0\.0\.1:\d+:127\.0\.0\.1:8765$')
                    raise RuntimeError('request failed')
        process.terminate.assert_called_once()
        process.wait.assert_called_once()

    def test_sigterm_cleans_up_and_restores_handler(self):
        os.environ['PAPERLAB_SSH_HOST'] = 'research-server'
        previous = signal.getsignal(signal.SIGTERM)
        process = MagicMock()
        process.poll.return_value = None
        with patch('paperlab_local.transport.shutil.which', return_value='/usr/bin/ssh'), \
             patch('paperlab_local.transport.subprocess.Popen', return_value=process), \
             patch('paperlab_local.transport._wait_for_tunnel'):
            with self.assertRaises(SystemExit):
                with sync_connection():
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        process.terminate.assert_called_once()
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_discarded_host_keys_are_rejected(self):
        os.environ.update(PAPERLAB_SSH_HOST='research-server', PAPERLAB_SSH_KNOWN_HOSTS='/dev/null')
        with self.assertRaisesRegex(ValueError, 'persistent known_hosts'):
            with sync_connection():
                self.fail('must verify a persistent trusted host key')

    def test_next_attempt_creates_a_new_tunnel_after_failure(self):
        os.environ['PAPERLAB_SSH_HOST'] = 'research-server'
        processes = [MagicMock(), MagicMock()]
        for process in processes:
            process.poll.return_value = None
        with patch('paperlab_local.transport.shutil.which', return_value='/usr/bin/ssh'), \
             patch('paperlab_local.transport.subprocess.Popen', side_effect=processes) as start, \
             patch('paperlab_local.transport._wait_for_tunnel', side_effect=[RuntimeError('offline'), None]):
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                with sync_connection():
                    pass
            with sync_connection() as connection:
                self.assertEqual(connection.transport, 'ssh')
        self.assertEqual(start.call_count, 2)
        for process in processes:
            process.terminate.assert_called_once()

    def test_known_hosts_or_auth_failure_is_reported(self):
        process = MagicMock()
        process.poll.return_value = 255
        with self.assertRaisesRegex(RuntimeError, 'Host key verification failed'):
            _wait_for_tunnel(process, 12345, io.BytesIO(b'Host key verification failed'))

    def test_unresponsive_process_is_killed(self):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('ssh', 3), None]
        _stop_tunnel(process)
        process.kill.assert_called_once()

    def test_explicit_https_fallback_and_url_validation(self):
        os.environ.update(PAPERLAB_SYNC_TRANSPORT='https', PAPERLAB_SYNC_URL='https://example.com/paperlab/')
        with sync_connection() as connection:
            self.assertEqual(connection.base_url, 'https://example.com/paperlab')
        for url in ('http://example.com', 'https://user:secret@example.com', 'https://example.com/?token=secret'):
            os.environ['PAPERLAB_SYNC_URL'] = url
            with self.assertRaises(ValueError):
                with sync_connection():
                    pass

    def test_configuration_rejects_option_injection_and_invalid_port(self):
        for host in ('-oProxyCommand=bad', 'host with spaces'):
            os.environ['PAPERLAB_SSH_HOST'] = host
            with self.assertRaises(ValueError):
                with sync_connection():
                    pass
        os.environ.update(PAPERLAB_SSH_HOST='research-server', PAPERLAB_SSH_REMOTE_PORT='0')
        with self.assertRaises(ValueError):
            with sync_connection():
                pass

    def test_autosync_uses_ssh_host_instead_of_public_url(self):
        os.environ.update(PAPERLAB_DEVICE_TOKEN='device-token', PAPERLAB_SSH_HOST='research-server')
        self.assertTrue(sync_configured())
        del os.environ['PAPERLAB_SSH_HOST']
        self.assertFalse(sync_configured())
