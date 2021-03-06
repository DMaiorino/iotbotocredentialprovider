import botocore.auth
import datetime
import json
import logging
import random
from threading import Timer
from .AWS import IotBotoCredentialProvider

try:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
except ImportError:
    from http.server import BaseHTTPRequestHandler, HTTPServer


log = logging.getLogger()
log.setLevel(logging.INFO)


# hosts = ["169.254.169.254", "169.254.170.2"]
# loopback is preferred, this will require that
# the following be set:
# /sbin/sysctl -w net.ipv4.conf.all.route_localnet=1
# /sbin/iptables -t nat -A PREROUTING -p tcp -d 169.254.169.254 --dport 80 -j DNAT --to-destination 127.0.0.1:51680
# may need the below, too
# /sbin/iptables -t nat -A PREROUTING -p tcp -d 169.254.170.2   --dport 80 -j DNAT --to-destination 127.0.0.1:51680

# hosts = ["169.254.169.254", "169.254.170.2"] # <%= @ip_address_list %>
HOST = "127.0.0.1"
PORT = 51680
HOST = "0.0.0.0"
ROLE_PATH = "/latest/meta-data/iam/security-credentials/"
PING_PATH = "/ping"
PING_RESPONSE = "pong"


def json_serial(obj):
    """
    JSON serializer for objects not serializable by default json code
    e.g. datetime.datetime objects
    """

    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    raise TypeError("Type %s not serializable" % type(obj))


class FakeMetadataCredentialProvider(IotBotoCredentialProvider):
    @property
    def role_name(self):
        return self.metadata['role_alias_name']

    @property
    def metadata_credentials(self):
        return {
            'AccessKeyId': self.credentials['accessKeyId'],
            'SecretAccessKey': self.credentials['secretAccessKey'],
            'Token': self.credentials['sessionToken'],
            'Expiration': self.credentials['expiration']
        }

    def update_timer(self, refresh_time_seconds=300):
        self._update_timer = Timer(refresh_time_seconds, self.get_credentials)
        self._update_timer.daemon = True
        logging.info("will refresh creds in %s", refresh_time_seconds)
        self._update_timer.start()

    def cancel_timer(self):
        if hasattr(self, "_update_timer"):
            self._update_timer.cancel()

    def get_refresh_seconds(self):
        if not hasattr(self, "_credential_expiration"):
            expire_time = datetime.datetime.strptime(self.credentials['expiration'],
                                                     botocore.auth.ISO8601)
            self._credential_expiration = expire_time

        now = datetime.datetime.utcnow()
        expiration = (self._credential_expiration - now).seconds
        logging.debug("credentials expire in %s seconds", expiration)
        refresh_jitter = int(0.1 * expiration)
        if refresh_jitter < 30:
            refresh_jitter = 30
        refresh_time = 0.7 * expiration + random.randrange(0, refresh_jitter)
        return refresh_time

    def get_credentials(self):
        result = super(FakeMetadataCredentialProvider, self).get_credentials()
        self.update_timer(self.get_refresh_seconds())
        return result


class FakeMetadataRequestHandler(BaseHTTPRequestHandler):
    """
    This implements the request handling that we'll
    need, it responds very simply:

    if user requests ROLE_PATH, respond with the role name we serve
    if user requests ROLE_PATH + role name, respond with credentials
        obtained by self.get_credentials(RoleArn)
    otherwise, return a 404

    This class shouldn't directly be used, instead use a child
    which implements get_credentials

    """
    # we want to use the same provider across all class instances
    # to allow for caching
    credential_provider = FakeMetadataCredentialProvider()

    def get_credentials(self, RoleArn=None):
        return FakeMetadataRequestHandler.credential_provider.metadata_credentials

    def get_role(self):
        return FakeMetadataRequestHandler.credential_provider.role_name

    def do_GET(self):
        our_role = self.get_role()
        our_path = ROLE_PATH + self.get_role()
        return_code = 200
        start_doc = "HTTP/1.0 200 OK\Content-Type: text/plain\n\n"
        result = ""

        if self.path == PING_PATH:
            result = PING_RESPONSE
        elif self.path == ROLE_PATH:
            # client is requesting we return the role name
            result = our_role
        elif self.path != our_path:
            # client asked for a role we don't serve
            return_code = 404
            start_doc = "HTTP/1.0 400 Bad Request\nContent-Type: text/html\n"
            result = """
<?xml version="1.0" encoding="iso-8859-1"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"
         "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
 <head>
  <title>404 - Not Found</title>
 </head>
 <body>
  <h1>404 - Not Found</h1>
 </body>
</html>
"""
        else:
            # client asked for credentials
            # start_doc ="HTTP/1.0 200 OK\nContent-Type: application/json\n\n"
            credentials = self.get_credentials()
            result = json.dumps(credentials, default=json_serial, indent=4)
        self.send_response(return_code)
        self.wfile.write(bytes(start_doc.encode("utf-8") + result.encode("utf-8")))


class FakeMetadataServer(object):
    """
    This creates a server which acts like METADATA
    You will need to have certain traffic directed to it, e.g.

    /sbin/sysctl -w net.ipv4.conf.all.route_localnet=1
    /sbin/iptables -t nat -A PREROUTING -p tcp -d 169.254.169.254 --dport 80 -j DNAT --to-destination 127.0.0.1:51679

    TBD: may not need to redirect the container address, this may
    be best left to ECSAgent, the container address is:

    /sbin/iptables -t nat -A PREROUTING -p tcp -d 169.254.170.2   --dport 80 -j DNAT --to-destination 127.0.0.1:51679

    """

    def __init__(self, request_handler, host=None, port=None):
        self.request_handler = request_handler
        if host is None:
            self.host = HOST
        else:
            self.host = host

        self.port = port
        if self.port is None:
            self.port = PORT

        self.server = HTTPServer((self.host, self.port), self.request_handler)

    def stop(self):
        self.request_handler.credential_provider.cancel_timer()
        self.server.shutdown()
        self.server.server_close()

    def run(self):
        print("run server on %s:%s" % (self.host, self.port))
        self.server.serve_forever()
        self.request_handler.credential_provider.cancel_timer()
        self.server.shutdown()
        self.server.server_close()
