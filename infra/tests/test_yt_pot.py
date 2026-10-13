"""Отказ ролика на IP дата-центра не должен навсегда выключать видеопробу."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_deploy import source
from test_yt_failover import load_watch


class PotTests(unittest.TestCase):
    def test_datacenter_refusal_uses_local_token_and_independent_video(self):
        watch = load_watch()
        calls = []
        caches = []
        user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())

        def run(args, **kwargs):
            calls.append(args)
            if args == ['/usr/local/bin/anysda-yt-check']:
                return subprocess.CompletedProcess(args, 0)
            self.assertEqual(args[:4], ['setpriv', f'--reuid={user.pw_uid}',
                                       f'--regid={user.pw_gid}', '--clear-groups'])
            if '/opt/anysda-yt-probe/bin/yt-dlp' in args:
                token = 'youtube:player_client=mweb;fetch_pot=always' in args
                if token:
                    self.assertNotIn('HTTPS_PROXY', kwargs['env'])
                    self.assertIn('youtubepot-bgutilscript:server_home=/opt/anysda-yt-probe/bgutil/server', args)
                    self.assertIn('--source-address', args)
                    self.assertIn(watch.POT_VIDEO, args)
                    self.assertNotEqual(watch.POT_VIDEO, watch.VIDEO)
                    cache = Path(kwargs['env']['XDG_CACHE_HOME'])
                    self.assertTrue(cache.is_dir())
                    self.assertEqual(cache.stat().st_uid, user.pw_uid)
                    caches.append(cache)
                return subprocess.CompletedProcess(args, 0, json.dumps({
                    'url': 'https://first.googlevideo.com/videoplayback' + ('?pot=secret' if token else ''),
                }))
            token = '?pot=secret' in args[-1]
            if token:
                self.assertIn('--location', args)
                self.assertIn('--max-redirs', args)
                self.assertIn('=https', args)
            return subprocess.CompletedProcess(args, 0 if token else 22, json.dumps({
                'http_code': 206 if token else 403, 'size_download': 4194304 if token else 0,
                'speed_download': 500000 if token else 0, 'content_type': 'video/mp4',
                'url_effective': 'https://last.googlevideo.com/video?pot=secret',
            }))

        with patch.object(watch.subprocess, 'run', side_effect=run), \
                patch.object(watch.pwd, 'getpwnam', return_value=user), \
                patch.dict(os.environ, {'HTTPS_PROXY': 'http://proxy.invalid'}):
            good, detail = watch.probe()
        self.assertIs(good, True, detail)
        self.assertIn('HTTP 206, 4194304 байт', detail)
        self.assertIn('CDN=last.googlevideo.com, client=mweb, PO=local', detail)
        self.assertNotIn('secret', detail)
        self.assertEqual(len(calls), 5)
        self.assertEqual(len(caches), 1)
        self.assertFalse(caches[0].exists())

    def test_measured_failure_or_success_is_never_retried_on_another_cdn(self):
        watch = load_watch()
        for verdict in (False, True):
            with self.subTest(verdict=verdict), \
                    patch.object(watch.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                    patch.object(watch.pwd, 'getpwnam'), \
                    patch.object(watch, 'probe_media', return_value=(verdict, 'measured')) as media:
                self.assertEqual(watch.probe(), (verdict, 'measured'))
                media.assert_called_once()

    def test_provider_failure_and_foreign_redirect_cannot_be_success(self):
        watch = load_watch()
        user = SimpleNamespace(pw_uid=42, pw_gid=42)
        for token, target in ((False, 'https://cdn.googlevideo.com/v'),
                              (True, 'https://example.org/video'),
                              (True, 'http://cdn.googlevideo.com/video'), (True, 42)):
            with self.subTest(token=token, target=target):
                responses = [subprocess.CompletedProcess([], 0, json.dumps({
                    'url': 'https://cdn.googlevideo.com/v' + ('?pot=secret' if token else ''),
                })), subprocess.CompletedProcess([], 0, json.dumps({
                    'http_code': 206, 'size_download': 4194304, 'speed_download': 500000,
                    'content_type': 'video/mp4', 'url_effective': target,
                }))]
                with patch.object(watch.subprocess, 'run', side_effect=responses):
                    good, detail = watch.probe_media(user, watch.POT_VIDEO, 'mweb', cache='/cache')
                self.assertIsNone(good)
                self.assertNotIn('secret', detail)

    def test_sabr_refusal_is_explained_without_logging_extractor_secrets(self):
        watch = load_watch()
        user = SimpleNamespace(pw_uid=42, pw_gid=42)
        with patch.object(watch.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 1, '', 'Requested format is not available; secret=hidden')):
            good, detail = watch.probe_media(user, watch.POT_VIDEO, 'mweb', cache='/cache')
        self.assertIsNone(good)
        self.assertIn('SABR/ограничения ролика', detail)
        self.assertNotIn('secret', detail)

    def test_installer_is_shipped_and_service_budget_covers_two_attempts(self):
        self.assertIn('install-yt-pot.sh', source('infra/deploy.sh'))
        stage = source('infra/scripts/19-yt-zapret.sh')
        self.assertIn('bash "$POT_INSTALL" "$YT_PROBE_VENV"', stage)
        self.assertIn('TimeoutStartSec=240', stage)
        installer = source('infra/lib/install-yt-pot.sh')
        self.assertIn('sha256sum -c -', installer)
        self.assertIn('ci --no-audit', installer)
        self.assertIn('prune --omit=dev', installer)


if __name__ == '__main__':
    unittest.main()
