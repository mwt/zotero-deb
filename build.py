#!/usr/bin/env python3

import os, sys
import configparser
import types
import shutil, shlex
import subprocess
import tempfile
import argparse
import re
import magic
import contextlib
import hashlib

args = argparse.ArgumentParser(description='update Zotero deb repo.')
args.add_argument('--config', type=str, default='config.ini')
args.add_argument('--mime', type=str, default='mime.xml')
args.add_argument('staged', nargs='+')
args = args.parse_args()

@contextlib.contextmanager
def IniFile(path):
  ini = configparser.RawConfigParser()
  ini.optionxform=str
  ini.read(path)
  yield ini

@contextlib.contextmanager
def chdir(path):
  cwd= os.getcwd()
  try:
    print('changing to', path)
    os.chdir(path)
    yield
  finally:
    print('changing back to', cwd)
    os.chdir(cwd)

class Open():
  def __init__(self, path, mode='r', fmode=None):
    self.path = path
    if 'w' in mode or 'a' in mode: os.makedirs(os.path.dirname(self.path), exist_ok=True)
    self.mode = fmode
    self.f = open(self.path, mode)

  def __enter__(self):
    return self.f

  def __exit__(self, exc_type, exc_value, exc_traceback):
    self.f.close()
    if self.mode is not None:
      os.chmod(self.path, self.mode)

config = types.SimpleNamespace()

# load build config
with IniFile(args.config) as ini:
  config.ini = ini
config.maintainer = config.ini['maintainer']['email']
config.gpgkey = config.ini['maintainer']['gpgkey']
config.path = dict(config.ini['path'])

# remove trailing slash since it messes with basename
config.staged = [ re.sub(r'/$', '', staged) for staged in args.staged ]

for staged in config.staged:
  assert os.path.isdir(staged)

  deb = types.SimpleNamespace()

  # get version, binary name, and base dir under
  with IniFile(os.path.join(staged, 'application.ini')) as ini:
    deb.vendor = ini['App']['Vendor']
    deb.binary = deb.client = deb.vendor.lower()
    deb.version = ini['App']['Version']
    if '-beta' in deb.version:
      deb.dir = 'beta'
      deb.binary += '-beta'
      deb.version = deb.version.replace('-beta', '')
    else:
      deb.dir = 'release'

  arch = magic.from_file(os.path.join(staged, deb.client + '-bin'))
  if arch.startswith('ELF 32-bit LSB executable, Intel 80386,'):
    deb.arch = 'i386'
  elif arch.startswith('ELF 64-bit LSB executable, x86-64,'):
    deb.arch = 'amd64'
  else:
    print('unsupported architecture', arch)
    sys.exit(1)

  with tempfile.TemporaryDirectory() as builddir:
    print('created temporary directory', builddir)
    deb.build = builddir

  if deb.dir == 'beta' and (bump := config.ini[deb.client].get('beta')):
    deb.bump = '-' + bump
  elif bump := config.ini[deb.client].get(deb.version):
    deb.bump = '-' + bump
  else:
    deb.bump = ''

  if dependencies := config.ini[deb.client].get('dependencies'):
    deb.dependencies = [dep.strip() for dep in dependencies.split(',')]
  else:
    deb.dependencies = []
    
  for dep in os.popen('apt-cache depends firefox-esr').read().split('\n'):
    dep = dep.strip()
    if not dep.startswith('Depends:'): continue
    dep = dep.split(':')[1].strip()
    if dep == 'lsb-release': continue # why should it need this?
    if 'gcc' in dep: continue #43
    deb.dependencies.append(dep)
  deb.dependencies = ', '.join(sorted(list(set(deb.dependencies))))
  deb.description = config.ini[deb.client]['description']
  deb.deb = os.path.join(config.path[deb.dir].format_map(vars(deb)), f'{deb.binary}_{deb.version}{deb.bump}_{deb.arch}.deb')

  # copy zotero to the build directory, excluding the desktpo file (which we'll recreate later) and the update files
  os.makedirs(os.path.join(deb.build, 'usr/lib'), exist_ok=True)
  shutil.copytree(staged, os.path.join(deb.build, 'usr/lib', deb.binary), ignore=shutil.ignore_patterns(deb.client + '.desktop', 'active-update.xml', 'precomplete', 'removed-files', 'updates', 'updates.xml'))
  if deb.binary != deb.client:
    # rename the 'zotero' binary to 'zotero-beta' for the beta package so they can be installed alongside each other
    shutil.move(os.path.join(deb.build, 'usr/lib', deb.binary, deb.client), os.path.join(deb.build, 'usr/lib', deb.binary, deb.binary))


  # disable auto-update
  with Open(os.path.join(deb.build, 'usr/lib/', deb.binary, 'defaults/pref/local-settings.js'), 'a') as ls, Open(os.path.join(deb.build, 'usr/lib/', deb.binary, 'mozilla.cfg'), 'a') as cfg:
    # enable mozilla.cfg
    if ls.tell() != 0: print('', file=ls)
    print('pref("general.config.obscure_value", 0); // only needed if you do not want to obscure the content with ROT-13', file=ls)
    print('pref("general.config.filename", "mozilla.cfg");', file=ls)

    # disable auto-update
    if cfg.tell() == 0:
      print('//', file=cfg)
    else:
      print('', file=cfg)
    print('lockPref("app.update.enabled", false);', file=cfg)
    print('lockPref("app.update.auto", false);', file=cfg)

  # create desktop file
  with IniFile(os.path.join(staged, deb.client + '.desktop')) as ini:
    deb.section = ini['Desktop Entry'].get('Categories', 'Science;Office;Education;Literature').rstrip(';')
    ini.set('Desktop Entry', 'Exec', f'/usr/lib/{deb.binary}/{deb.binary} --url %u')
    ini.set('Desktop Entry', 'Icon', f'/usr/lib/{deb.binary}/chrome/icons/default/default256.png')
    ini.set('Desktop Entry', 'MimeType', ';'.join([
      'x-scheme-handler/zotero',
      'application/x-endnote-refer',
      'application/x-research-info-systems',
      'text/ris',
      'text/x-research-info-systems',
      'application/x-inst-for-Scientific-info',
      'application/mods+xml',
      'application/rdf+xml',
      'application/x-bibtex',
      'text/x-bibtex',
      'application/marc',
      'application/vnd.citationstyles.style+xml'
    ]))
    ini.set('Desktop Entry', 'Description', deb.description.format_map(vars(deb)))
    with Open(os.path.join(deb.build, 'usr/share/applications', f'{deb.binary}.desktop'), 'w') as f:
      ini.write(f, space_around_delimiters=False)

  # add mime info
  with open(args.mime) as mime, Open(os.path.join(deb.build, 'usr/share/mime/packages', f'{deb.binary}.xml'), 'w') as f:
    f.write(mime.read())

  #write build control file
  with Open(os.path.join(deb.build, 'DEBIAN/control'), 'w') as f:
    print(f'Package: {deb.binary}', file=f)
    print(f'Architecture: {deb.arch}', file=f)
    print(f'Depends: {deb.dependencies}', file=f)
    print(f'Maintainer: {config.maintainer}', file=f)
    print(f'Section: {deb.section}', file=f)
    print('Priority: optional', file=f)
    print(f'Version: {deb.version}{deb.bump}', file=f)
    print(f'Description: {deb.description}', file=f)

  # create symlink to binary
  os.makedirs(os.path.join(deb.build, 'usr/local/bin'))
  os.symlink(f'/usr/lib/{deb.binary}/{deb.binary}', os.path.join(deb.build, 'usr/local/bin', deb.binary))

  def run(cmd):
    print('$', cmd)
    print(subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT).decode('utf-8'))

  # build deb
  if os.path.exists(deb.deb):
    os.remove(deb.deb)
  os.makedirs(os.path.dirname(deb.deb), exist_ok=True)
  run(f'fakeroot dpkg-deb --build -Zgzip {shlex.quote(deb.build)} {shlex.quote(deb.deb)}')
  run(f'dpkg-sig -k {shlex.quote(config.gpgkey)} --sign builder {shlex.quote(deb.deb)}')

# rebuild repo
config.repo = config.path['repo']
os.makedirs(config.repo, exist_ok=True)
with chdir(config.repo):
  run('rm -f *Package* *Release*')
  gpgkey = shlex.quote(config.gpgkey)
  relpath = shlex.quote(os.path.relpath(os.path.commonpath(config.path.values()), config.repo))
  run(f'apt-ftparchive packages {relpath} > Packages')
  run(f'bzip2 -kf Packages')
  run(f'apt-ftparchive -o APT::FTPArchive::AlwaysStat="true" -o APT::FTPArchive::Release::Acquire-By-Hash="yes" release . > Release')
  run(f'gpg --armor --export {gpgkey} > deb.gpg.key')
  run(f'gpg --yes -abs -u {gpgkey} -o Release.gpg --digest-algo sha256 Release')
  run(f'gpg --yes -abs -u {gpgkey} --clearsign -o InRelease --digest-algo sha256 Release')

  # apt is such a mess. https://blog.packagecloud.io/eng/2016/09/27/fixing-apt-hash-sum-mismatch-consistent-apt-repositories/
  hash_type = None
  run('rm -rf by-hash')
  with open('Release') as f:
    for line in f.readlines():
      line = line.rstrip()
      if line in [ 'MD5Sum:', 'SHA1:', 'SHA256:', 'SHA512:' ]:
        hash_type = line.replace(':', '')
      elif line.startswith(' '):
        hsh, size, filename = line.strip().split()

        if filename == 'Release': # how can Release contain it's own size and hash?!
          continue

        assert os.path.getsize(filename) == int(size), (filename, os.path.getsize(filename), int(size))

        with open(filename, 'rb') as f:
          hasher = getattr(hashlib, hash_type.lower().replace('sum', ''))
          should = hasher(f.read()).hexdigest()
          assert hsh == should, (filename, hash_type, 'mismatch')

        hash_dir = os.path.join('by-hash', hash_type)
        os.makedirs(hash_dir, exist_ok=True)
        run(f'cp {filename} {hash_dir}/{hsh}')