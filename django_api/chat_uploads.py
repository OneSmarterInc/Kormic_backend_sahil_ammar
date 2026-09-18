"""Stop over-budget multipart streams before writing full files to disk."""
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from django_api.chat_policy import MAX_ATTACHMENTS, MAX_ATTACHMENT_BYTES, MAX_TOTAL_ATTACHMENT_BYTES


class ChatUploadBudgetHandler(FileUploadHandler):
    def __init__(self, request):
        super().__init__(request)
        self.total = self.count = self.file_size = 0
        self.exceeded = False

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.count += 1
        self.file_size = 0
        if self.count > MAX_ATTACHMENTS:
            self.stop()

    def receive_data_chunk(self, raw_data, start):
        self.total += len(raw_data)
        self.file_size += len(raw_data)
        if self.file_size > MAX_ATTACHMENT_BYTES or self.total > MAX_TOTAL_ATTACHMENT_BYTES:
            self.stop()
        return raw_data

    def file_complete(self, file_size):
        return None  # Let the normal memory/temp-file handler own accepted files.

    def stop(self):
        self.exceeded = True
        raise StopUpload(connection_reset=True)
