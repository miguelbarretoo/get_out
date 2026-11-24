import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / 'workflows/build.yml'


class PinnedJdk(unittest.TestCase):
    def block(self):
        text = WORKFLOW.read_text(encoding='utf-8')
        return text.split('      - name: Install pinned r8s build JDK\n', 1)[1].split('\n      - name:', 1)[0]

    def install(self, version='25.0.4.1', corrupt=False):
        script = textwrap.dedent(self.block().split('        run: |\n', 1)[1])
        with tempfile.TemporaryDirectory(prefix='jdk-contract-') as directory:
            root = Path(directory)
            run = root / 'runner'
            run.mkdir()
            manifest = root / 'QuantumROM/targets/r8s/compat/hub/toolchain.lock.json'
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps(dict(java_version='25.0.4.1')))
            archive = root / 'fixture.tar.gz'
            with tarfile.open(archive, 'w:gz') as output:
                content = ('#!/bin/sh\necho "javac ' + version + '"\n').encode()
                info = tarfile.TarInfo('jdk-test/bin/javac')
                info.size = len(content)
                info.mode = 0o755
                output.addfile(info, io.BytesIO(content))
            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            if corrupt:
                checksum = '0' * 64
            env = dict(os.environ, RUNNER_TEMP=run.as_posix(), JDK_URL='https://fixture.invalid/jdk.tar.gz',
                       JDK_SHA256=checksum, GITHUB_ENV=(root / 'environment').as_posix(),
                       GITHUB_PATH=(root / 'path').as_posix(), TEST_ARCHIVE=archive.as_posix())
            # The script under test is copied verbatim. Only network and host
            # utility executables are doubled; checksum/extraction really run.
            mock = '''curl() { while [ "$#" -gt 0 ]; do if [ "$1" = --output ]; then cp "$TEST_ARCHIVE" "$2"; return; fi; shift; done; return 1; }
'''
            busybox = os.environ.get('GM_TEST_BUSYBOX')
            if busybox:
                box = Path(busybox).as_posix()
                env['TEST_BOX'] = box
                env['TEST_PYTHON'] = os.sys.executable.replace('\\', '/')
                mock = '''cp() { "$TEST_BOX" cp "$@"; }
mkdir() { "$TEST_BOX" mkdir "$@"; }
tar() { "$TEST_BOX" tar "$@"; }
sha256sum() { test "$*" = '--check --status' || return 1; "$TEST_BOX" sha256sum -c -s; }
python3() { "$TEST_PYTHON" "$@"; }
''' + mock
                command = [box, 'sh', '-e']
            else:
                bash = shutil.which('bash')
                self.assertIsNotNone(bash)
                command = [bash, '-e']
            runner = root / 'install.sh'
            runner.write_text(mock + script, encoding='utf-8', newline='\n')
            result = subprocess.run([*command, runner.as_posix()], cwd=root, env=env, capture_output=True, text=True)
            exported = (root / 'environment').read_text() if (root / 'environment').exists() else ''
            path = (root / 'path').read_text() if (root / 'path').exists() else ''
            return result, exported, path

    def test_exact_four_component_jdk_is_exported_without_semver_parsing(self):
        result, exported, path = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('JAVA_HOME=', exported)
        self.assertIn('/r8s-jdk/bin', path)

    def test_checksum_mismatch_never_exports_java(self):
        result, exported, path = self.install(corrupt=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((exported, path), ('', ''))

    def test_wrong_runtime_version_never_exports_java(self):
        result, exported, path = self.install(version='25.0.5')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((exported, path), ('', ''))

    def test_other_targets_keep_temurin_17_and_r8s_archive_is_pinned(self):
        text = WORKFLOW.read_text(encoding='utf-8')
        generic = text.split('      - name: Set up build JDK\n', 1)[1].split('\n      - name:', 1)[0]
        self.assertIn("if: matrix.target != 'r8s'", generic)
        self.assertIn("java-version: '17'", generic)
        self.assertIn("if: matrix.target == 'r8s'", self.block())
        self.assertRegex(self.block(), r'JDK_SHA256: [a-f0-9]{64}\n')
        self.assertIn('/adoptium/temurin25-binaries/releases/download/jdk-25.0.4.1%2B1/', self.block())
        self.assertNotIn('25.0.4.1+1', generic)


if __name__ == '__main__':
    unittest.main()
