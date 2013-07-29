import json
import logging
import uuid
from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import FileSystemStorage
from django.http import HttpResponse
from django.utils._os import safe_join
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View

# Get an instance of a logger
logger = logging.getLogger()


class JSONResponseMixin(object):
    """
    A mixin that can be used to render a JSON response.
    """
    response_class = HttpResponse

    def render_to_response(self, context, **response_kwargs):
        """
        Returns a JSON response, transforming 'context' to make the payload.
        """
        response_kwargs['content_type'] = 'application/json'
        return self.response_class(
            self.convert_context_to_json(context),
            **response_kwargs
        )

    def convert_context_to_json(self, context):
        # Convert the context dictionary into a JSON object
        return json.dumps(context)


class UploadTempStorageFileSystem(object):
    def __init__(self, temp_file_folder):
        """
        Basic file system manager for handling uploaded files up until they're 'complete'

        @param temp_file_folder: A folder (within MEDIA_ROOT) where the uploads will be saved.
        @type temp_file_folder: str
        """
        self.temp_file_folder = temp_file_folder

    def open_temp_file(self, file_id):
        """
        Open the temp uploaded file.

        @param file_id: Unique ID of the uploaded file.
        @type file_id: str
        @return: Opened (as binary) File instance
        @rtype: File
        """
        fs = self._temp_file_storage()
        return fs.open(self._file_path(file_id), 'rb')

    def remove_temp_file(self, file_id):
        """
        Delete the temp uploaded file.

        @param file_id: Unique ID of the uploaded file
        @type file_id: str
        """
        fs = self._temp_file_storage()
        fs.delete(self._file_path(file_id))

    def append_content_to_temp_file(self, file_id, file_contents):
        """
        Append additional content to a temporary upload file (or create the file if it doesn't exist).

        @param file_id: Unique ID of the uploaded file.
        @type file_id: str
        @param file_contents: Contents to append to the temp file. If this is unicode, it's encoded in UTF-8.
        @type file_contents: str or unicode
        """
        if isinstance(file_contents, unicode):
            file_contents = file_contents.encode('utf-8')

        fs = self._temp_file_storage()
        with fs.open(self._file_path(file_id), 'ab+') as f:
            f.write(file_contents)

    def _file_path(self, file_id):
        return file_id

    def _temp_file_storage(self):
        location = safe_join(settings.MEDIA_ROOT, self.temp_file_folder)
        return FileSystemStorage(location)


class BaseUploadContentView(JSONResponseMixin, View):
    model = None
    temp_storage = None
    file_field_name = None
    uploaded_files_parameter = u'files'

    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        return super(BaseUploadContentView, self).dispatch(request, *args, **kwargs)

    def post(self, request, **kwargs):
        blob_file = request.FILES.get(self.uploaded_files_parameter, None)
        blob_size = blob_file._get_size()
        blob_name = blob_file.name

        if self.partial_upload_supported():
            #If files are chunked, the original file metadata is stored in extra META headers and the
            #POSTed file is called 'blob'. For files that aren't chunked, use the POSTed file directly.
            starting, ending, expected_byte_count = self._get_filesize_from_request_meta(request.META)
            if not expected_byte_count:
                expected_byte_count = blob_size
            expected_file_name = self._get_filename_from_request_meta(request.META) or blob_name
        else:
            starting = 0
            expected_byte_count = blob_size
            expected_file_name = blob_name

        chunked_file, file_finalised, file_id, uploaded_bytes_count = \
            self.handle_upload(blob_file, expected_file_name, expected_byte_count, starting)

        if not file_finalised:
            return self.render_to_response({'size': uploaded_bytes_count})

        try:
            self.object = self.create_and_save_object(expected_file_name, chunked_file)
            self._remove_temporary_file(file_id)
            return self.render_to_response({'files': [
                self.make_upload_response(expected_file_name)
            ]})
        except Exception, e:
            return self.render_to_response([{'name': expected_file_name, 'error': 'ERROR SAVING FILE'}])

    def handle_upload(self, uploaded_file, expected_file_name, expected_byte_count, starting_byte_index=None):
        """
        Handles the uploading of a file, either some or all of it.

        @param uploaded_file: The file or blob being uploaded.
        @type uploaded_file: UploadedFile
        @param expected_file_name: The name of the file being uploaded
        @type expected_file_name: str
        @param expected_byte_count: The final total size of the file being uploaded. We may be uploading only a portion
        of it.
        @type expected_byte_count: int
        @param starting_byte_index: Optional, the position in the file we're starting the upload from, if its a partial
        upload. Zero implies we're at the beginning of the file.
        @type starting_byte_index: int
        @return: An opened copy of the file if we're finished uploading it, a boolean to show if its been finished or
        not, the unique ID of the file, and a count of how much of the file has been uploaded so far
        @rtype: (File, bool, str, int)
        """
        # the file should be saved with a GUID name
        # we remember the guid name and size for a given uploaded file in the session

        # we should check the request here - if the content-range is starting at zero, kill the old stored name
        if (starting_byte_index == 0 or starting_byte_index is None) and self.partial_upload_supported():
            self.forget_about_upload(expected_file_name)

        existing_file_id = self.get_file_id(expected_file_name) if self.partial_upload_supported() else None

        chunked_file, file_id, file_finalised, uploaded_bytes_count = \
            self._write_upload(uploaded_file, expected_file_name, expected_byte_count, existing_file_id)

        if (not existing_file_id and file_id) and self.partial_upload_supported():
            self.set_file_id(expected_file_name, file_id)

        if file_finalised and self.partial_upload_supported():
            # Remove these bits from the session
            self.forget_about_upload(expected_file_name, file_id)

        return chunked_file, file_finalised, file_id, uploaded_bytes_count

    def make_upload_response(self, expected_file_name):
        """
        Get the data to be returned to the file uploaded, as a dictionary.

        By default, the name and size should be provided. It is therefore recommended when overriding to still call the
        base method and just update the resultant dictionary.

        @param expected_file_name: The name of the file that's been uploaded
        @type expected_file_name: str
        @return: Dictionary of useful information to return to the file upload widget
        @rtype: dict
        """
        file_field = self.get_file_field(self.object)
        return {
            'name': expected_file_name,
            'size': file_field.size,
        }

    def create_and_save_object(self, expected_file_name, chunked_file):
        """
        Creates an instance of the model for this view, attaches your uploaded file to it, and saves the model instance.

        @param expected_file_name: The name of the file we're attaching
        @type expected_file_name: str
        @param chunked_file: The content to save onto the object
        @type chunked_file: File
        @return: Saved instance of the model for this view
        @rtype: Model
        """
        o = self.create_new_instance()
        self.decorate_instance(o)

        self.save_in_content(o, chunked_file, expected_file_name)
        chunked_file.close()

        self.pre_save(o)
        o.save()
        self.post_save(o)

        return o

    def create_new_instance(self):
        """
        Create a new initial instance of the model for this view.

        @return: Instantiated model instance
        @rtype: Model
        """
        return self.model()

    def decorate_instance(self, instance):
        """
        Perform some work on the model instance, before a file is attached to it, and before it is saved.

        @param instance: An instance of the model for this view, not yet saved and with no attached file.
        @type instance: Model
        """
        pass

    def get_file_field(self, instance):
        """
        Access the file field for the model being worked on.

        @param instance: An instance of the model for this view
        @type instance: Model
        @return: Field on the model where the file is stored
        @rtype: FieldFile
        """
        return getattr(instance, self.file_field_name)

    def save_in_content(self, instance, chunked_file, expected_file_name):
        """
        Save some content onto an instance of your model.

        @param instance: An instance of the model for this view
        @type instance: Model
        @param chunked_file: The content to save onto the object
        @type chunked_file: File
        @param expected_file_name: The name of the file we're attaching
        @type expected_file_name: str
        """
        file_field = self.get_file_field(instance)
        file_field.save(expected_file_name, chunked_file)

    def pre_save(self, instance):
        """
        Perform some work on the model instance, before it has been saved.

        @param instance: An instance of the model for this view, not yet saved.
        @type instance: Model
        """
        pass

    def post_save(self, instance):
        """
        Perform some work on the model instance, after it has been saved.

        @param instance: An instance of the model for this view, saved.
        @type instance: Model
        """
        pass

    def generate_file_id(self, expected_file_name, expected_byte_count):
        """
        Generate a unique file ID for something being uploaded. The name and the size of the file are provided for
        convenience.

        @param expected_file_name: Name of the file being uploaded.
        @type expected_file_name: str
        @param expected_byte_count: Size of the file being uploaded.
        @type expected_byte_count: int
        @return: Unique ID for the file
        @rtype: str
        """
        return str(uuid.uuid4())

    def _get_filename_from_request_meta(self, meta):
        """
        Shortcut method for splitting out the file name from request.META

        @return: The name of the file being uploaded, taken from the Content-Disposition property, or None
        @rtype: str or None
        """
        http_content_disposition = meta.get('HTTP_CONTENT_DISPOSITION', None)
        if http_content_disposition:
            filename = http_content_disposition.split("filename=")[1].replace('\"', '')
            return filename
        return None

    def _get_filesize_from_request_meta(self, meta):
        """
        Shortcut method for splitting out the file size from request.META

        @return: Tuple of the number of position in the byte stream we're starting at, ending at, and the total size
        @rtype: (int, int, int)
        """
        http_content_range = meta.get('HTTP_CONTENT_RANGE', None)
        if http_content_range:
            preamble, expected_file_size = http_content_range.split('/')
            starting, ending = preamble.lstrip('bytes ').split('-')
            return int(starting), int(ending), int(expected_file_size)
        return None, None, None

    def _write_upload(self, uploaded_file, expected_file_name, expected_byte_count, file_id=None):
        logger.warning('%s, size: %s, id:%s' % (expected_file_name, expected_byte_count, file_id))
        if file_id is None:
            file_id = self.generate_file_id(expected_file_name, expected_byte_count)
            logging.warning('Id for %s: %s' % (expected_file_name, file_id))

        self.temp_storage.append_content_to_temp_file(file_id, uploaded_file.read())

        # How many bytes stored?
        these_bytes = uploaded_file.size
        uploaded_bytes_count = self.get_uploaded_bytes(file_id) if self.partial_upload_supported() else 0
        logger.warning('Uploaded so far for %s: %s' % (file_id, uploaded_bytes_count))
        uploaded_bytes_count += these_bytes
        logger.warning('Upload vs Expectation for %s: %s v %s' % (file_id, uploaded_bytes_count, expected_byte_count))

        if self.partial_upload_supported():
            self.update_uploaded_bytes(file_id, uploaded_bytes_count)

        if self.partial_upload_supported():
            finalised = False
            if (uploaded_bytes_count and expected_byte_count) is not None:
                finalised = int(uploaded_bytes_count) == int(expected_byte_count)
        else:
            finalised = True

        if finalised:
            return self.temp_storage.open_temp_file(file_id), file_id, finalised, uploaded_bytes_count

        return None, file_id, finalised, uploaded_bytes_count

    def _remove_temporary_file(self, file_id):
        self.temp_storage.remove_temp_file(file_id)

    def partial_upload_supported(self):
        return False

    def get_file_id(self, expected_file_name):
        """
        Get the unique file ID for a file we were uploading earlier. If we don't know the ID (such as when this is a new
        file), then return None.

        @param expected_file_name: The name of the file being uploaded.
        @type expected_file_name: str
        @return: Unique ID of the file being uploaded, or None if we don't have an ID for it yet.
        @rtype: str or None
        """
        raise NotImplementedError()

    def set_file_id(self, expected_file_name, file_id):
        """
        Remember the unique file ID for a file being/been uploaded.

        @param expected_file_name: The name of the file being uploaded.
        @type expected_file_name: str
        @param file_id: Unique ID of the file being uploaded
        @type file_id: str
        """
        raise NotImplementedError()

    def forget_about_upload(self, expected_file_name, file_id=None):
        """
        Forget about what has been uploaded so far for a particular file. This is used when we're either finished
        uploading a file, or when we're starting from scratch with a file we tried to upload earlier.

        @param expected_file_name: The name of the file being uploaded.
        @type expected_file_name: str
        @param file_id: Optional, unique ID of the file being uploaded. If you don't provide this, we try to fetch it.
        @type file_id: str
        """
        raise NotImplementedError()

    def get_uploaded_bytes(self, file_id):
        """
        For the given file ID, tell us how many bytes have been uploaded so far. If we don't know, the answer is zero.

        @param file_id: Unique ID of the file being uploaded
        @type file_id: str
        @return: Number of bytes uploaded so far, or zero if we don't know.
        @rtype: int
        """
        raise NotImplementedError()

    def update_uploaded_bytes(self, file_id, count):
        """
        Remember how many bytes have been uploaded so far for a given file ID.

        @param file_id: Unique ID of the file being uploaded
        @type file_id: str
        @param count: Number of bytes uploaded so far.
        @type count: int
        """
        raise NotImplementedError()


class PartialUploadCacheMixin(object):
    """
    Mixin for BaseUploadView to support partial uploads with Django cache and session key.
    """

    def partial_upload_supported(self):
        return True

    def get_file_id(self, expected_file_name):
        """
        Get the unique file ID for a file we were uploading earlier. If we don't know the ID (such as when this is a new
        file), then return None.

        @param expected_file_name: The name of the file being uploaded.
        @type expected_file_name: str
        @return: Unique ID of the file being uploaded, or None if we don't have an ID for it yet.
        @rtype: str or None
        """
        stored_name_key = self._stored_name_key(expected_file_name)
        stored_id = self._get_key(stored_name_key)
        logger.warning('ID for %s is %s' % (stored_name_key, stored_id))
        return stored_id

    def set_file_id(self, expected_file_name, file_id):
        """
        Remember the unique file ID for a file being/been uploaded.

        @param expected_file_name: The name of the file being uploaded.
        @type expected_file_name: str
        @param file_id: Unique ID of the file being uploaded
        @type file_id: str
        """
        return self._store_key(self._stored_name_key(expected_file_name), file_id)

    def forget_about_upload(self, expected_file_name, file_id=None):
        """
        Forget about what has been uploaded so far for a particular file. This is used when we're either finished
        uploading a file, or when we're starting from scratch with a file we tried to upload earlier.

        @param expected_file_name: The name of the file being uploaded.
        @type expected_file_name: str
        @param file_id: Optional, unique ID of the file being uploaded. If you don't provide this, we try to fetch it.
        @type file_id: str
        """
        if file_id is None:
            # try and get id so we can delete byte count
            file_id = self.get_file_id(expected_file_name)
        self._drop_key(self._stored_name_key(expected_file_name))
        if file_id is not None:
            self._drop_key(self._byte_count_key(file_id))

    def get_uploaded_bytes(self, file_id):
        """
        For the given file ID, tell us how many bytes have been uploaded so far. If we don't know, the answer is zero.

        @param file_id: Unique ID of the file being uploaded
        @type file_id: str
        @return: Number of bytes uploaded so far, or zero if we don't know.
        @rtype: int
        """
        return self._get_key(self._byte_count_key(file_id), 0)

    def update_uploaded_bytes(self, file_id, count):
        """
        Remember how many bytes have been uploaded so far for a given file ID.

        @param file_id: Unique ID of the file being uploaded
        @type file_id: str
        @param count: Number of bytes uploaded so far.
        @type count: int
        """
        self._store_key(self._byte_count_key(file_id), count)

    def _stored_name_key(self, expected_file_name):
        return '%s::%s::%s::file_id' % (self.request.session.session_key, self.model.__name__, expected_file_name)

    def _store_key(self, key, value):
        cache.set(key, value)

    def _get_key(self, key, default=None):
        return cache.get(key, default)

    def _drop_key(self, key):
        try:
            cache.delete(key)
        except KeyError:
            # ignore, key was never there
            pass

    def _byte_count_key(self, file_id):
        return '%s::%s::uploaded_byte_count' % (self.request.session.session_key, file_id)