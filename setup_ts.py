# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""
Script for building TypeScript modules.
This script is called by `setup.py` and common users should avoid using this directly.

It compiles TypeScript source files in `ts` directory,
and copies (or links) JavaScript output as well as dependencies to `nni_node`.

You can set environment `GLOBAL_TOOLCHAIN=1` to use global node and yarn, if you know what you are doing.
"""

from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import traceback
from zipfile import ZipFile


node_version = 'v16.3.0'
yarn_version = 'v1.22.10'

def _get_jupyter_lab_version():
    try:
        import jupyterlab
        return jupyterlab.__version__
    except ImportError:
        return '3.x'

jupyter_lab_major_version = _get_jupyter_lab_version().split('.')[0]

def build(release):
    """
    Compile TypeScript modules and copy or symlink to nni_node directory.

    `release` is the version number without leading letter "v".

    If `release` is None or empty, this is a development build and uses symlinks on Linux/macOS;
    otherwise this is a release build and copies files instead.
    On Windows it always copies files because creating symlink requires extra privilege.
    """
    if release or not os.environ.get('GLOBAL_TOOLCHAIN'):
        download_toolchain()
    prepare_nni_node()
    update_package()
    compile_ts(release)
    if release or sys.platform == 'win32':
        copy_nni_node(release)
    else:
        symlink_nni_node()
    restore_package()

def clean(clean_all=False):
    """
    Remove TypeScript-related intermediate files.
    Python intermediate files are not touched here.
    """
    shutil.rmtree('nni_node', ignore_errors=True)

    for file_or_dir in generated_files:
        path = Path(file_or_dir)
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    if clean_all:
        shutil.rmtree('toolchain', ignore_errors=True)


if sys.platform == 'linux' or sys.platform == 'darwin':
    node_executable = 'node'
    node_spec = f'node-{node_version}-{sys.platform}-x64'
    node_download_url = f'https://nodejs.org/dist/{node_version}/{node_spec}.tar.xz'
    node_extractor = lambda data: tarfile.open(fileobj=BytesIO(data), mode='r:xz')
    node_executable_in_tarball = 'bin/node'

    yarn_executable = 'yarn'
    yarn_download_url = f'https://github.com/yarnpkg/yarn/releases/download/{yarn_version}/yarn-{yarn_version}.tar.gz'

    path_env_seperator = ':'

elif sys.platform == 'win32':
    node_executable = 'node.exe'
    node_spec = f'node-{node_version}-win-x64'
    node_download_url = f'https://nodejs.org/dist/{node_version}/{node_spec}.zip'
    node_extractor = lambda data: ZipFile(BytesIO(data))
    node_executable_in_tarball = 'node.exe'

    yarn_executable = 'yarn.cmd'
    yarn_download_url = f'https://github.com/yarnpkg/yarn/releases/download/{yarn_version}/yarn-{yarn_version}.tar.gz'

    path_env_seperator = ';'

else:
    raise RuntimeError('Unsupported system')


def download_toolchain():
    """
    Download and extract node and yarn.
    """
    if Path('toolchain/node', node_executable_in_tarball).is_file():
        return

    Path('toolchain').mkdir(exist_ok=True)
    import requests  # place it here so setup.py can install it before importing

    _print(f'Downloading node.js from {node_download_url}')
    resp = requests.get(node_download_url)
    resp.raise_for_status()
    _print('Extracting node.js')
    tarball = node_extractor(resp.content)
    tarball.extractall('toolchain')
    shutil.rmtree('toolchain/node', ignore_errors=True)
    Path('toolchain', node_spec).rename('toolchain/node')

    _print(f'Downloading yarn from {yarn_download_url}')
    resp = requests.get(yarn_download_url)
    resp.raise_for_status()
    _print('Extracting yarn')
    tarball = tarfile.open(fileobj=BytesIO(resp.content), mode='r:gz')
    tarball.extractall('toolchain')
    shutil.rmtree('toolchain/yarn', ignore_errors=True)
    Path(f'toolchain/yarn-{yarn_version}').rename('toolchain/yarn')

def update_package():
    if jupyter_lab_major_version == '2':
        package_json = json.load(open('ts/jupyter_extension/package.json'))
        json.dump(package_json, open('ts/jupyter_extension/.package_default.json', 'w'), indent=2)

        package_json['scripts']['build'] = 'tsc && jupyter labextension link .'
        package_json['dependencies']['@jupyterlab/application'] = '^2.3.0'
        package_json['dependencies']['@jupyterlab/launcher'] = '^2.3.0'

        package_json['jupyterlab']['outputDir'] = 'build'
        json.dump(package_json, open('ts/jupyter_extension/package.json', 'w'), indent=2)
        print(f'updated package.json with {json.dumps(package_json, indent=2)}')

def restore_package():
    if jupyter_lab_major_version == '2':
        package_json = json.load(open('ts/jupyter_extension/.package_default.json'))
        print(f'stored package.json with {json.dumps(package_json, indent=2)}')
        json.dump(package_json, open('ts/jupyter_extension/package.json', 'w'), indent=2)
        os.remove('ts/jupyter_extension/.package_default.json')

def prepare_nni_node():
    """
    Create clean nni_node diretory, then copy node runtime to it.
    """
    shutil.rmtree('nni_node', ignore_errors=True)
    Path('nni_node').mkdir()

    Path('nni_node/__init__.py').write_text('"""NNI node.js modules."""\n')

    node_src = Path('toolchain/node', node_executable_in_tarball)
    node_dst = Path('nni_node', node_executable)
    shutil.copy(node_src, node_dst)


def compile_ts(release):
    """
    Use yarn to download dependencies and compile TypeScript code.
    """
    _print('Building NNI manager')
    _yarn('ts/nni_manager')
    _yarn('ts/nni_manager', 'build')
    # todo: I don't think these should be here
    shutil.rmtree('ts/nni_manager/dist/config', ignore_errors=True)
    shutil.copytree('ts/nni_manager/config', 'ts/nni_manager/dist/config')

    _print('Building web UI')
    _yarn('ts/webui')
    _yarn('ts/webui', 'build')

    _print('Building JupyterLab extension')
    if release:
        _yarn('ts/jupyter_extension')
        _yarn('ts/jupyter_extension', 'build')
    else:
        try:
            _yarn('ts/jupyter_extension')
            _yarn('ts/jupyter_extension', 'build')
        except Exception:
            _print('Failed to build JupyterLab extension, skip for develop mode', color='yellow')
            _print(traceback.format_exc(), color='yellow')


def symlink_nni_node():
    """
    Create symlinks to compiled JS files.
    If you manually modify and compile TS source files you don't need to install again.
    """
    _print('Creating symlinks')

    for path in Path('ts/nni_manager/dist').iterdir():
        _symlink(path, Path('nni_node', path.name))
    _symlink('ts/nni_manager/package.json', 'nni_node/package.json')
    _symlink('ts/nni_manager/node_modules', 'nni_node/node_modules')

    _symlink('ts/webui/build', 'nni_node/static')

    if jupyter_lab_major_version == '2':
        _symlink('ts/jupyter_extension/build', 'nni_node/jupyter-extension')
        _symlink(os.path.join(sys.exec_prefix, 'share/jupyter/lab/extensions'), 'nni_node/jupyter-extension/extensions')
    elif Path('ts/jupyter_extension/dist').exists():
        _symlink('ts/jupyter_extension/dist', 'nni_node/jupyter-extension')


def copy_nni_node(version):
    """
    Copy compiled JS files to nni_node.
    This is meant for building release package, so you need to provide version string.
    The version will written to `package.json` in nni_node directory,
    while `package.json` in ts directory will be left unchanged.
    """
    _print('Copying files')

    # copytree(..., dirs_exist_ok=True) is not supported by Python 3.6
    for path in Path('ts/nni_manager/dist').iterdir():
        if path.is_dir():
            shutil.copytree(path, Path('nni_node', path.name))
        elif path.name != 'nni_manager.tsbuildinfo':
            shutil.copyfile(path, Path('nni_node', path.name))

    package_json = json.load(open('ts/nni_manager/package.json'))
    if version:
        while len(version.split('.')) < 3:  # node.js semver requires at least three parts
            version = version + '.0'
        package_json['version'] = version
    json.dump(package_json, open('nni_node/package.json', 'w'), indent=2)

    # reinstall without development dependencies
    _yarn('ts/nni_manager', '--prod', '--cwd', str(Path('nni_node').resolve()))

    shutil.copytree('ts/webui/build', 'nni_node/static')

    if jupyter_lab_major_version == '2':
        shutil.copytree('ts/jupyter_extension/build', 'nni_node/jupyter-extension/build')
        shutil.copytree(os.path.join(sys.exec_prefix, 'share/jupyter/lab/extensions'), 'nni_node/jupyter-extension/extensions')
    elif version or Path('ts/jupyter_extension/dist').exists():
        shutil.copytree('ts/jupyter_extension/dist', 'nni_node/jupyter-extension')


_yarn_env = dict(os.environ)
# `Path('nni_node').resolve()` does not work on Windows if the directory not exists
_yarn_env['PATH'] = str(Path().resolve() / 'nni_node') + path_env_seperator + os.environ['PATH']
_yarn_path = Path().resolve() / 'toolchain/yarn/bin' / yarn_executable

def _yarn(path, *args):
    if os.environ.get('GLOBAL_TOOLCHAIN'):
        subprocess.run(['yarn', *args], cwd=path, check=True)
    else:
        subprocess.run([str(_yarn_path), *args], cwd=path, check=True, env=_yarn_env)


def _symlink(target_file, link_location):
    target = Path(target_file)
    link = Path(link_location)
    relative = os.path.relpath(target, link.parent)
    link.symlink_to(relative, target.is_dir())


def _print(*args, color='cyan'):
    color_code = {'yellow': 33, 'cyan': 36}[color]
    if sys.platform == 'win32':
        print(*args, flush=True)
    else:
        print(f'\033[1;{color_code}m#', *args, '\033[0m', flush=True)


generated_files = [
    'ts/nni_manager/dist',
    'ts/nni_manager/node_modules',
    'ts/webui/build',
    'ts/webui/node_modules',

    # unit test
    'ts/nni_manager/.nyc_output',
    'ts/nni_manager/coverage',
    'ts/nni_manager/exp_profile.json',
    'ts/nni_manager/metrics.json',
    'ts/nni_manager/trial_jobs.json',
]
