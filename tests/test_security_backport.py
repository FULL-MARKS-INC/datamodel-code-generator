"""Regression tests for security fixes backported from upstream 0.60.2 - 0.64.0.

Each test reproduces the attack described in the upstream advisory and checks
that the backported fix rejects or neutralizes it.
"""

import ast
import http.server
import json
import socket
import threading
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterator, List

import httpcore
import pytest

import datamodel_code_generator.http as http_module
from datamodel_code_generator import Error
from datamodel_code_generator.parser.jsonschema import JsonSchemaParser

SECRET = 'TOP-SECRET-VALUE'
PUBLIC_IP = '93.184.216.34'
SECRET_SCHEMA: Dict[str, Any] = {
    'title': 'Secret',
    'type': 'object',
    'properties': {'api_key': {'type': 'string', 'default': SECRET}},
}


class _SchemaServer:
    """Local HTTP server standing in for an internal service."""

    def __init__(self) -> None:
        self.hits: List[str] = []
        hits = self.hits

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                hits.append(self.path)
                if self.path == '/redirect':
                    port = self.server.server_address[1]
                    self.send_response(302)
                    self.send_header(
                        'Location', f'http://internal.example.test:{port}/secret.json'
                    )
                    self.end_headers()
                    return
                body = json.dumps(SECRET_SCHEMA).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def server() -> Iterator[_SchemaServer]:
    schema_server = _SchemaServer()
    yield schema_server
    schema_server.close()


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[str]]:
    """Resolve test host names to the given IPs, one answer per lookup."""
    answers: Dict[str, List[str]] = {}
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host in answers:
            ips = answers[host]
            ip = ips.pop(0) if len(ips) > 1 else ips[0]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port or 0))]
        return original_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', getaddrinfo)
    return answers


def _write_schema(path: Path, schema: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema), encoding='utf-8')
    return path


def _schema_with_ref(ref: str) -> Dict[str, Any]:
    return {'title': 'Root', 'type': 'object', 'properties': {'s': {'$ref': ref}}}


# CVE-2026-54654 / GHSA-wjv6-jcfj-mf9r
@pytest.mark.parametrize('separator', ['\r', '\r\n', '\n', '\v', '\f'])
def test_extra_template_data_comment_cannot_inject_code(
    tmp_path: Path, separator: str
) -> None:
    source = _write_schema(
        tmp_path / 'schema.json',
        {'title': 'Pet', 'type': 'object', 'properties': {'name': {'type': 'string'}}},
    )
    extra_template_data = defaultdict(
        dict, {'Pet': {'comment': f'note{separator}    injected_by_comment = 1'}}
    )

    output = JsonSchemaParser(
        source=source, extra_template_data=extra_template_data
    ).parse()

    assigned_names = {
        target.id
        for node in ast.walk(ast.parse(output))
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert 'injected_by_comment' not in assigned_names
    for line in output.splitlines():
        if 'injected_by_comment' in line:
            assert line.lstrip().startswith('#')


# CVE-2026-55415 / GHSA-5578-w22f-pfx9
@pytest.mark.parametrize(
    'custom_type_path',
    [
        'os.path\nimport os',
        'os.path\rimport os',
        'os.path;import os',
        'os.path as p',
        'os.1path',
        'os.class',
        'Path',
    ],
)
@pytest.mark.parametrize('location', ['property', 'array_item', 'root'])
def test_custom_type_path_rejects_non_identifier_path(
    tmp_path: Path, custom_type_path: str, location: str
) -> None:
    item = {'type': 'string', 'customTypePath': custom_type_path}
    if location == 'property':
        schema = {'title': 'Model', 'type': 'object', 'properties': {'a': item}}
    elif location == 'array_item':
        schema = {
            'title': 'Model',
            'type': 'object',
            'properties': {'a': {'type': 'array', 'items': item}},
        }
    else:
        schema = {'title': 'Model', **item}
    source = _write_schema(tmp_path / 'schema.json', schema)

    with pytest.raises(Error, match='customTypePath'):
        JsonSchemaParser(source=source).parse()


def test_custom_type_path_accepts_dotted_identifier_path(tmp_path: Path) -> None:
    source = _write_schema(
        tmp_path / 'schema.json',
        {
            'title': 'Model',
            'type': 'object',
            'properties': {
                'a': {'type': 'string', 'customTypePath': 'datetime.datetime'}
            },
        },
    )

    output = JsonSchemaParser(source=source).parse()

    assert 'from datetime import datetime' in output
    assert 'a: Optional[datetime]' in output


# CVE-2026-54691 / GHSA-rfr2-mq9m-x2qx
@pytest.mark.parametrize(
    'url',
    [
        'http://localhost:{port}/secret.json',
        'http://127.0.0.1:{port}/secret.json',
        'http://127.1:{port}/secret.json',
        'http://2130706433:{port}/secret.json',
        'http://[::1]:{port}/secret.json',
        'http://[::ffff:127.0.0.1]:{port}/secret.json',
        'http://169.254.169.254/latest/meta-data/',
        'http://10.0.0.1/secret.json',
        'file:///etc/passwd',
    ],
)
def test_get_body_rejects_non_public_target(server: _SchemaServer, url: str) -> None:
    with pytest.raises(Error):
        http_module.get_body(url.format(port=server.port), timeout=3)
    assert server.hits == []


def test_get_body_allows_private_network_when_opted_in(server: _SchemaServer) -> None:
    body = http_module.get_body(
        f'http://127.0.0.1:{server.port}/secret.json', allow_private_network=True
    )

    assert SECRET in body
    assert server.hits == ['/secret.json']


def test_get_body_revalidates_redirect_target(
    server: _SchemaServer,
    fake_dns: Dict[str, List[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 127.0.0.1 stands in for a public address so the first hop reaches the test server.
    original_is_safe_ip = http_module._is_safe_ip
    monkeypatch.setattr(
        http_module,
        '_is_safe_ip',
        lambda ip: str(ip) == '127.0.0.1' or original_is_safe_ip(ip),
    )
    fake_dns['schema.example.test'] = ['127.0.0.1']
    fake_dns['internal.example.test'] = ['10.0.0.5']

    with pytest.raises(Error, match='internal.example.test'):
        http_module.get_body(f'http://schema.example.test:{server.port}/redirect')
    assert server.hits == ['/redirect']


# CVE-2026-55391 / GHSA-vx7x-vcc2-c44g
def test_get_body_pins_validated_address_against_dns_rebinding(
    server: _SchemaServer,
    fake_dns: Dict[str, List[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The first lookup (validation) answers a public IP, later lookups answer loopback.
    fake_dns['schema.example.test'] = [PUBLIC_IP, '127.0.0.1']
    connected_hosts: List[str] = []
    original_connect_tcp = httpcore.SyncBackend.connect_tcp

    def connect_tcp(self: Any, host: str, port: int, **kwargs: Any) -> Any:
        connected_hosts.append(host)
        if host != '127.0.0.1':
            raise OSError(f'outbound connection to {host} suppressed in test')
        return original_connect_tcp(self, host, port, **kwargs)

    monkeypatch.setattr(httpcore.SyncBackend, 'connect_tcp', connect_tcp)

    with pytest.raises(Error):
        http_module.get_body(
            f'http://schema.example.test:{server.port}/secret.json', timeout=3
        )
    assert connected_hosts == [PUBLIC_IP]
    assert server.hits == []


# CVE-2026-54690 / GHSA-954p-556p-r752
def test_http_ref_to_private_network_is_not_fetched(
    tmp_path: Path, server: _SchemaServer
) -> None:
    source = _write_schema(
        tmp_path / 'schema.json',
        _schema_with_ref(f'http://127.0.0.1:{server.port}/secret.json'),
    )

    with pytest.warns(FutureWarning), pytest.raises(Error):
        JsonSchemaParser(source=source).parse()
    assert server.hits == []


def test_http_ref_is_rejected_when_remote_refs_disabled(
    tmp_path: Path, server: _SchemaServer
) -> None:
    source = _write_schema(
        tmp_path / 'schema.json',
        _schema_with_ref(f'http://127.0.0.1:{server.port}/secret.json'),
    )

    with pytest.raises(Error, match='remote \\$ref is disabled'):
        JsonSchemaParser(source=source, allow_remote_refs=False).parse()
    assert server.hits == []


# CVE-2026-55389 / GHSA-8359-h9fx-j6v9
@pytest.fixture
def outside_secret(tmp_path: Path) -> Path:
    return _write_schema(tmp_path / 'secret.json', SECRET_SCHEMA).resolve()


@pytest.mark.parametrize('ref_kind', ['parent_relative', 'absolute'])
def test_ref_outside_base_path_is_blocked_when_remote_refs_disabled(
    tmp_path: Path, outside_secret: Path, ref_kind: str
) -> None:
    ref = '../secret.json' if ref_kind == 'parent_relative' else str(outside_secret)
    source = _write_schema(tmp_path / 'base' / 'schema.json', _schema_with_ref(ref))

    with pytest.raises(Error, match='outside the input base path'):
        JsonSchemaParser(source=source, allow_remote_refs=False).parse()


def test_ref_outside_base_path_warns_by_default(
    tmp_path: Path, outside_secret: Path
) -> None:
    source = _write_schema(
        tmp_path / 'base' / 'schema.json', _schema_with_ref('../secret.json')
    )

    with pytest.warns(FutureWarning, match='outside the input base path'):
        JsonSchemaParser(source=source).parse()


def test_ref_outside_base_path_is_allowed_when_remote_refs_enabled(
    tmp_path: Path, outside_secret: Path
) -> None:
    source = _write_schema(
        tmp_path / 'base' / 'schema.json', _schema_with_ref('../secret.json')
    )

    with warnings.catch_warnings():
        warnings.simplefilter('error', FutureWarning)
        output = JsonSchemaParser(source=source, allow_remote_refs=True).parse()
    assert SECRET in output


def test_ref_inside_base_path_is_unaffected(tmp_path: Path) -> None:
    _write_schema(tmp_path / 'base' / 'models' / 'pet.json', SECRET_SCHEMA)
    source = _write_schema(
        tmp_path / 'base' / 'schema.json', _schema_with_ref('./models/pet.json')
    )

    with warnings.catch_warnings():
        warnings.simplefilter('error', FutureWarning)
        output = JsonSchemaParser(source=source, allow_remote_refs=False).parse()
    assert SECRET in output


def test_file_uri_ref_does_not_read_outside_base_path(
    tmp_path: Path, outside_secret: Path
) -> None:
    source = _write_schema(
        tmp_path / 'base' / 'schema.json', _schema_with_ref(outside_secret.as_uri())
    )

    try:
        output = JsonSchemaParser(source=source, allow_remote_refs=False).parse()
    except (Error, OSError):
        return
    assert SECRET not in output
