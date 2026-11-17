import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('release_export', Path(__file__).with_name('export-artifacts.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
sha = lambda data: hashlib.sha256(data).hexdigest()


class ReleaseTests(unittest.TestCase):
    def fixture(self, root):
        rom, kernel = root / 'rom', root / 'kernel'
        rom.mkdir(); kernel.mkdir()
        report = {'target': 'r8s', 'filesystem': 'erofs', 'filesystem_check': 'fsck.erofs --extract',
                  'group_max_bytes': release.GROUP_SIZE, 'images': {}}
        for part in release.PARTITIONS:
            data = bytes(1024) + bytes.fromhex('e2e1f5e0') + bytes(3068)
            (rom / (part + '.img')).write_bytes(data)
            report['images'][part] = {'bytes': len(data), 'sha256': sha(data)}
        (rom / 'build-info.json').write_text(json.dumps(report))
        image = b'kernel fixture'
        boot = bytearray(4096)
        boot[:8] = b'ANDROID!'
        struct.pack_into('<I', boot, 8, len(image))
        struct.pack_into('<II', boot, 36, 4096, 2)
        boot += image
        for name in release.KERNEL_FILES:
            (kernel / name).write_bytes(boot if name == 'boot.img' else image if name == 'Image' else b'fixture')
        info = {'target': 'r8s', 'profile_sha256': 'a' * 64, 'image_sha256': sha(image),
                'files': {name: release.sha(kernel / name) for name in release.KERNEL_FILES}}
        (kernel / 'kernel-info.json').write_text(json.dumps(info))
        with zipfile.ZipFile(rom / 'rom.zip', 'w') as archive:
            for name in release.ZIP_FILES:
                data = (kernel / name).read_bytes() if name in ('boot.img', 'dtbo.img') else b'fixture'
                if name == 'dynamic_partitions_op_list':
                    data = ('add_group group_basic ' + str(release.GROUP_SIZE) + '\n' +
                            ''.join('resize ' + part + ' 4096\n' for part in release.PARTITIONS)).encode()
                archive.writestr(name, data)
        return rom, kernel

    def test_export_binds_zip_kernel_and_all_published_checksums(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rom, kernel = self.fixture(root)
            output = root / 'release'
            release.export(rom, kernel, output)
            for line in (output / 'SHA256SUMS').read_text().splitlines():
                digest, name = line.split('  ')
                self.assertEqual(release.sha(output / name), digest)
            with zipfile.ZipFile(output / 'GmKernel-r8s.zip') as archive:
                self.assertEqual(set(archive.namelist()), set(release.KERNEL_FILES) | {'SHA256SUMS'})
                self.assertEqual(archive.read('boot.img'), (kernel / 'boot.img').read_bytes())
            self.assertFalse((output / 'kernel-info.json').exists())

    def test_missing_profile_provenance_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rom, kernel = self.fixture(root)
            path = kernel / 'kernel-info.json'
            info = json.loads(path.read_text())
            info.pop('profile_sha256')
            path.write_text(json.dumps(info))
            with self.assertRaisesRegex(RuntimeError, 'provenance'):
                release.export(rom, kernel, root / 'release')
            self.assertFalse((root / 'release').exists())

    def test_development_files_are_rejected_before_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rom, kernel = self.fixture(root)
            with zipfile.ZipFile(rom / 'rom.zip', 'a') as archive:
                archive.writestr('kernel-source.patch', 'private development file')
            with self.assertRaisesRegex(RuntimeError, 'development'):
                release.export(rom, kernel, root / 'release')
            self.assertFalse((root / 'release').exists())

    def test_changed_kernel_and_unverified_filesystem_are_rejected(self):
        for failure in ('kernel', 'filesystem'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                rom, kernel = self.fixture(root)
                if failure == 'kernel':
                    (kernel / 'dtbo.img').write_bytes(b'changed')
                else:
                    data = json.loads((rom / 'build-info.json').read_text())
                    data['filesystem_check'] = 'not_run'
                    (rom / 'build-info.json').write_text(json.dumps(data))
                with self.assertRaises(RuntimeError):
                    release.export(rom, kernel, root / 'release')


if __name__ == '__main__':
    unittest.main()
