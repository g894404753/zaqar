# Copyright (c) 2015 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import io
import json

from autobahn.asyncio import websocket
from oslo_log import log as logging
from oslo_utils import timeutils
import pytz
import txaio

try:
    import asyncio
except ImportError:
    import trollius as asyncio

try:
    import mimetools
    Message = mimetools.Message
except ImportError:
    from email.mime import message
    Message = message.MIMEMessage


LOG = logging.getLogger(__name__)


class MessagingProtocol(websocket.WebSocketServerProtocol):

    _fake_env = {
        'REQUEST_METHOD': 'POST',
        'SERVER_NAME': 'zaqar',
        'SERVER_PORT': 80,
        'SERVER_PROTOCOL': 'HTTP/1.1',
        'PATH_INFO': '/',
        'SCRIPT_NAME': '',
        'wsgi.url_scheme': 'http'
    }

    def __init__(self, handler, proto_id, auth_strategy, loop):
        txaio.use_asyncio()
        websocket.WebSocketServerProtocol.__init__(self)
        self._handler = handler
        self.proto_id = proto_id
        self._auth_strategy = auth_strategy
        self._loop = loop
        self._authentified = False
        self._auth_app = None
        self._deauth_handle = None

    def onConnect(self, request):
        print("Client connecting: {0}".format(request.peer))

    def onOpen(self):
        print("WebSocket connection open.")

    def onMessage(self, payload, isBinary):
        if isBinary:
            # TODO(vkmc): Binary support will be added in the next cycle
            # For now, we are returning an invalid request response
            print("Binary message received: {0} bytes".format(len(payload)))
            body = {'error': 'Schema validation failed.'}
            resp = self._handler.create_response(400, body)
            return self._send_response(resp)
        try:
            print("Text message received: {0}".format(payload))
            payload = json.loads(payload)
        except ValueError as ex:
            LOG.exception(ex)
            body = {'error': str(ex)}
            resp = self._handler.create_response(400, body)
            return self._send_response(resp)

        req = self._handler.create_request(payload)
        resp = self._handler.validate_request(payload, req)
        if resp is None:
            if self._auth_strategy and not self._authentified:
                if self._auth_app or payload.get('action') != 'authenticate':
                    if 'URL-Signature' in payload.get('headers', {}):
                        if self._handler.verify_signature(
                                self.factory._secret_key, payload):
                            resp = self._handler.process_request(req, self)
                        else:
                            body = {'error': 'Not authentified.'}
                            resp = self._handler.create_response(
                                403, body, req)
                    else:
                        body = {'error': 'Not authentified.'}
                        resp = self._handler.create_response(403, body, req)
                else:
                    return self._authenticate(payload)
            elif payload.get('action') == 'authenticate':
                return self._authenticate(payload)
            else:
                resp = self._handler.process_request(req, self)
        return self._send_response(resp)

    def onClose(self, wasClean, code, reason):
        print("WebSocket connection closed: {0}".format(reason))

    def _authenticate(self, payload):
        self._auth_app = self._auth_strategy(self._auth_start)
        env = self._fake_env.copy()
        env.update(
            (self._header_to_env_var(key), value)
            for key, value in payload.get('headers').items())
        self._auth_app(env, self._auth_response)

    def _auth_start(self, env, start_response):
        self._authentified = True
        self._auth_app = None
        expire = env['keystone.token_info']['token']['expires_at']
        expire_time = timeutils.parse_isotime(expire)
        now = datetime.datetime.now(tz=pytz.UTC)
        delta = (expire_time - now).total_seconds()
        if self._deauth_handle is not None:
            self._deauth_handle.cancel()
        self._deauth_handle = self._loop.call_later(
            delta, self._deauthenticate)

        start_response('200 OK', [])

    def _deauthenticate(self):
        self._authentified = False
        self.sendClose(4003, u'Authentication expired.')

    def _auth_response(self, status, message):
        code = int(status.split()[0])
        req = self._handler.create_request({'action': 'authenticate'})
        if code != 200:
            body = {'error': 'Authentication failed.'}
            resp = self._handler.create_response(code, body, req)
            self._send_response(resp)
        else:
            body = {'message': 'Authentified.'}
            resp = self._handler.create_response(200, body, req)
            self._send_response(resp)

    def _header_to_env_var(self, key):
        return 'HTTP_%s' % key.replace('-', '_').upper()

    def _send_response(self, resp):
        resp_json = json.dumps(resp.get_response())
        self.sendMessage(resp_json, False)


class NotificationProtocol(asyncio.Protocol):

    def __init__(self, factory):
        self._factory = factory

    def connection_made(self, transport):
        self._transport = transport
        self._data = bytearray()
        self._state = 'INIT'
        self._subscriber_id = None
        self._length = 0

    def write_status(self, status):
        self._transport.write(b'HTTP/1.0 %s\r\n\r\n' % status)
        self._transport.close()

    def data_received(self, data):
        self._data.extend(data)
        if self._state == 'INIT' and b'\r\n' in self._data:
            first_line, self._data = self._data.split(b'\r\n', 1)
            verb, uri, version = first_line.split()
            if verb != b'POST':
                self.write_status(b'405 Not Allowed')
                return
            self._state = 'HEADERS'
            self._subscriber_id = uri[1:]

        if self._state == 'HEADERS' and b'\r\n\r\n' in self._data:
            headers, self._data = self._data.split(b'\r\n\r\n', 1)
            headers = Message(io.BytesIO(headers))
            length = headers.get(b'content-length')
            if not length:
                self.write_status(b'400 Bad Request')
                return
            self._length = int(length)
            self._state = 'BODY'

        if self._state == 'BODY':
            if len(self._data) >= self._length:
                if self._subscriber_id:
                    self._factory.send_data(bytes(self._data),
                                            str(self._subscriber_id))
                    self.write_status(b'200 OK')
                else:
                    self.write_status(b'400 Bad Request')

    def connection_lost(self, exc):
        self._data = self._subscriber_id = None
        self._length = 0
