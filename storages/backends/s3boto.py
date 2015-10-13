import logging
logger = logging.getLogger(__name__)

import os
import posixpath
import mimetypes
from gzip import GzipFile
import datetime
from tempfile import SpooledTemporaryFile
import hashlib

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO  # noqa

from django.core.files.base import File
from django.core.files.storage import Storage
from django.core.exceptions import ImproperlyConfigured, SuspiciousOperation
from django.utils.encoding import force_unicode, smart_str, filepath_to_uri

try:
    from boto import __version__ as boto_version
    from boto.s3.connection import S3Connection, SubdomainCallingFormat
    from boto.exception import S3ResponseError
    from boto.s3.key import Key as S3Key
    from boto.utils import parse_ts
except ImportError:
    raise ImproperlyConfigured("Could not load Boto's S3 bindings.\n"
                               "See https://github.com/boto/boto")

from storages.utils import setting

boto_version_info = tuple([int(i) for i in boto_version.split('-')[0].split('.')])

if boto_version_info[:2] < (2, 4):
    raise ImproperlyConfigured("The installed Boto library must be 2.4 or "
                               "higher.\nSee https://github.com/boto/boto")


def parse_ts_extended(ts):
    RFC1123 = '%a, %d %b %Y %H:%M:%S %Z'
    rv = None
    try:
        rv = parse_ts(ts)
    except ValueError:
        rv = datetime.datetime.strptime(ts, RFC1123)
    return rv


def safe_join(base, *paths):
    """
    A version of django.utils._os.safe_join for S3 paths.

    Joins one or more path components to the base path component
    intelligently. Returns a normalized version of the final path.

    The final path must be located inside of the base path component
    (otherwise a ValueError is raised).

    Paths outside the base path indicate a possible security
    sensitive operation.
    """
    from urlparse import urljoin
    base_path = force_unicode(base)
    base_path = base_path.rstrip('/')
    paths = [force_unicode(p) for p in paths]

    final_path = base_path
    for path in paths:
        final_path = urljoin(final_path.rstrip('/') + "/", path)

    # Ensure final_path starts with base_path and that the next character after
    # the final path is '/' (or nothing, in which case final_path must be
    # equal to base_path).
    base_path_len = len(base_path)
    if (not final_path.startswith(base_path) or
            final_path[base_path_len:base_path_len + 1] not in ('', '/')):
        raise ValueError('the joined path is located outside of the base path'
                         ' component')

    return final_path.lstrip('/')


class S3BotoStorageFile(File):
    """
    The default file object used by the S3BotoStorage backend.

    This file implements file streaming using boto's multipart
    uploading functionality. The file can be opened in read or
    write mode.

    This class extends Django's File class. However, the contained
    data is only the data contained in the current buffer. So you
    should not access the contained file object directly. You should
    access the data via this class.

    Warning: This file *must* be closed using the close() method in
    order to properly write the file to S3. Be sure to close the file
    in your application.
    """
    # TODO: Read/Write (rw) mode may be a bit undefined at the moment. Needs testing.
    # TODO: When Django drops support for Python 2.5, rewrite to use the
    #       BufferedIO streams in the Python 2.6 io module.
    buffer_size = setting('AWS_S3_FILE_BUFFER_SIZE', 5242880)

    def __init__(self, name, mode, storage, buffer_size=None):
        self._storage = storage
        self.name = name[len(self._storage.location):].lstrip('/')
        self._mode = mode
        self.key = storage.bucket.get_key(self._storage._encode_name(name))
        if not self.key and 'w' in mode:
            self.key = storage.bucket.new_key(storage._encode_name(name))
        self._is_dirty = False
        self._file = None
        self._multipart = None
        # 5 MB is the minimum part size (if there is more than one part).
        # Amazon allows up to 10,000 parts.  The default supports uploads
        # up to roughly 50 GB.  Increase the part size to accommodate
        # for files larger than this.
        if buffer_size is not None:
            self.buffer_size = buffer_size
        self._write_counter = 0

    @property
    def size(self):
        return self.key.size

    def _get_file(self):
        if self._file is None:
            self._file = SpooledTemporaryFile(
                max_size=self._storage.max_memory_size,
                suffix=".S3BotoStorageFile",
                dir=setting("FILE_UPLOAD_TEMP_DIR", None)
            )
            if 'r' in self._mode:
                self._is_dirty = False
                self.key.get_contents_to_file(self._file)
                self._file.seek(0)
            if self._storage.gzip and self.key.content_encoding == 'gzip':
                self._file = GzipFile(mode=self._mode, fileobj=self._file)
        return self._file

    def _set_file(self, value):
        self._file = value

    file = property(_get_file, _set_file)

    def read(self, *args, **kwargs):
        if 'r' not in self._mode:
            raise AttributeError("File was not opened in read mode.")
        return super(S3BotoStorageFile, self).read(*args, **kwargs)

    def write(self, *args, **kwargs):
        if 'w' not in self._mode:
            raise AttributeError("File was not opened in write mode.")
        self._is_dirty = True
        if self._multipart is None:
            provider = self.key.bucket.connection.provider
            upload_headers = {
                provider.acl_header: self._storage.default_acl
            }
            upload_headers.update({'Content-Type': mimetypes.guess_type(self.key.name)[0] or self._storage.key_class.DefaultContentType})
            upload_headers.update(self._storage.headers)
            self._multipart = self._storage.bucket.initiate_multipart_upload(
                self.key.name,
                headers=upload_headers,
                reduced_redundancy=self._storage.reduced_redundancy
            )
        if self.buffer_size <= self._buffer_file_size:
            self._flush_write_buffer()
        return super(S3BotoStorageFile, self).write(*args, **kwargs)

    @property
    def _buffer_file_size(self):
        pos = self.file.tell()
        self.file.seek(0, os.SEEK_END)
        length = self.file.tell()
        self.file.seek(pos)
        return length

    def _flush_write_buffer(self):
        """
        Flushes the write buffer.
        """
        if self._buffer_file_size:
            self._write_counter += 1
            self.file.seek(0)
            headers = self._storage.headers.copy()
            self._multipart.upload_part_from_file(
                self.file, self._write_counter, headers=headers)
            self.file.close()
            self._file = None

    def close(self):
        if self._is_dirty:
            self._flush_write_buffer()
            self._multipart.complete_upload()
        else:
            if not self._multipart is None:
                self._multipart.cancel_upload()
        self.key.close()


class S3BotoStorage(Storage):
    """
    Amazon Simple Storage Service using Boto

    This storage backend supports opening files in read or write
    mode and supports streaming(buffering) data in chunks to S3
    when writing.
    """
    connection_class = S3Connection
    connection_response_error = S3ResponseError
    file_class = S3BotoStorageFile
    key_class = S3Key

    # used for looking up the access and secret key from env vars
    access_key_names = ['AWS_S3_ACCESS_KEY_ID', 'AWS_ACCESS_KEY_ID']
    secret_key_names = ['AWS_S3_SECRET_ACCESS_KEY', 'AWS_SECRET_ACCESS_KEY']

    access_key = setting('AWS_S3_ACCESS_KEY_ID', setting('AWS_ACCESS_KEY_ID'))
    secret_key = setting('AWS_S3_SECRET_ACCESS_KEY', setting('AWS_SECRET_ACCESS_KEY'))
    file_overwrite = setting('AWS_S3_FILE_OVERWRITE', True)
    headers = setting('AWS_HEADERS', {})
    bucket_name = setting('AWS_STORAGE_BUCKET_NAME')
    auto_create_bucket = setting('AWS_AUTO_CREATE_BUCKET', False)
    default_acl = setting('AWS_DEFAULT_ACL', 'public-read')
    bucket_acl = setting('AWS_BUCKET_ACL', default_acl)
    querystring_auth = setting('AWS_QUERYSTRING_AUTH', True)
    querystring_expire = setting('AWS_QUERYSTRING_EXPIRE', 3600)
    reduced_redundancy = setting('AWS_REDUCED_REDUNDANCY', False)
    location = setting('AWS_LOCATION', '')
    encryption = setting('AWS_S3_ENCRYPTION', False)
    custom_domain = setting('AWS_S3_CUSTOM_DOMAIN')
    calling_format = setting('AWS_S3_CALLING_FORMAT', SubdomainCallingFormat())
    secure_urls = setting('AWS_S3_SECURE_URLS', True)
    file_name_charset = setting('AWS_S3_FILE_NAME_CHARSET', 'utf-8')
    gzip = setting('AWS_IS_GZIPPED', False)
    preload_metadata = setting('AWS_PRELOAD_METADATA', False)
    gzip_content_types = setting('GZIP_CONTENT_TYPES', (
        'text/css',
        'application/javascript',
        'application/x-javascript',
    ))
    url_protocol = setting('AWS_S3_URL_PROTOCOL', 'http:')
    host = setting('AWS_S3_HOST', S3Connection.DefaultHost)
    use_ssl = setting('AWS_S3_USE_SSL', True)
    port = setting('AWS_S3_PORT', None)

    # The max amount of memory a returned file can take up before being
    # rolled over into a temporary file on disk. Default is 0: Do not roll over.
    max_memory_size = setting('AWS_S3_MAX_MEMORY_SIZE', 0)

    def __init__(self, acl=None, bucket=None, **settings):
        # check if some of the settings we've provided as class attributes
        # need to be overwritten with values passed in here
        for name, value in settings.items():
            if hasattr(self, name):
                setattr(self, name, value)

        # For backward-compatibility of old differing parameter names
        if acl is not None:
            self.default_acl = acl
        if bucket is not None:
            self.bucket_name = bucket

        self.location = (self.location or '').lstrip('/')
        # Backward-compatibility: given the anteriority of the SECURE_URL setting
        # we fall back to https if specified in order to avoid the construction
        # of unsecure urls.
        if self.secure_urls:
            self.url_protocol = 'https:'

        self._entries = {}
        self._bucket = None
        self._connection = None

        if not self.access_key and not self.secret_key:
            self.access_key, self.secret_key = self._get_access_keys()

    @property
    def connection(self):
        if self._connection is None:
            self._connection = self.connection_class(
                self.access_key,
                self.secret_key,
                is_secure=self.use_ssl,
                calling_format=self.calling_format,
                host=self.host,
                port=self.port,
            )
        return self._connection

    @property
    def bucket(self):
        """
        Get the current bucket. If there is no current bucket object
        create it.
        """
        if self._bucket is None:
            self._bucket = self._get_or_create_bucket(self.bucket_name)
        return self._bucket

    @property
    def entries(self):
        """
        Get the locally cached files for the bucket.
        """
        if self.preload_metadata and not self._entries:
            self._entries = dict((self._decode_name(entry.key), entry)
                                for entry in self.bucket.list(prefix=self.location))
        return self._entries

    def _get_access_keys(self):
        """
        Gets the access keys to use when accessing S3. If none
        are provided to the class in the constructor or in the
        settings then get them from the environment variables.
        """
        def lookup_env(names):
            for name in names:
                value = os.environ.get(name)
                if value:
                    return value
        access_key = self.access_key or lookup_env(self.access_key_names)
        secret_key = self.secret_key or lookup_env(self.secret_key_names)
        return access_key, secret_key

    def _get_or_create_bucket(self, name):
        """
        Retrieves a bucket if it exists, otherwise creates it.
        """
        try:
            return self.connection.get_bucket(name,
                validate=self.auto_create_bucket)
        except self.connection_response_error:
            if self.auto_create_bucket:
                bucket = self.connection.create_bucket(name)
                bucket.set_acl(self.bucket_acl)
                return bucket
            raise ImproperlyConfigured("Bucket %s does not exist. Buckets "
                                       "can be automatically created by "
                                       "setting AWS_AUTO_CREATE_BUCKET to "
                                       "``True``." % name)

    def _clean_name(self, name):
        """
        Cleans the name so that Windows style paths work
        """
        # Normalize Windows style paths
        clean_name = posixpath.normpath(name).replace('\\', '/')

        # os.path.normpath() can strip trailing slashes so we implement
        # a workaround here.
        if name.endswith('/') and not clean_name.endswith('/'):
            # Add a trailing slash as it was stripped.
            return clean_name + '/'
        else:
            return clean_name

    def _normalize_name(self, name):
        """
        Normalizes the name so that paths like /path/to/ignored/../something.txt
        work. We check to make sure that the path pointed to is not outside
        the directory specified by the LOCATION setting.
        """
        try:
            return safe_join(self.location, name)
        except ValueError:
            raise SuspiciousOperation("Attempted access to '%s' denied." %
                                      name)

    def _encode_name(self, name):
        return smart_str(name, encoding=self.file_name_charset)

    def _decode_name(self, name):
        return force_unicode(name, encoding=self.file_name_charset)

    def _compress_content(self, content):
        """Gzip a given string content."""
        zbuf = StringIO()
        zfile = GzipFile(mode='wb', compresslevel=6, fileobj=zbuf)
        try:
            zfile.write(content.read())
        finally:
            zfile.close()
        zbuf.seek(0)
        content.file = zbuf
        content.seek(0)
        return content

    def _open(self, name, mode='rb'):
        name = self._normalize_name(self._clean_name(name))
        f = self.file_class(name, mode, self)
        if not f.key:
            raise IOError('File does not exist: %s' % name)
        return f

    def _save(self, name, content):
        cleaned_name = self._clean_name(name)
        name = self._normalize_name(cleaned_name)
        headers = self.headers.copy()
        content_type = getattr(content, 'content_type',
            mimetypes.guess_type(name)[0] or self.key_class.DefaultContentType)

        # setting the content_type in the key object is not enough.
        headers.update({'Content-Type': content_type})

        if self.gzip and content_type in self.gzip_content_types:
            content = self._compress_content(content)
            headers.update({'Content-Encoding': 'gzip'})

        content.name = cleaned_name
        encoded_name = self._encode_name(name)
        key = self.bucket.get_key(encoded_name)
        if not key:
            key = self.bucket.new_key(encoded_name)
        if self.preload_metadata:
            self._entries[encoded_name] = key

        key.set_metadata('Content-Type', content_type)
        self._save_content(key, content, headers=headers)
        return cleaned_name

    def _save_content(self, key, content, headers):
        current_md5 = key.etag.strip('"')
        new_md5 = hashlib.md5(content).hexdigest()
        logger.debug('For %s, comparing md5s of %s and %s',
                     key, current_md5, new_md5)
        if current_md5 == new_md5:
            logger.debug('File unchanged. Skipping.')
            return

        # only pass backwards incompatible arguments if they vary from the default
        kwargs = {}
        if self.encryption:
            kwargs['encrypt_key'] = self.encryption
        key.set_contents_from_file(content, headers=headers,
                                   policy=self.default_acl,
                                   reduced_redundancy=self.reduced_redundancy,
                                   rewind=True, **kwargs)

    def delete(self, name):
        name = self._normalize_name(self._clean_name(name))
        self.bucket.delete_key(self._encode_name(name))

    def exists(self, name):
        name = self._normalize_name(self._clean_name(name))
        if self.entries:
            return name in self.entries
        k = self.bucket.new_key(self._encode_name(name))
        return k.exists()

    def listdir(self, name):
        name = self._normalize_name(self._clean_name(name))
        # for the bucket.list and logic below name needs to end in /
        # But for the root path "" we leave it as an empty string
        if name and not name.endswith('/'):
            name += '/'

        dirlist = self.bucket.list(self._encode_name(name))
        files = []
        dirs = set()
        base_parts = name.split("/")[:-1]
        for item in dirlist:
            parts = item.name.split("/")
            parts = parts[len(base_parts):]
            if len(parts) == 1:
                # File
                files.append(parts[0])
            elif len(parts) > 1:
                # Directory
                dirs.add(parts[0])
        return list(dirs), files

    def size(self, name):
        name = self._normalize_name(self._clean_name(name))
        if self.entries:
            entry = self.entries.get(name)
            if entry:
                return entry.size
            return 0
        return self.bucket.get_key(self._encode_name(name)).size

    def modified_time(self, name):
        name = self._normalize_name(self._clean_name(name))
        entry = self.entries.get(name)
        # only call self.bucket.get_key() if the key is not found
        # in the preloaded metadata.
        if entry is None:
            entry = self.bucket.get_key(self._encode_name(name))
        # Parse the last_modified string to a local datetime object.
        return parse_ts_extended(entry.last_modified)

    def url(self, name, headers=None, response_headers=None):
        # Preserve the trailing slash after normalizing the path.
        name = self._normalize_name(self._clean_name(name))
        if self.custom_domain:
            return "%s//%s/%s" % (self.url_protocol,
                                  self.custom_domain, filepath_to_uri(name))
        return self.connection.generate_url(self.querystring_expire,
            method='GET', bucket=self.bucket.name, key=self._encode_name(name),
            headers=headers,
            query_auth=self.querystring_auth, force_http=not self.secure_urls,
            response_headers=response_headers)

    def get_available_name(self, name):
        """ Overwrite existing file with the same name. """
        if self.file_overwrite:
            name = self._clean_name(name)
            return name
        return super(S3BotoStorage, self).get_available_name(name)
