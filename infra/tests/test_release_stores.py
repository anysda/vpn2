"""The same image contract must work with both Docker image stores."""
import json
import unittest
from unittest.mock import patch

from test_release import module


class ImageStoreTests(unittest.TestCase):
    def test_containerd_index_selects_platform_from_immutable_id(self):
        release = module('release')
        index = 'sha256:' + 'd' * 64
        expected = 'sha256:' + 'a' * 64
        foreign = 'sha256:' + 'c' * 64
        manifest = {'images': {'panel': {'amd64': {'repository': 'example/panel',
                    'digest': expected, 'config': 'sha256:' + 'b' * 64}}}}
        for selected in (expected, foreign):
            with self.subTest(platform_digest=selected):
                responses = [json.dumps([{'Id': index, 'Architecture': 'amd64',
                              'Descriptor': {'digest': index, 'mediaType': 'application/vnd.oci.image.index.v1+json'}}]),
                             json.dumps([{'Id': selected, 'Architecture': 'amd64',
                              'RepoDigests': ['example/panel@' + index], 'Descriptor': {'digest': selected}}])]
                with patch.object(release, 'command', side_effect=responses) as run:
                    if selected == expected:
                        self.assertEqual(release.image(manifest, 'panel', 'example/panel:1.0.1', False, 'amd64'), expected)
                    else:
                        with self.assertRaisesRegex(release.ReleaseError, 'digest mismatch'):
                            release.image(manifest, 'panel', 'example/panel:1.0.1', False, 'amd64')
                self.assertEqual(run.call_args_list[-1].args[0],
                                 ['docker', 'image', 'inspect', '--platform', 'linux/amd64', index])

    def test_inspect_stores_and_retagged_foreign_images(self):
        release = module('release')
        manifest_digest = 'sha256:' + 'a' * 64
        config = 'sha256:' + 'b' * 64
        foreign = 'sha256:' + 'c' * 64
        for component in ('panel', 'tgbot'):
            repository = 'example/' + component
            reference = repository + '@' + manifest_digest
            manifest = {'images': {component: {'amd64': {
                'repository': repository, 'digest': manifest_digest, 'config': config}}}}
            cases = (
                ('classic', config, [reference], None, True),
                ('containerd', manifest_digest, [reference], {'digest': manifest_digest}, True),
                ('containerd-no-descriptor', manifest_digest, [reference], None, True),
                ('classic-loaded', config, [], None, True),
                ('containerd-loaded', manifest_digest, [], {'digest': manifest_digest}, True),
                ('foreign-classic', foreign, [repository + '@' + foreign], None, False),
                ('foreign-containerd', foreign, [repository + '@' + foreign], {'digest': foreign}, False),
                ('tag-only', foreign, [], None, False),
                ('descriptor-is-not-config', config, [], {'digest': config}, False),
            )
            for label, image_id, repos, descriptor, accepted in cases:
                with self.subTest(component=component, store=label):
                    actual = {'Id': image_id, 'Architecture': 'amd64', 'RepoDigests': repos,
                              'RepoTags': [repository + ':1.0.1']}
                    if descriptor:
                        actual['Descriptor'] = descriptor
                    with patch.object(release, 'command', return_value=json.dumps([actual])):
                        if accepted:
                            self.assertEqual(release.image(manifest, component, repository + ':1.0.1',
                                                           False, 'amd64'), image_id)
                        else:
                            with self.assertRaisesRegex(release.ReleaseError, 'digest mismatch'):
                                release.image(manifest, component, repository + ':1.0.1', False, 'amd64')
                    actual['Architecture'] = 'arm64'
                    with patch.object(release, 'command', return_value=json.dumps([actual])):
                        with self.assertRaisesRegex(release.ReleaseError, 'digest mismatch'):
                            release.image(manifest, component, pull=False, arch='amd64')

    def test_containerd_never_looks_up_unavailable_config(self):
        release = module('release')
        expected = 'sha256:' + 'a' * 64
        reference = 'example/panel@' + expected
        manifest = {'images': {'panel': {'amd64': {'repository': 'example/panel',
                    'digest': expected, 'config': 'sha256:' + 'b' * 64}}}}
        actual = {'Id': expected, 'Architecture': 'amd64', 'RepoDigests': [reference]}
        with patch.object(release, 'command', return_value=json.dumps([actual])) as run:
            self.assertEqual(release.image(manifest, 'panel', pull=False, arch='amd64'), expected)
        run.assert_called_once_with(['docker', 'image', 'inspect', reference])


if __name__ == '__main__':
    unittest.main(verbosity=2)
