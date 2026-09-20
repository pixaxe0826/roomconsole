"""Real OpenSSL verification and generated nginx -t; no shared certificate keys."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import pytest

pytestmark=pytest.mark.skipif(os.name!='posix' or not shutil.which('nginx') or not shutil.which('openssl'),reason='Requires POSIX, nginx and OpenSSL')

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('rh_https',ROOT/'deploy/termux/setup_https.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)


def test_certificate_and_nginx_config(tmp_path):
    root=tmp_path/'root';dest=mod.setup(root,'192.168.0.14')
    assert (dest/'private/ca.key').stat().st_mode & 0o777 == 0o600
    ca=(dest/'private/ca.crt').read_bytes();leaf=(dest/'private/server.crt').read_bytes()
    assert (dest/'public/roomhub-ca.cer').read_bytes()!=ca
    for ip in ['192.168.0.14','127.0.0.1']:
        subprocess.run(['openssl','verify','-CAfile',str(dest/'private/ca.crt'),'-verify_ip',ip,str(dest/'private/server.crt')],check=True,capture_output=True)
    bad=subprocess.run(['openssl','verify','-CAfile',str(dest/'private/ca.crt'),'-verify_ip','192.168.0.99',str(dest/'private/server.crt')],capture_output=True)
    assert bad.returncode!=0
    conf=(dest/'nginx.conf').read_text();assert 'listen 8443 ssl' in conf
    assert 'Host $http_host' in conf and 'proxy_cookie_flags ~ secure' in conf and 'Upgrade $http_upgrade' in conf
    assert '/etc/nginx' not in conf and 'listen 8080' not in conf
    assert 'access_log off' in conf
    subprocess.run(['nginx','-t','-p',str(dest)+'/', '-c',str(dest/'nginx.conf')],check=True,capture_output=True)
    mod.setup(root,'192.168.0.14')
    assert (dest/'private/ca.crt').read_bytes()==ca and (dest/'private/server.crt').read_bytes()==leaf
    mod.setup(root,'192.168.0.14',renew=True)
    assert (dest/'private/ca.crt').read_bytes()==ca and (dest/'private/server.crt').read_bytes()!=leaf


@pytest.mark.parametrize('ip',['8.8.8.8','127.0.0.1','0.0.0.0','192.168.0.14;rm','not-an-ip'])
def test_wrong_ip_rejected(tmp_path,ip):
    with pytest.raises(ValueError):mod.setup(tmp_path,ip)


@pytest.mark.parametrize('port',[8080,8088,8022,80,99999])
def test_existing_service_port_rejected(tmp_path,port):
    with pytest.raises(ValueError):mod.setup(tmp_path,'192.168.0.14',port=port)
