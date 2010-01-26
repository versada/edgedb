import postgresql
import urllib.parse

from semantix.caos.backends.resolver.shell import BackendShell, BackendResolverHelper
from semantix.caos.backends.pgsql.backend import Backend

class BackendResolver(BackendResolverHelper):
    def resolve(self, url):
        url = urllib.parse.urlunsplit(url)
        connection = postgresql.open(url)
        return BackendShell(backend_class=Backend, connection=connection)
