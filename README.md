django-jquery-upload
====================

Django app for supporting the [jQuery file upload](https://github.com/blueimp/jQuery-File-Upload) library. Designed to be flexibility adapted to different models.

Usage
-----

The app makes it easy for you to create a view that handles the uploading of files, where each file is associated with a particular instance of a model.

An example explains how to use it best:

```python
class MyModel(models.Model):
  content = models.FileField()


class MyUploadView(BaseUploadContentView):
  model = MyModel
  file_field_name = 'content'
  temp_storage = UploadTempStorageFileSystem('my-temp-files')  
```

By providing the model class, the name of the field for the file and a temp storage instance, you'll recieve a view that can accept the data POSTed by jQuery file upload.

Chunked Uploads
---------------

Chunked uploads require the server to persist an ID and a running total of the bytes sent between each part of the overall upload. Add the ```PartialUploadCacheMixin``` to your views superclasses and it'll use the default cache for your site to store this data.

If you prefer not to use the cache, you'll find the class very straightforward to replace.

Using this on your site
-----------------------

1. Set-up your custom view
2. Hook it in to your URLs
3. Set the `action` of the `<form>` element on your page to this new URL.

Thanks
------
Inspiration from [Morsels by stholmes](https://github.com/stholmes/Morsels)
