##
# Copyright (c) 2012, 2013 Sprymix Inc.
# All rights reserved.
#
# See LICENSE for details.
##


import hashlib
import gzip
import os
import logging
import subprocess
import sys
import re

from metamagic.node import Node
from metamagic.utils import config, fs
from metamagic.utils.datastructures import OrderedSet

from .resource import Resource, VirtualFile, AbstractFileSystemResource
from .exceptions import ResourceError


class ResourceBucketError(ResourceError):
    pass


class ResourcePublisherError(ResourceError, fs.FSError):
    pass


class ResourceBucketMeta(fs.BucketMeta):
    def __new__(mcls, name, bases, dct, **kwargs):
        dct['resources'] = None
        dct['published'] = []
        return super().__new__(mcls, name, bases, dct, **kwargs)


class ResourceBucket(fs.BaseBucket, metaclass=ResourceBucketMeta, abstract=True):
    logger = logging.getLogger('metamagic.utils.resource')

    can_contain = (AbstractFileSystemResource, VirtualFile)

    @classmethod
    def url(cls, resource):
        try:
            return getattr(resource, cls.id.hex)
        except KeyError:
            raise ResourceBucketError('unable to provide a url for an unpublished resource {!r}'.
                                      format(resource)) from None

    @classmethod
    def add(cls, resource):
        if not isinstance(resource, cls.can_contain):
            raise ResourceBucketError('resource bucket {!r} can\'t contain resource {!r}'.
                                      format(cls, resource))

        cls._error_if_abstract()
        if cls.resources is None:
            cls.resources = []
        cls.resources.append(resource)

    @classmethod
    def _collect_deps(cls):
        collected = OrderedSet()

        for resource in cls.resources:
            collected.update(Resource._list_resources(resource))

        return tuple(collected)

    @classmethod
    def set_backends(cls, *backends):
        if len(backends) != 1:
            raise ResourceBucketError('invalid backend {!r} for resource bucket {!r}, '
                                      'an instance of BaseResourceBackend is expected'.
                                      format(backend, cls))
        super().set_backends(*backends)

    @classmethod
    def validate_backend(cls, backend):
        if not isinstance(backend, BaseResourceBackend):
            raise ResourceBucketError('invalid backend {!r} for resource bucket {!r}, '
                                      'an instance of BaseResourceBackend is expected'.
                                      format(backend, cls))

    @classmethod
    def _build_dep_list(cls, name):
        visited = set()
        lst = []

        def collect(name):
            if name in visited:
                return
            visited.add(name)

            mod = sys.modules[name]
            lst.append(mod)

            if hasattr(mod, '__sx_imports__') and mod.__sx_imports__:
                for sub in mod.__sx_imports__:
                    collect(sub)

        collect(name)
        return lst

    @classmethod
    def build(cls):
        """Called during Node.build phase"""

        node = Node.active
        if not node.packages:
            cls.logger.info('node {} does not have any "packages" defined, this may manifest '
                            'in no resources being published'.format(node))

        for mod in node.packages:
            deps = cls._build_dep_list(mod.__name__)

            for mod in deps:
                if hasattr(mod, '__mm_module_tags__') and mod.__mm_module_tags__:
                    for tag in mod.__mm_module_tags__:
                        if hasattr(tag, 'resource_bucket') \
                                    and isinstance(mod, tag.resource_bucket.can_contain):
                            tag.resource_bucket.add(mod)

        for bucket in cls._iter_children(include_self=True):
            if bucket.resources:
                for backend in bucket.get_backends():
                    backend.publish_bucket(bucket)


class BaseResourceBackend(fs.backends.BaseFSBackend):
    def publish_bucket(self, bucket):
        raise NotImplementedError


class ResourceFSBackend(BaseResourceBackend):
    def __init__(self, *, path, pub_path, **kwargs):
        super().__init__(path=path, **kwargs)
        self.pub_path = pub_path

    def publish_bucket(self, bucket):
        bucket.published = OrderedSet()

        resources = self._collect_resources(bucket)
        assert resources

        bucket_id = bucket.id.hex
        bucket_path = os.path.join(self.path, bucket_id)
        os.makedirs(bucket_path, exist_ok=True, mode=(0o777 - self.umask))
        bucket_pub_path = os.path.join(self.pub_path, bucket_id)

        self._publish_bucket(bucket, resources, bucket_id, bucket_path, bucket_pub_path)

    def _publish_bucket(self, bucket, resources, bucket_id, bucket_path, bucket_pub_path):
        for resource in resources:
            if isinstance(resource, AbstractFileSystemResource):
                self._publish_fs_resource(bucket_path, bucket_pub_path, resource)
            elif isinstance(resource, VirtualFile):
                self._publish_virtual_resource(bucket_path, bucket_pub_path, resource)
            else:
                continue

            # XXX
            # We assign here a pub-url for the current bucket to later be able
            # to tell front-end where to download them from.  Definitely need
            # more sane API.
            #
            # P.S. The idea is that a bucket may only have one publisher, and
            # hence, be published only once.  But resources may belong to many
            # buckets, and hence published many times.
            #
            pub_path = os.path.join(bucket_pub_path, resource.__sx_resource_public_path__)
            setattr(resource, bucket_id, pub_path)

            bucket.published.add(resource)

    def _fix_css_links(self, source, bucket_path, *, rx=re.compile('///([^/]+)///')):
        # XXX
        #
        # This method patches links to media resources.
        # The problem with the current state of SCSS, is that in its current
        # architecture it's extremely slow.  The only possible way to speed it
        # up without a complete rewrite of compiler is to cache produced CSS.
        # However, this cache is created during the import phase, when it's
        # unknown what Node & and what configuration a system has.  Hence,
        # there is no way of guessing at what public URL resources will be
        # available.  Hence, this hack: "url" function in SCSS produces URLs like
        # "///media.module.name.object.name///", which are replaced by real
        # URLs here.
        #
        # NOTE: In case of refactoring, please update links to this comment
        # in "rendering.media" and this module.
        def cb(m):
            return os.path.join(bucket_path, m.group(1))
        return rx.sub(cb, source)

    def _collect_resources(self, bucket):
        collected = OrderedSet()
        for resource in bucket.resources:
            collected.update(Resource._list_resources(resource))
        return tuple(collected)

    def _publish_fs_resource(self, bucket_path, bucket_pub_path, resource):
        src_path = resource.__sx_resource_path__
        dest_path = os.path.join(bucket_path, resource.__sx_resource_public_path__)

        if os.path.exists(dest_path):
            if os.path.islink(dest_path):
                if os.stat(dest_path).st_ino == os.stat(src_path).st_ino:
                    # same file
                    return
                else:
                    os.unlink(dest_path)

            else:
                # not a symlink, let's just remove it
                if os.path.isfile(dest_path):
                    os.remove(dest_path)
                else:
                    os.rmdir(dest_path)

        elif os.path.islink(dest_path):
            # broken symlink
            os.unlink(dest_path)

        os.symlink(src_path, dest_path)

    def _publish_virtual_resource(self, bucket_path, bucket_pub_path, resource):
        dest_path = os.path.join(bucket_path, resource.__sx_resource_public_path__)

        if os.path.exists(dest_path):
            os.remove(dest_path)

        source = resource.__sx_resource_get_source__()

        if dest_path.endswith('.css') and b'///' in source:
            #: Read the comment in "_fix_css_links"
            source = self._fix_css_links(source.decode('utf-8'), bucket_pub_path).encode('utf-8')

        with open(dest_path, 'wb+') as dest:
            dest.write(source)


class OptimizedFSBackend(ResourceFSBackend):
    '''Compresses javascript and css files using YUI Compressor.
    Use it for production purposes.'''

    yui_compressor_path = config.cvalue('/usr/bin/yuicompressor', type=str,
                                        doc='Path to YUI Compressor executable')

    yui_compressor_jar = config.cvalue(None, type=str,
                                       doc='Path to YUI Compressor jar file, if no run '
                                           'script (yui_compressor_path) is available')

    java_path = config.cvalue('/usr/bin/java', type=str,
                              doc='Path to java executable, used in conjunction with '
                                  'yui_compressor_jar config option')

    gzip_output = config.cvalue(False, type=bool)
    compiled_module_name = config.cvalue('__compiled__', type=str)

    def _get_file_hash(self, filename):
        md5 = hashlib.md5()

        with open(filename, 'rb') as out:
            while True:
                data = out.read(4096)
                if not data:
                    break
                md5.update(data)

        return md5.hexdigest()

    def _publish_bucket(self, bucket, resources, bucket_id, bucket_path, bucket_pub_path):
        from metamagic.utils.lang.javascript import BaseJavaScriptModule, CompiledJavascriptModule
        from metamagic.rendering.css import ScssModule, ProxyScssModule, \
                                            CssModule, CompiledScssModule

        compressor_path = self.yui_compressor_path
        if compressor_path is None or self.yui_compressor_jar is not None:
            if self.yui_compressor_jar is None:
                raise ResourcePublisherError('Please configure {}.{}.yui_compressor_path '
                                             'config option'.
                                             format(self.__class__.__module__,
                                                    self.__class__.__name__))
            compressor_path = self.java_path + ' -jar ' + self.yui_compressor_jar

        js_deps = OrderedSet()
        css_deps = OrderedSet()

        for res in resources:
            if isinstance(res, BaseJavaScriptModule):
                js_deps.add(res)

            elif isinstance(res, (ScssModule, ProxyScssModule, CssModule)):
                css_deps.add(res)

            else:
                if isinstance(res, AbstractFileSystemResource):
                    self._publish_fs_resource(bucket_path, bucket_pub_path, res)
                elif isinstance(res, VirtualFile):
                    self._publish_virtual_resource(bucket_path, bucket_pub_path, res)
                else:
                    continue

                # Read XXX comment in "ResourceFSBackend._publish_bucket"
                pub_path = os.path.join(bucket_pub_path, res.__sx_resource_public_path__)
                setattr(res, bucket_id, pub_path)
                bucket.published.append(res)

        compiled_name = self.compiled_module_name
        compiled_name += (bucket.__module__ + '.' + bucket.__name__).replace('.', '_')

        for type, mod_cls, deps in (('js', CompiledJavascriptModule, js_deps),
                                    ('css', CompiledScssModule, css_deps)):

            output = os.path.abspath(os.path.join(bucket_path, '{}.{}'.format(compiled_name, type)))

            with open(output, 'wb') as out:
                for mod in deps:
                    if isinstance(mod, VirtualFile):
                        source = mod.__sx_resource_get_source__()
                        if type == 'css':
                            #: Read the comment in "_fix_css_links"
                            source = self._fix_css_links(source.decode('utf-8'), bucket_pub_path) \
                                                                                    .encode('utf-8')
                        out.write(source)
                    else:
                        with open(mod.__sx_resource_path__, 'rb') as i:
                            out.write(i.read())

                    if type == 'js':
                        out.write(b'\n;\n')

            command = [compressor_path,
                       '--line-break', '500',
                       '-v',
                       '--type', type,
                       output, '-o', output]

            command = ' '.join(command)

            status, result = subprocess.getstatusoutput(command)
            if status:
                raise ResourcePublisherError('{}\n\nFILE: {}'.format(result, output))

            output_gz = output + '.gz'
            if self.gzip_output:
                with open(output, 'rb') as f_in:
                    with gzip.open(output_gz, 'wb', compresslevel=9) as f_out:
                        f_out.writelines(f_in)

                hash = self._get_file_hash(output_gz)

                stats = os.stat(output)
                os.utime(output_gz, (stats.st_atime, stats.st_mtime))

            else:
                hash = self._get_file_hash(output)

                if os.path.exists(output_gz):
                    os.remove(output_gz)

            result = mod_cls(output.encode('utf-8'), compiled_name,
                             '{}.{}?_cache={}'.format(compiled_name, type, hash))
            pub_path = os.path.join(bucket_pub_path, result.__sx_resource_public_path__)
            setattr(result, bucket_id, pub_path)
            bucket.published.append(result)


def _collect_published_resources(bucket, types):
    collected = []
    for mod in bucket.published:
        if isinstance(mod, types):
            collected.append(bucket.url(mod))
    return collected


def render_script_tags(bucket):
    from metamagic.utils.lang.javascript import BaseJavaScriptModule
    collected = _collect_published_resources(bucket, BaseJavaScriptModule)

    return '\n'.join(('<script src="{}" type="text/javascript"></script>'.format(path)
                                                                        for path in collected))


def render_style_tags(bucket):
    from metamagic.rendering.css import ScssModule, ProxyScssModule, CssModule
    collected = _collect_published_resources(bucket, (ScssModule, ProxyScssModule, CssModule))

    return '\n'.join(('<link href="{}" type="text/css" rel="stylesheet"/>'.format(path)
                                                                        for path in collected))