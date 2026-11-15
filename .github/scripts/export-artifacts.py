#!/usr/bin/env python3
"""Export only verified ROM and kernel binaries, never the source workspace."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import zipfile

PARTITIONS = ('system', 'vendor', 'product', 'odm')
KERNEL_FILES = ('Image', 'dtb.img', 'dtbo.img', 'boot.img', 'kernel.config', 'System.map')
GROUP_SIZE = 9432989696
ZIP_FILES = {
    'boot.img', 'dtbo.img', 'unsparse_super_empty.img', 'dynamic_partitions_op_list',
    'META-INF/com/android/metadata', 'META-INF/com/android/metadata.pb',
    'META-INF/com/android/otacert', 'META-INF/com/google/android/update-binary',
    'META-INF/com/google/android/updater-script',
} | {part + suffix for part in PARTITIONS for suffix in ('.new.dat.br', '.transfer.list', '.patch.dat')}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for data in iter(lambda: source.read(8 << 20), b''):
            digest.update(data)
    return digest.hexdigest()


def export(rom, kernel, output):
    report = json.loads((rom / 'build-info.json').read_text())
    require(report.get('target') == 'r8s' and report.get('filesystem') == 'erofs', 'Wrong ROM target or filesystem')
    require(report.get('filesystem_check') == 'fsck.erofs --extract', 'EROFS verification is missing')
    require(report.get('group_max_bytes') == GROUP_SIZE, 'Unexpected dynamic group capacity')
    require(set(report.get('images', {})) == set(PARTITIONS), 'The release requires four verified images')
    total = 0
    for part in PARTITIONS:
        path = rom / (part + '.img')
        info = report['images'][part]
        require(path.stat().st_size == info['bytes'] and sha(path) == info['sha256'], 'Image changed: ' + part)
        with path.open('rb') as image:
            image.seek(1024)
            require(image.read(4) == bytes.fromhex('e2e1f5e0'), 'Image is not EROFS: ' + part)
        total += info['bytes']
    require(total <= GROUP_SIZE, 'Images exceed the dynamic group')
    info = json.loads((kernel / 'kernel-info.json').read_text())
    require(info.get('target') == 'r8s' and len(info.get('profile_sha256', '')) == 64, 'Updated kernel provenance is missing')
    for name in KERNEL_FILES:
        require(sha(kernel / name) == info.get('files', {}).get(name), 'Kernel checksum differs: ' + name)
    require(sha(kernel / 'Image') == info.get('image_sha256'), 'Image provenance differs')
    boot = (kernel / 'boot.img').read_bytes()
    require(boot[:8] == b'ANDROID!' and len(boot) >= 1660, 'Invalid boot image')
    page, version = struct.unpack_from('<II', boot, 36)
    size = struct.unpack_from('<I', boot, 8)[0]
    require(version == 2 and page in (2048, 4096), 'Unexpected boot layout')
    require(hashlib.sha256(boot[page:page + size]).hexdigest() == info['image_sha256'], 'Boot contains another kernel')
    candidates = list(rom.glob('*.zip'))
    require(len(candidates) == 1, 'Expected one completed ROM ZIP')
    archive_path = candidates[0]
    with zipfile.ZipFile(archive_path) as archive:
        names = [p.filename for p in archive.infolist() if not p.is_dir()]
        require(len(names) == len(set(names)), 'Duplicate ZIP entries')
        require(set(names) <= ZIP_FILES, 'ZIP contains unexpected development files')
        required = ZIP_FILES - {part + '.patch.dat' for part in PARTITIONS}
        require(required <= set(names), 'ZIP is missing release payloads')
        for name in ('boot.img', 'dtbo.img'):
            require(hashlib.sha256(archive.read(name)).hexdigest() == info['files'][name], 'ZIP kernel differs: ' + name)
        operations = archive.read('dynamic_partitions_op_list').decode().splitlines()
        require('add_group group_basic ' + str(GROUP_SIZE) in operations, 'ZIP group capacity differs')
        for part in PARTITIONS:
            require(f"resize {part} {report['images'][part]['bytes']}" in operations, 'ZIP partition size differs: ' + part)
    require(not output.exists(), 'Release output already exists')
    output.mkdir(parents=True)
    for path in (archive_path,) + tuple(kernel / name for name in KERNEL_FILES):
        destination = output / path.name
        try:
            os.link(path, destination)
        except OSError:
            shutil.copyfile(path, destination)
    with zipfile.ZipFile(output / 'GmKernel-r8s.zip', 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for name in KERNEL_FILES:
            archive.write(kernel / name, name)
        archive.writestr('SHA256SUMS', ''.join(sha(kernel / name) + '  ' + name + '\n' for name in KERNEL_FILES))
    (output / 'release-notes.txt').write_text(
        'ColorOS 17 C.80 - Galaxy S20 FE SM-G780F/r8s\n'
        'Maintainer: GMNEVZ\n\n'
        'Included: updated GmKernel profile (17 kernel and 2 KernelSU patches), '
        'C.80 HIDL enrollment guard, offline charger selection, existing HYPER '
        'normalizer and Horae bridge, runner filesystem support, maintainer label.\n'
        'Excluded: new visual ART compilation routine (S33), SeedlingSdk DEX context (S34).\n'
        'Four EROFS images verified; boot and DTBO match the exported kernel. '
        'This release has not undergone new device testing.\n', encoding='utf-8')
    files = sorted(path for path in output.iterdir() if path.is_file())
    (output / 'SHA256SUMS').write_text(''.join(sha(path) + '  ' + path.name + '\n' for path in files))
    print('Four EROFS images verified; group space remaining:', GROUP_SIZE - total)
    print('ZIP contents, embedded boot/DTBO and exported kernel hashes match.')
    print('Release files:', ', '.join(path.name for path in sorted(output.iterdir())))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('rom-dir', 'kernel-dir', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    export(args.rom_dir.resolve(), args.kernel_dir.resolve(), args.output.resolve())
